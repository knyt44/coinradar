import os
import time
import json
from datetime import datetime, timezone

import requests

# =========================================================
# CONFIG
# =========================================================
SYMBOL = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"

MEXC_BASE_URL = "https://api.mexc.com"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    or os.getenv("BOT_TOKEN", "").strip()
)

TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID", "").strip()
    or os.getenv("CHAT_ID", "").strip()
    or os.getenv("TELEGRAM_CHAT_ID_ETH", "").strip()
    or os.getenv("CHAT_ID_ETH", "").strip()
)

CHECK_EVERY_SECONDS = 20
STATE_FILE = "eth_mexc_usdtd_core_state.json"

# =========================================================
# GENERAL
# =========================================================
MAX_HTTP_RETRY = 3
HTTP_TIMEOUT = 15

# =========================================================
# SIGNAL / LIFECYCLE
# =========================================================
MIN_SIGNAL_GAP_MINUTES = 15
SIGNAL_TIMEOUT_MINUTES = 90
MAX_ACTIVE_SIGNAL_AGE_MINUTES = 240
MIN_PRICE_DISTANCE_PCT = 0.22

ALLOW_SMART_REPEAT_SIGNAL = False
RESEND_IF_SCORE_IMPROVED_BY = 1.0
RESEND_IF_ENTRY_MOVED_PCT = 0.20

ENTRY_FILL_TOLERANCE_PCT = 0.08
CANCEL_IF_TP1_BEFORE_ENTRY = True

# 15m post-entry confirmation
POST_ENTRY_CONFIRM_WINDOW_MINUTES = 18
FAIL_CONFIRM_CLOSE_SIGNAL = True
SOFT_CONFIRM_MODE = False

# =========================================================
# THRESHOLDS
# =========================================================
LONG_SCORE_THRESHOLD = 10.2
SHORT_SCORE_THRESHOLD = 10.2
MIN_RR_TO_TP2 = 1.25

# =========================================================
# RISK
# =========================================================
ATR_SL_MULTIPLIER = 1.25
ATR_TP1_MULTIPLIER = 1.10
ATR_TP2_MULTIPLIER = 2.00
ATR_TP3_MULTIPLIER = 3.10
TRAIL_AFTER_TP2_ATR = 1.00

# =========================================================
# FILTERS
# =========================================================
USE_BTC_FILTER = True
USE_USDTD_FILTER = True

USDTD_FAST_EMA = 6
USDTD_SLOW_EMA = 18
USDTD_HISTORY_LIMIT = 320
CG_CACHE_TTL_SECONDS = 180

REQUIRE_4H_TREND_LOCK = True
REQUIRE_BTC_CONFIRMATION = True

# USDT.D artık merkezde
USDTD_HARD_VETO_ENABLED = True
USDTD_SOFT_PENALTY_ENABLED = True

# Güçlü rejim eşikleri
USDTD_STRONG_DIFF_PCT = 0.22   # fast/slow farkı %
USDTD_WEAK_DIFF_PCT = 0.08
USDTD_STRONG_NOW_FAST_PCT = 0.08
USDTD_WEAK_NOW_FAST_PCT = 0.03

# =========================================================
# UTILS
# =========================================================
def now_utc():
    return datetime.now(timezone.utc)

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def utc_ts():
    return int(now_utc().timestamp())

def log(msg: str):
    print(f"[{now_str()}] {msg}", flush=True)

def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def pct_change(a, b):
    a = safe_float(a)
    b = safe_float(b)
    if a in (None, 0) or b is None:
        return 0.0
    return ((b - a) / a) * 100.0

def pct_diff(a, b):
    a = safe_float(a)
    b = safe_float(b)
    if a in (None, 0) or b is None:
        return 999.0
    return abs(a - b) / a * 100.0

def minutes_since(ts):
    if not ts:
        return 999999
    return (utc_ts() - int(ts)) / 60.0

def fmt_price(v):
    try:
        v = float(v)
        if v >= 1000:
            return f"{v:.2f}"
        elif v >= 100:
            return f"{v:.2f}"
        elif v >= 1:
            return f"{v:.4f}"
        return f"{v:.6f}"
    except Exception:
        return str(v)

def clamp(v, a, b):
    return max(a, min(b, v))

def day_key_from_ts(ts):
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")

def week_key_from_ts(ts):
    dt = datetime.fromtimestamp(int(ts))
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"

# =========================================================
# TELEGRAM
# =========================================================
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram ENV eksik")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            return True
        log(f"Telegram hata: {r.status_code} | {r.text}")
        return False
    except Exception as e:
        log(f"Telegram exception: {e}")
        return False

def tg_get_updates(offset):
    if not TELEGRAM_TOKEN:
        return []

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 1}

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            return []
        return data.get("result", [])
    except Exception as e:
        log(f"Telegram updates exception: {e}")
        return []

# =========================================================
# HTTP
# =========================================================
def http_get_json(url: str, params=None):
    last_err = None
    for attempt in range(MAX_HTTP_RETRY):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < MAX_HTTP_RETRY - 1:
                time.sleep(1.0)
    raise last_err

# =========================================================
# MEXC
# =========================================================
def get_klines(symbol: str, interval: str, limit: int = 200):
    url = f"{MEXC_BASE_URL}/api/v3/klines"
    data = http_get_json(url, params={
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    })

    candles = []
    for k in data:
        candles.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": int(k[6]),
        })
    return candles

def get_last_price(symbol: str):
    url = f"{MEXC_BASE_URL}/api/v3/ticker/price"
    data = http_get_json(url, params={"symbol": symbol})
    return float(data["price"])

# =========================================================
# USDT.D PROXY (CoinGecko)
# =========================================================
def cg_get_global_total_market_cap():
    url = f"{COINGECKO_BASE_URL}/global"
    data = http_get_json(url)
    total_mc = safe_float(data.get("data", {}).get("total_market_cap", {}).get("usd"))
    if total_mc is None or total_mc <= 0:
        raise RuntimeError("Global market cap alınamadı")
    return total_mc

def cg_get_tether_market_cap():
    url = f"{COINGECKO_BASE_URL}/simple/price"
    data = http_get_json(url, params={
        "ids": "tether",
        "vs_currencies": "usd",
        "include_market_cap": "true"
    })
    usdt_mc = safe_float(data.get("tether", {}).get("usd_market_cap"))
    if usdt_mc is None or usdt_mc <= 0:
        raise RuntimeError("Tether market cap alınamadı")
    return usdt_mc

def get_usdtd_proxy(state):
    manual = state.get("manual_usdtd_override")
    if manual is not None:
        return {
            "usdtd": float(manual),
            "source": "MANUAL_OVERRIDE",
            "ts": utc_ts()
        }

    cache = state.get("cg_cache", {})
    if cache and (utc_ts() - int(cache.get("ts", 0)) < CG_CACHE_TTL_SECONDS):
        return cache

    total_mc = cg_get_global_total_market_cap()
    usdt_mc = cg_get_tether_market_cap()
    usdtd = (usdt_mc / total_mc) * 100.0

    result = {
        "usdtd": usdtd,
        "source": "COINGECKO_LIVE",
        "ts": utc_ts()
    }
    state["cg_cache"] = result
    return result

# =========================================================
# INDICATORS
# =========================================================
def ema_series(values, period):
    if len(values) < period:
        return [None] * len(values)

    alpha = 2 / (period + 1)
    result = [None] * len(values)

    seed = sum(values[:period]) / period
    result[period - 1] = seed
    prev = seed

    for i in range(period, len(values)):
        prev = (values[i] - prev) * alpha + prev
        result[i] = prev

    return result

def rsi_series(values, period=14):
    if len(values) < period + 1:
        return [None] * len(values)

    gains = [0.0]
    losses = [0.0]

    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    result = [None] * len(values)

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))

    for i in range(period + 1, len(values)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))

    return result

def macd_series(values, fast=12, slow=26, signal=9):
    ema_fast = ema_series(values, fast)
    ema_slow = ema_series(values, slow)

    macd_line = [None] * len(values)
    for i in range(len(values)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]

    valid = [x for x in macd_line if x is not None]
    if len(valid) < signal:
        return macd_line, [None] * len(values), [None] * len(values)

    signal_valid = ema_series(valid, signal)
    signal_line = [None] * len(values)

    vi = 0
    for i in range(len(values)):
        if macd_line[i] is not None:
            signal_line[i] = signal_valid[vi]
            vi += 1

    hist = [None] * len(values)
    for i in range(len(values)):
        if macd_line[i] is not None and signal_line[i] is not None:
            hist[i] = macd_line[i] - signal_line[i]

    return macd_line, signal_line, hist

def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period

def highest_high(candles, period):
    if len(candles) < period:
        return None
    return max(x["high"] for x in candles[-period:])

def lowest_low(candles, period):
    if len(candles) < period:
        return None
    return min(x["low"] for x in candles[-period:])

def analyze_timeframe(candles):
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]
    opens = [c["open"] for c in candles]

    ema9 = ema_series(closes, 9)
    ema20 = ema_series(closes, 20)
    ema21 = ema_series(closes, 21)
    ema50 = ema_series(closes, 50)
    ema200 = ema_series(closes, 200)

    rsi14 = rsi_series(closes, 14)
    macd_line, macd_signal, macd_hist = macd_series(closes, 12, 26, 9)
    atr14 = atr(candles, 14)

    volume_avg_20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else None

    hh3 = highs[-1] > highs[-2] > highs[-3] if len(highs) >= 3 else False
    hl3 = lows[-1] > lows[-2] > lows[-3] if len(lows) >= 3 else False
    lh3 = highs[-1] < highs[-2] < highs[-3] if len(highs) >= 3 else False
    ll3 = lows[-1] < lows[-2] < lows[-3] if len(lows) >= 3 else False

    close_up_3 = closes[-1] > closes[-2] > closes[-3] if len(closes) >= 3 else False
    close_down_3 = closes[-1] < closes[-2] < closes[-3] if len(closes) >= 3 else False

    return {
        "close": closes[-1],
        "open": opens[-1],
        "high": highs[-1],
        "low": lows[-1],
        "volume": volumes[-1],
        "volume_avg_20": volume_avg_20,
        "ema9": ema9[-1],
        "ema20": ema20[-1],
        "ema21": ema21[-1],
        "ema50": ema50[-1],
        "ema200": ema200[-1],
        "rsi14": rsi14[-1],
        "macd": macd_line[-1],
        "macd_signal": macd_signal[-1],
        "macd_hist": macd_hist[-1],
        "atr14": atr14,
        "hh3": hh3,
        "hl3": hl3,
        "lh3": lh3,
        "ll3": ll3,
        "close_up_3": close_up_3,
        "close_down_3": close_down_3,
        "highest_20": highest_high(candles, 20),
        "lowest_20": lowest_low(candles, 20),
    }

# =========================================================
# STATE
# =========================================================
def default_stats():
    return {
        "total_signals": 0,
        "wins": 0,
        "losses": 0,
        "tp1_hits": 0,
        "tp2_hits": 0,
        "tp3_hits": 0,
        "stops": 0,
        "confirm_failures": 0,
        "usdtd_veto_blocks": 0
    }

def default_state():
    return {
        "last_signal": None,
        "active_signal": None,
        "signal_history": [],
        "cg_cache": {},
        "history": {"usdtd": []},
        "manual_usdtd_override": None,
        "last_update_id": 0,
        "stats": default_stats()
    }

def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return default_state()

            data.setdefault("last_signal", None)
            data.setdefault("active_signal", None)
            data.setdefault("signal_history", [])
            data.setdefault("cg_cache", {})
            data.setdefault("manual_usdtd_override", None)
            data.setdefault("history", {"usdtd": []})
            data["history"].setdefault("usdtd", [])
            data.setdefault("last_update_id", 0)
            data.setdefault("stats", default_stats())

            for k, v in default_stats().items():
                data["stats"].setdefault(k, v)

            return data
    except Exception as e:
        log(f"State load exception: {e}")
        return default_state()

def save_state(state):
    tmp_file = STATE_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, STATE_FILE)

# =========================================================
# USDTD HISTORY
# =========================================================
def append_usdtd_history(state, value):
    hist = state["history"].get("usdtd", [])
    hist.append({"ts": utc_ts(), "value": float(value)})
    state["history"]["usdtd"] = hist[-USDTD_HISTORY_LIMIT:]

def get_usdtd_values(state):
    values = []
    for x in state.get("history", {}).get("usdtd", []):
        val = safe_float(x.get("value"))
        if val is not None:
            values.append(val)
    return values

def get_usdtd_bias(state):
    if not USE_USDTD_FILTER:
        return {
            "bias": "NEUTRAL",
            "strength": "NONE",
            "reason": "USDTD_DISABLED",
            "now": None,
            "fast": None,
            "slow": None,
            "fast_slow_diff_pct": 0.0,
            "now_fast_diff_pct": 0.0
        }

    try:
        info = get_usdtd_proxy(state)
        usdtd_now = float(info["usdtd"])
        append_usdtd_history(state, usdtd_now)
        values = get_usdtd_values(state)

        if len(values) < USDTD_SLOW_EMA:
            return {
                "bias": "NEUTRAL",
                "strength": "WAIT",
                "reason": f"WAIT_HISTORY:{len(values)}",
                "now": usdtd_now,
                "fast": None,
                "slow": None,
                "fast_slow_diff_pct": 0.0,
                "now_fast_diff_pct": 0.0
            }

        fast_series = ema_series(values, USDTD_FAST_EMA)
        slow_series = ema_series(values, USDTD_SLOW_EMA)

        usdtd_fast = fast_series[-1]
        usdtd_slow = slow_series[-1]

        if usdtd_fast is None or usdtd_slow is None or usdtd_fast == 0:
            return {
                "bias": "NEUTRAL",
                "strength": "WAIT",
                "reason": "EMA_NOT_READY",
                "now": usdtd_now,
                "fast": usdtd_fast,
                "slow": usdtd_slow,
                "fast_slow_diff_pct": 0.0,
                "now_fast_diff_pct": 0.0
            }

        fast_slow_diff_pct = abs(usdtd_fast - usdtd_slow) / usdtd_fast * 100.0
        now_fast_diff_pct = abs(usdtd_now - usdtd_fast) / usdtd_fast * 100.0

        # USDT.D yükseliyorsa risk-off => SHORT rejim
        if usdtd_fast > usdtd_slow and usdtd_now >= usdtd_fast:
            if fast_slow_diff_pct >= USDTD_STRONG_DIFF_PCT and now_fast_diff_pct >= USDTD_STRONG_NOW_FAST_PCT:
                return {
                    "bias": "SHORT",
                    "strength": "STRONG",
                    "reason": f"STRONG_RISK_OFF:{round(usdtd_now, 3)}",
                    "now": usdtd_now,
                    "fast": usdtd_fast,
                    "slow": usdtd_slow,
                    "fast_slow_diff_pct": fast_slow_diff_pct,
                    "now_fast_diff_pct": now_fast_diff_pct
                }
            return {
                "bias": "SHORT",
                "strength": "NORMAL",
                "reason": f"RISK_OFF:{round(usdtd_now, 3)}",
                "now": usdtd_now,
                "fast": usdtd_fast,
                "slow": usdtd_slow,
                "fast_slow_diff_pct": fast_slow_diff_pct,
                "now_fast_diff_pct": now_fast_diff_pct
            }

        # USDT.D düşüyorsa risk-on => LONG rejim
        if usdtd_fast < usdtd_slow and usdtd_now <= usdtd_fast:
            if fast_slow_diff_pct >= USDTD_STRONG_DIFF_PCT and now_fast_diff_pct >= USDTD_STRONG_NOW_FAST_PCT:
                return {
                    "bias": "LONG",
                    "strength": "STRONG",
                    "reason": f"STRONG_RISK_ON:{round(usdtd_now, 3)}",
                    "now": usdtd_now,
                    "fast": usdtd_fast,
                    "slow": usdtd_slow,
                    "fast_slow_diff_pct": fast_slow_diff_pct,
                    "now_fast_diff_pct": now_fast_diff_pct
                }
            return {
                "bias": "LONG",
                "strength": "NORMAL",
                "reason": f"RISK_ON:{round(usdtd_now, 3)}",
                "now": usdtd_now,
                "fast": usdtd_fast,
                "slow": usdtd_slow,
                "fast_slow_diff_pct": fast_slow_diff_pct,
                "now_fast_diff_pct": now_fast_diff_pct
            }

        # hafif eğilim var ama güçlü değil
        if usdtd_fast > usdtd_slow and fast_slow_diff_pct >= USDTD_WEAK_DIFF_PCT:
            return {
                "bias": "SHORT",
                "strength": "WEAK",
                "reason": f"WEAK_RISK_OFF:{round(usdtd_now, 3)}",
                "now": usdtd_now,
                "fast": usdtd_fast,
                "slow": usdtd_slow,
                "fast_slow_diff_pct": fast_slow_diff_pct,
                "now_fast_diff_pct": now_fast_diff_pct
            }

        if usdtd_fast < usdtd_slow and fast_slow_diff_pct >= USDTD_WEAK_DIFF_PCT:
            return {
                "bias": "LONG",
                "strength": "WEAK",
                "reason": f"WEAK_RISK_ON:{round(usdtd_now, 3)}",
                "now": usdtd_now,
                "fast": usdtd_fast,
                "slow": usdtd_slow,
                "fast_slow_diff_pct": fast_slow_diff_pct,
                "now_fast_diff_pct": now_fast_diff_pct
            }

        return {
            "bias": "NEUTRAL",
            "strength": "NEUTRAL",
            "reason": f"NEUTRAL:{round(usdtd_now, 3)}",
            "now": usdtd_now,
            "fast": usdtd_fast,
            "slow": usdtd_slow,
            "fast_slow_diff_pct": fast_slow_diff_pct,
            "now_fast_diff_pct": now_fast_diff_pct
        }
    except Exception as e:
        return {
            "bias": "NEUTRAL",
            "strength": "FAIL",
            "reason": f"FAIL_OPEN:{e}",
            "now": None,
            "fast": None,
            "slow": None,
            "fast_slow_diff_pct": 0.0,
            "now_fast_diff_pct": 0.0
        }

def get_usdtd_regime_label(info):
    bias = info.get("bias", "NEUTRAL")
    strength = info.get("strength", "NEUTRAL")

    if bias == "LONG" and strength == "STRONG":
        return "STRONG_LONG"
    if bias == "LONG" and strength == "NORMAL":
        return "LONG"
    if bias == "LONG" and strength == "WEAK":
        return "WEAK_LONG"
    if bias == "SHORT" and strength == "STRONG":
        return "STRONG_SHORT"
    if bias == "SHORT" and strength == "NORMAL":
        return "SHORT"
    if bias == "SHORT" and strength == "WEAK":
        return "WEAK_SHORT"
    return "NEUTRAL"

def usdtd_veto_long(info):
    if not USDTD_HARD_VETO_ENABLED:
        return False
    return info.get("bias") == "SHORT" and info.get("strength") == "STRONG"

def usdtd_veto_short(info):
    if not USDTD_HARD_VETO_ENABLED:
        return False
    return info.get("bias") == "LONG" and info.get("strength") == "STRONG"

def usdtd_long_score_adjustment(info):
    regime = get_usdtd_regime_label(info)
    if regime == "STRONG_LONG":
        return 3.4, "USDTD_STRONG_LONG"
    if regime == "LONG":
        return 2.1, "USDTD_LONG"
    if regime == "WEAK_LONG":
        return 0.9, "USDTD_WEAK_LONG"
    if regime == "WEAK_SHORT":
        return -1.4, "USDTD_WEAK_HEADWIND"
    if regime == "SHORT":
        return -3.0, "USDTD_SHORT_HEADWIND"
    if regime == "STRONG_SHORT":
        return -99.0, "USDTD_STRONG_SHORT_VETO"
    return 0.0, "USDTD_NEUTRAL"

def usdtd_short_score_adjustment(info):
    regime = get_usdtd_regime_label(info)
    if regime == "STRONG_SHORT":
        return 3.4, "USDTD_STRONG_SHORT"
    if regime == "SHORT":
        return 2.1, "USDTD_SHORT"
    if regime == "WEAK_SHORT":
        return 0.9, "USDTD_WEAK_SHORT"
    if regime == "WEAK_LONG":
        return -1.4, "USDTD_WEAK_HEADWIND"
    if regime == "LONG":
        return -3.0, "USDTD_LONG_HEADWIND"
    if regime == "STRONG_LONG":
        return -99.0, "USDTD_STRONG_LONG_VETO"
    return 0.0, "USDTD_NEUTRAL"

# =========================================================
# SIGNAL HELPERS
# =========================================================
def classify_signal_strength(score):
    s = safe_float(score, 0.0)
    if s >= 15.0:
        return "GUCLU"
    elif s >= 12.0:
        return "ORTA"
    return "ZAYIF"

def build_signal_signature(sig):
    if not sig:
        return ""
    return (
        f"{sig.get('direction','')}"
        f"|{round(safe_float(sig.get('entry'), 0.0), 2)}"
        f"|{round(safe_float(sig.get('score'), 0.0), 2)}"
        f"|{sig.get('reason','')}"
    )

def build_signal_payload(direction, entry, sl, tp1, tp2, tp3, score, reason, extra=None):
    payload = {
        "direction": direction.upper(),
        "entry": round(float(entry), 4),
        "sl": round(float(sl), 4),
        "tp1": round(float(tp1), 4),
        "tp2": round(float(tp2), 4),
        "tp3": round(float(tp3), 4),
        "score": round(float(score), 2),
        "reason": reason,
        "created_ts": utc_ts(),
        "updated_ts": utc_ts(),
        "status": "PENDING",
        "active": False,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "breakeven_done": False,
        "trail_active": False,
        "trail_stop": round(float(sl), 4),
        "signal_signature": "",
        "confirmed_15m": False,
        "confirm_check_due_ts": None,
        "confirm_failed": False,
        "extra": extra or {}
    }
    payload["signal_signature"] = build_signal_signature(payload)
    return payload

def format_signal_message(sig):
    direction = sig["direction"].upper()
    emoji = "🟢" if direction == "LONG" else "🔴"
    strength = classify_signal_strength(sig["score"])
    extra = sig.get("extra", {})
    btc_bias = extra.get("btc_bias", "-")
    usdtd_bias = extra.get("usdtd_bias", "-")
    usdtd_regime = extra.get("usdtd_regime", "-")
    risk = abs(sig["entry"] - sig["sl"])
    reward = abs(sig["tp2"] - sig["entry"])
    rr = reward / risk if risk > 0 else 0

    phase = "ERKEN GIRIS / 15M TEYIT BEKLIYOR"

    return (
        f"{emoji} ETHUSDT {direction}\n\n"
        f"Güç: {strength}\n"
        f"Skor: {sig['score']}\n"
        f"Durum: {phase}\n"
        f"BTC: {btc_bias}\n"
        f"USDT.D: {usdtd_bias}\n"
        f"USDT.D Rejim: {usdtd_regime}\n\n"
        f"Entry: {fmt_price(sig['entry'])}\n"
        f"SL: {fmt_price(sig['sl'])}\n"
        f"TP1: {fmt_price(sig['tp1'])}\n"
        f"TP2: {fmt_price(sig['tp2'])}\n"
        f"TP3: {fmt_price(sig['tp3'])}\n\n"
        f"RR: {rr:.2f}\n"
        f"Reason: {sig['reason']}"
    )

def format_upgraded_signal_message(old_sig, new_sig, upgrade_reason):
    direction = new_sig["direction"].upper()
    return (
        f"🔄 ETHUSDT {direction} UPDATE\n\n"
        f"Skor: {old_sig['score']} -> {new_sig['score']}\n"
        f"Neden: {upgrade_reason}\n\n"
        f"Entry: {fmt_price(new_sig['entry'])}\n"
        f"SL: {fmt_price(new_sig['sl'])}\n"
        f"TP1: {fmt_price(new_sig['tp1'])}\n"
        f"TP2: {fmt_price(new_sig['tp2'])}\n"
        f"TP3: {fmt_price(new_sig['tp3'])}"
    )

def format_close_message(sig, close_reason):
    if close_reason == "TP3_HIT":
        return "🏁 TP3 HIT\n\nTrade tamamlandı"
    if close_reason == "SL_HIT":
        return "❌ STOP\n\nTrade kapandı"
    if close_reason == "TIMEOUT":
        return "⌛ TIMEOUT\n\nSinyal süresi doldu"
    if close_reason == "CANCELLED_BEFORE_ENTRY_TP1":
        return "⚠️ SETUP IPTAL\n\nEntry gelmeden TP1 oldu"
    if close_reason == "REVERSED_BY_STRONGER_SIGNAL":
        return "↩️ TERS SİNYAL\n\nDaha güçlü ters sinyal geldi"
    if close_reason == "CONFIRM_FAIL_15M":
        return "⚠️ 15M TEYIT GELMEDI\n\nTrade fake sayıldı, işlem kapandı"
    return "Trade kapandı"

def format_active_signal(sig):
    if not sig:
        return "Aktif işlem yok."

    risk = abs(sig["entry"] - sig["sl"])
    reward = abs(sig["tp2"] - sig["entry"])
    rr = reward / risk if risk > 0 else 0.0
    filled = "EVET" if sig.get("active") else "BEKLIYOR"
    confirm = "EVET" if sig.get("confirmed_15m") else "BEKLIYOR"
    usdtd_regime = sig.get("extra", {}).get("usdtd_regime", "-")

    return (
        f"📌 AKTIF ISLEM\n\n"
        f"Yön: {sig['direction']}\n"
        f"Durum: {filled}\n"
        f"15M Teyit: {confirm}\n"
        f"USDT.D Rejim: {usdtd_regime}\n"
        f"Güç: {classify_signal_strength(sig.get('score'))}\n\n"
        f"Entry: {fmt_price(sig['entry'])}\n"
        f"SL: {fmt_price(sig['sl'])}\n"
        f"TP1: {fmt_price(sig['tp1'])}\n"
        f"TP2: {fmt_price(sig['tp2'])}\n"
        f"TP3: {fmt_price(sig['tp3'])}\n\n"
        f"RR: {rr:.2f}"
    )

def format_panel(state):
    stats = state.get("stats", default_stats())
    total = int(stats.get("total_signals", 0))
    wins = int(stats.get("wins", 0))
    losses = int(stats.get("losses", 0))
    win_rate = (wins / total * 100.0) if total > 0 else 0.0

    return (
        f"📊 BOT PANEL\n\n"
        f"Toplam Sinyal: {total}\n"
        f"Kazanan: {wins}\n"
        f"Kaybeden: {losses}\n"
        f"15M Fail: {int(stats.get('confirm_failures', 0))}\n"
        f"USDT.D Veto: {int(stats.get('usdtd_veto_blocks', 0))}\n"
        f"Win Rate: %{win_rate:.1f}\n\n"
        f"TP1 Hit: {int(stats.get('tp1_hits', 0))}\n"
        f"TP2 Hit: {int(stats.get('tp2_hits', 0))}\n"
        f"TP3 Hit: {int(stats.get('tp3_hits', 0))}\n"
        f"STOP: {int(stats.get('stops', 0))}"
    )

def aggregate_period_summary(state, period="daily"):
    history = state.get("signal_history", [])
    now = utc_ts()

    if period == "daily":
        key = day_key_from_ts(now)
        title = "📅 GUNLUK OZET"
        match_fn = lambda ts: day_key_from_ts(ts) == key
    else:
        key = week_key_from_ts(now)
        title = "📈 HAFTALIK OZET"
        match_fn = lambda ts: week_key_from_ts(ts) == key

    total = 0
    wins = 0
    losses = 0
    tp1 = 0
    tp2 = 0
    tp3 = 0
    stops = 0
    confirm_fail = 0

    for sig in history:
        closed_ts = sig.get("closed_ts")
        if not closed_ts:
            continue
        if not match_fn(int(closed_ts)):
            continue

        total += 1
        if sig.get("tp1_hit"):
            tp1 += 1
        if sig.get("tp2_hit"):
            tp2 += 1
        if sig.get("tp3_hit"):
            tp3 += 1

        reason = sig.get("close_reason")
        if reason == "TP3_HIT":
            wins += 1
        elif reason == "SL_HIT":
            losses += 1
            stops += 1
        elif reason == "CONFIRM_FAIL_15M":
            confirm_fail += 1

    win_rate = (wins / total * 100.0) if total > 0 else 0.0

    return (
        f"{title}\n\n"
        f"Toplam Sinyal: {total}\n"
        f"Kazanan: {wins}\n"
        f"Kaybeden: {losses}\n"
        f"15M Fail: {confirm_fail}\n"
        f"Win Rate: %{win_rate:.1f}\n\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n"
        f"TP3: {tp3}\n"
        f"STOP: {stops}"
    )

def help_text():
    return (
        "🤖 KOMUTLAR\n\n"
        "/panel\n"
        "/gunluk\n"
        "/haftalik\n"
        "/aktif\n"
        "/yardim\n"
        "/usdtd\n"
        "/fiyat"
    )

# =========================================================
# STATS
# =========================================================
def update_stats_for_closed_signal(state, sig):
    stats = state.setdefault("stats", default_stats())
    stats["total_signals"] = int(stats.get("total_signals", 0)) + 1

    if sig.get("tp1_hit"):
        stats["tp1_hits"] = int(stats.get("tp1_hits", 0)) + 1
    if sig.get("tp2_hit"):
        stats["tp2_hits"] = int(stats.get("tp2_hits", 0)) + 1
    if sig.get("tp3_hit"):
        stats["tp3_hits"] = int(stats.get("tp3_hits", 0)) + 1

    reason = sig.get("close_reason")
    if reason == "TP3_HIT":
        stats["wins"] = int(stats.get("wins", 0)) + 1
    elif reason == "SL_HIT":
        stats["losses"] = int(stats.get("losses", 0)) + 1
        stats["stops"] = int(stats.get("stops", 0)) + 1
    elif reason == "CONFIRM_FAIL_15M":
        stats["confirm_failures"] = int(stats.get("confirm_failures", 0)) + 1

# =========================================================
# SIGNAL HELPERS
# =========================================================
def is_same_direction(a, b):
    return bool(a and b and a.get("direction") == b.get("direction"))

def is_opposite_direction(a, b):
    return bool(a and b and a.get("direction") != b.get("direction"))

def entry_distance_pct(sig_a, sig_b):
    if not sig_a or not sig_b:
        return 999.0
    return pct_diff(sig_a.get("entry"), sig_b.get("entry"))

def is_tp1_hit(signal, current_price):
    if not signal:
        return False
    tp1 = safe_float(signal.get("tp1"))
    cp = safe_float(current_price)
    if tp1 is None or cp is None:
        return False
    return cp >= tp1 if signal["direction"] == "LONG" else cp <= tp1

def is_tp2_hit(signal, current_price):
    if not signal:
        return False
    tp2 = safe_float(signal.get("tp2"))
    cp = safe_float(current_price)
    if tp2 is None or cp is None:
        return False
    return cp >= tp2 if signal["direction"] == "LONG" else cp <= tp2

def is_tp3_hit(signal, current_price):
    if not signal:
        return False
    tp3 = safe_float(signal.get("tp3"))
    cp = safe_float(current_price)
    if tp3 is None or cp is None:
        return False
    return cp >= tp3 if signal["direction"] == "LONG" else cp <= tp3

def is_sl_hit(signal, current_price):
    if not signal:
        return False
    sl = safe_float(signal.get("sl"))
    cp = safe_float(current_price)
    if sl is None or cp is None:
        return False
    return cp <= sl if signal["direction"] == "LONG" else cp >= sl

def is_entry_filled(signal, current_price):
    if not signal:
        return False
    entry = safe_float(signal.get("entry"))
    cp = safe_float(current_price)
    if entry is None or cp is None or entry == 0:
        return False
    return (abs(cp - entry) / entry * 100.0) <= ENTRY_FILL_TOLERANCE_PCT

def is_expired(signal):
    if not signal:
        return True
    return minutes_since(signal.get("created_ts")) >= SIGNAL_TIMEOUT_MINUTES

def update_trailing_stop(signal, current_price, atr_now):
    if not signal or not signal.get("trail_active"):
        return False
    if atr_now is None or atr_now <= 0:
        return False

    cp = float(current_price)
    trail = float(signal.get("trail_stop", signal["sl"]))

    if signal["direction"] == "LONG":
        new_trail = cp - (atr_now * TRAIL_AFTER_TP2_ATR)
        if new_trail > trail:
            signal["trail_stop"] = round(new_trail, 4)
            signal["sl"] = round(max(float(signal["sl"]), new_trail), 4)
            return True
    else:
        new_trail = cp + (atr_now * TRAIL_AFTER_TP2_ATR)
        if new_trail < trail:
            signal["trail_stop"] = round(new_trail, 4)
            signal["sl"] = round(min(float(signal["sl"]), new_trail), 4)
            return True

    return False

# =========================================================
# ACTIVE SIGNAL MGMT
# =========================================================
def close_active_signal(state, close_reason, current_price=None, notify_telegram=True):
    active = state.get("active_signal")
    if not active:
        return

    active["status"] = "CLOSED"
    active["close_reason"] = close_reason
    active["closed_ts"] = utc_ts()

    if current_price is not None:
        active["close_price"] = round(float(current_price), 4)

    if notify_telegram:
        tg_send(format_close_message(active, close_reason))

    state["signal_history"].append(active)
    update_stats_for_closed_signal(state, active)
    state["active_signal"] = None
    log(f"Aktif sinyal kapatildi: {close_reason}")

def long_confirm_15m_ok(tf15):
    return (
        tf15["ema9"] is not None and tf15["ema21"] is not None and tf15["ema50"] is not None and
        tf15["close"] >= tf15["ema21"] and
        tf15["ema9"] >= tf15["ema21"] >= tf15["ema50"] and
        tf15["rsi14"] is not None and tf15["rsi14"] >= 50 and
        tf15["macd_hist"] is not None and tf15["macd_hist"] >= -0.02
    )

def short_confirm_15m_ok(tf15):
    return (
        tf15["ema9"] is not None and tf15["ema21"] is not None and tf15["ema50"] is not None and
        tf15["close"] <= tf15["ema21"] and
        tf15["ema9"] <= tf15["ema21"] <= tf15["ema50"] and
        tf15["rsi14"] is not None and tf15["rsi14"] <= 50 and
        tf15["macd_hist"] is not None and tf15["macd_hist"] <= 0.02
    )

def refresh_active_signal_if_needed(state, current_price, atr_now=None):
    active = state.get("active_signal")
    if not active:
        return

    if minutes_since(active.get("created_ts")) >= MAX_ACTIVE_SIGNAL_AGE_MINUTES:
        close_active_signal(state, "TIMEOUT", current_price, notify_telegram=True)
        return

    if not active.get("active"):
        if CANCEL_IF_TP1_BEFORE_ENTRY and is_tp1_hit(active, current_price):
            close_active_signal(state, "CANCELLED_BEFORE_ENTRY_TP1", current_price, notify_telegram=True)
            return

        if is_entry_filled(active, current_price):
            active["active"] = True
            active["status"] = "OPEN"
            active["updated_ts"] = utc_ts()
            active["confirm_check_due_ts"] = utc_ts() + int(POST_ENTRY_CONFIRM_WINDOW_MINUTES * 60)

            tg_send(
                f"✅ ENTRY FILLED\n\n"
                f"{active['direction']} @ {fmt_price(active['entry'])}\n"
                f"15M teyit bekleniyor"
            )
            return

        if is_expired(active):
            close_active_signal(state, "TIMEOUT", current_price, notify_telegram=True)
            return

        return

    if not active.get("confirmed_15m"):
        due_ts = int(active.get("confirm_check_due_ts") or 0)
        if due_ts and utc_ts() >= due_ts:
            try:
                tf15 = analyze_timeframe(get_klines(SYMBOL, "15m", 220))
                ok = long_confirm_15m_ok(tf15) if active["direction"] == "LONG" else short_confirm_15m_ok(tf15)

                if ok:
                    active["confirmed_15m"] = True
                    active["updated_ts"] = utc_ts()
                    tg_send(
                        f"✅ 15M TEYIT GELDI\n\n"
                        f"{active['direction']} devam ediyor"
                    )
                else:
                    active["confirm_failed"] = True
                    active["updated_ts"] = utc_ts()

                    if FAIL_CONFIRM_CLOSE_SIGNAL and not SOFT_CONFIRM_MODE:
                        close_active_signal(state, "CONFIRM_FAIL_15M", current_price, notify_telegram=True)
                        return
                    else:
                        active["sl"] = round(float(active["entry"]), 4)
                        active["trail_stop"] = round(float(active["entry"]), 4)
                        tg_send(
                            f"⚠️ 15M TEYIT GELMEDI\n\n"
                            f"{active['direction']} zayıfladı, SL entry'e çekildi"
                        )
            except Exception as e:
                log(f"15m confirm exception: {e}")

    if is_sl_hit(active, current_price):
        close_active_signal(state, "SL_HIT", current_price, notify_telegram=True)
        return

    if not active.get("tp1_hit") and is_tp1_hit(active, current_price):
        active["tp1_hit"] = True
        if not active.get("breakeven_done"):
            active["sl"] = round(float(active["entry"]), 4)
            active["trail_stop"] = round(float(active["entry"]), 4)
            active["breakeven_done"] = True
            active["updated_ts"] = utc_ts()
            tg_send("🎯 TP1 HIT\n\nBE aktif")

    if not active.get("tp2_hit") and is_tp2_hit(active, current_price):
        active["tp2_hit"] = True
        active["trail_active"] = True
        if atr_now is not None and atr_now > 0:
            update_trailing_stop(active, current_price, atr_now)
        active["updated_ts"] = utc_ts()
        tg_send("🚀 TP2 HIT\n\nTrailing aktif")

    if active.get("trail_active"):
        changed = update_trailing_stop(active, current_price, atr_now)
        if changed:
            active["updated_ts"] = utc_ts()

    if is_sl_hit(active, current_price):
        close_active_signal(state, "SL_HIT", current_price, notify_telegram=True)
        return

    if not active.get("tp3_hit") and is_tp3_hit(active, current_price):
        active["tp3_hit"] = True
        close_active_signal(state, "TP3_HIT", current_price, notify_telegram=True)
        return

    if is_expired(active):
        close_active_signal(state, "TIMEOUT", current_price, notify_telegram=True)
        return

# =========================================================
# BTC / TREND BIAS
# =========================================================
def get_btc_bias():
    try:
        btc_15m = analyze_timeframe(get_klines(BTC_SYMBOL, "15m", 220))
        btc_1h = analyze_timeframe(get_klines(BTC_SYMBOL, "1h", 220))

        bull = (
            btc_15m["ema20"] is not None and btc_15m["ema50"] is not None and
            btc_15m["close"] > btc_15m["ema20"] >= btc_15m["ema50"] and
            btc_15m["rsi14"] is not None and btc_15m["rsi14"] >= 52 and
            btc_1h["ema20"] is not None and btc_1h["close"] > btc_1h["ema20"]
        )

        bear = (
            btc_15m["ema20"] is not None and btc_15m["ema50"] is not None and
            btc_15m["close"] < btc_15m["ema20"] <= btc_15m["ema50"] and
            btc_15m["rsi14"] is not None and btc_15m["rsi14"] <= 48 and
            btc_1h["ema20"] is not None and btc_1h["close"] < btc_1h["ema20"]
        )

        if bull and not bear:
            return "LONG"
        if bear and not bull:
            return "SHORT"
        return "NEUTRAL"
    except Exception as e:
        log(f"BTC bias exception: {e}")
        return "NEUTRAL"

# =========================================================
# SCORE HELPERS
# =========================================================
def score_long_tf(tf, name):
    score = 0.0
    reasons = []

    if tf["ema20"] is not None and tf["close"] > tf["ema20"]:
        score += 1.0
        reasons.append(f"{name}_EMA20")

    if tf["ema20"] is not None and tf["ema50"] is not None and tf["ema20"] >= tf["ema50"]:
        score += 1.0
        reasons.append(f"{name}_STACK")

    if tf["rsi14"] is not None:
        if tf["rsi14"] >= 58:
            score += 1.0
            reasons.append(f"{name}_RSI58")
        elif tf["rsi14"] >= 52:
            score += 0.5
            reasons.append(f"{name}_RSI52")

    if tf["macd_hist"] is not None:
        if tf["macd_hist"] > 0:
            score += 0.8
            reasons.append(f"{name}_MACD+")
        elif tf["macd_hist"] > -0.01:
            score += 0.3
            reasons.append(f"{name}_MACD_SOFT")

    if tf["close_up_3"]:
        score += 0.5
        reasons.append(f"{name}_CLOSE3")
    if tf["hl3"]:
        score += 0.5
        reasons.append(f"{name}_HL3")

    return score, reasons

def score_short_tf(tf, name):
    score = 0.0
    reasons = []

    if tf["ema20"] is not None and tf["close"] < tf["ema20"]:
        score += 1.0
        reasons.append(f"{name}_EMA20")

    if tf["ema20"] is not None and tf["ema50"] is not None and tf["ema20"] <= tf["ema50"]:
        score += 1.0
        reasons.append(f"{name}_STACK")

    if tf["rsi14"] is not None:
        if tf["rsi14"] <= 42:
            score += 1.0
            reasons.append(f"{name}_RSI42")
        elif tf["rsi14"] <= 48:
            score += 0.5
            reasons.append(f"{name}_RSI48")

    if tf["macd_hist"] is not None:
        if tf["macd_hist"] < 0:
            score += 0.8
            reasons.append(f"{name}_MACD-")
        elif tf["macd_hist"] < 0.01:
            score += 0.3
            reasons.append(f"{name}_MACD_SOFT")

    if tf["close_down_3"]:
        score += 0.5
        reasons.append(f"{name}_CLOSE3")
    if tf["ll3"]:
        score += 0.5
        reasons.append(f"{name}_LL3")

    return score, reasons

def long_trigger_5m_ok(tf5):
    return (
        tf5["ema9"] is not None and tf5["ema21"] is not None and
        tf5["close"] >= tf5["ema9"] >= tf5["ema21"] and
        tf5["rsi14"] is not None and tf5["rsi14"] >= 52 and
        tf5["macd_hist"] is not None and tf5["macd_hist"] >= 0 and
        (tf5["close_up_3"] or tf5["hl3"])
    )

def short_trigger_5m_ok(tf5):
    return (
        tf5["ema9"] is not None and tf5["ema21"] is not None and
        tf5["close"] <= tf5["ema9"] <= tf5["ema21"] and
        tf5["rsi14"] is not None and tf5["rsi14"] <= 48 and
        tf5["macd_hist"] is not None and tf5["macd_hist"] <= 0 and
        (tf5["close_down_3"] or tf5["ll3"])
    )

# =========================================================
# BUILD SIGNAL
# =========================================================
def build_trade_signal(state):
    try:
        candles_5m = get_klines(SYMBOL, "5m", 220)
        candles_15m = get_klines(SYMBOL, "15m", 220)
        candles_30m = get_klines(SYMBOL, "30m", 220)
        candles_1h = get_klines(SYMBOL, "1h", 220)
        candles_2h = get_klines(SYMBOL, "2h", 220)
        candles_4h = get_klines(SYMBOL, "4h", 220)

        tf5 = analyze_timeframe(candles_5m)
        tf15 = analyze_timeframe(candles_15m)
        tf30 = analyze_timeframe(candles_30m)
        tf1h = analyze_timeframe(candles_1h)
        tf2h = analyze_timeframe(candles_2h)
        tf4h = analyze_timeframe(candles_4h)

        current_price = tf5["close"]
        atr_now = tf5["atr14"]
        if atr_now is None or atr_now <= 0:
            return None, current_price, {"reason": "ATR_NOT_READY"}, None

        btc_bias = get_btc_bias()
        usdtd_info = get_usdtd_bias(state)
        usdtd_bias = usdtd_info["bias"]
        usdtd_strength = usdtd_info["strength"]
        usdtd_regime = get_usdtd_regime_label(usdtd_info)

        allow_long = True
        allow_short = True
        long_score = 0.0
        short_score = 0.0
        reasons_long = []
        reasons_short = []

        # 4H hard lock
        if REQUIRE_4H_TREND_LOCK:
            long_4h_ok = (
                tf4h["ema20"] is not None and tf4h["ema50"] is not None and
                tf4h["close"] > tf4h["ema20"] and
                tf4h["ema20"] >= tf4h["ema50"] and
                tf4h["rsi14"] is not None and tf4h["rsi14"] >= 48
            )
            short_4h_ok = (
                tf4h["ema20"] is not None and tf4h["ema50"] is not None and
                tf4h["close"] < tf4h["ema20"] and
                tf4h["ema20"] <= tf4h["ema50"] and
                tf4h["rsi14"] is not None and tf4h["rsi14"] <= 52
            )
            if not long_4h_ok:
                allow_long = False
            if not short_4h_ok:
                allow_short = False

        # BTC confirmation
        if REQUIRE_BTC_CONFIRMATION:
            if btc_bias == "SHORT":
                allow_long = False
                reasons_long.append("BTC_VETO")
            elif btc_bias == "LONG":
                allow_short = False
                reasons_short.append("BTC_VETO")

        # 5m trigger must
        if allow_long and long_trigger_5m_ok(tf5):
            long_score += 3.0
            reasons_long.append("5M_TRIGGER")
        else:
            allow_long = False

        if allow_short and short_trigger_5m_ok(tf5):
            short_score += 3.0
            reasons_short.append("5M_TRIGGER")
        else:
            allow_short = False

        # USDT.D merkez veto
        if allow_long and usdtd_veto_long(usdtd_info):
            allow_long = False
            state["stats"]["usdtd_veto_blocks"] = int(state["stats"].get("usdtd_veto_blocks", 0)) + 1
            reasons_long.append("USDTD_HARD_VETO")

        if allow_short and usdtd_veto_short(usdtd_info):
            allow_short = False
            state["stats"]["usdtd_veto_blocks"] = int(state["stats"].get("usdtd_veto_blocks", 0)) + 1
            reasons_short.append("USDTD_HARD_VETO")

        # 15m
        if allow_long:
            s, r = score_long_tf(tf15, "15M")
            long_score += s * 0.85
            reasons_long.extend(r)

        if allow_short:
            s, r = score_short_tf(tf15, "15M")
            short_score += s * 0.85
            reasons_short.extend(r)

        # 30m
        if allow_long:
            s, r = score_long_tf(tf30, "30M")
            long_score += s * 0.70
            reasons_long.extend(r)

        if allow_short:
            s, r = score_short_tf(tf30, "30M")
            short_score += s * 0.70
            reasons_short.extend(r)

        # 1h
        if allow_long:
            s, r = score_long_tf(tf1h, "1H")
            long_score += s * 1.00
            reasons_long.extend(r)

        if allow_short:
            s, r = score_short_tf(tf1h, "1H")
            short_score += s * 1.00
            reasons_short.extend(r)

        # 2h
        if allow_long:
            s, r = score_long_tf(tf2h, "2H")
            long_score += s * 0.85
            reasons_long.extend(r)

        if allow_short:
            s, r = score_short_tf(tf2h, "2H")
            short_score += s * 0.85
            reasons_short.extend(r)

        # 4h
        if allow_long:
            s, r = score_long_tf(tf4h, "4H")
            long_score += s * 1.05
            reasons_long.extend(r)

        if allow_short:
            s, r = score_short_tf(tf4h, "4H")
            short_score += s * 1.05
            reasons_short.extend(r)

        # BTC score
        if allow_long and btc_bias == "LONG":
            long_score += 0.90
            reasons_long.append("BTC_LONG")
        elif allow_long and btc_bias == "NEUTRAL":
            long_score += 0.10
            reasons_long.append("BTC_NEUTRAL")

        if allow_short and btc_bias == "SHORT":
            short_score += 0.90
            reasons_short.append("BTC_SHORT")
        elif allow_short and btc_bias == "NEUTRAL":
            short_score += 0.10
            reasons_short.append("BTC_NEUTRAL")

        # USDT.D merkez score
        if allow_long:
            adj, tag = usdtd_long_score_adjustment(usdtd_info)
            long_score += adj
            reasons_long.append(tag)

        if allow_short:
            adj, tag = usdtd_short_score_adjustment(usdtd_info)
            short_score += adj
            reasons_short.append(tag)

        long_signal = None
        short_signal = None

        if allow_long and long_score >= LONG_SCORE_THRESHOLD:
            entry = current_price
            sl = entry - (atr_now * ATR_SL_MULTIPLIER)
            tp1 = entry + (atr_now * ATR_TP1_MULTIPLIER)
            tp2 = entry + (atr_now * ATR_TP2_MULTIPLIER)
            tp3 = entry + (atr_now * ATR_TP3_MULTIPLIER)

            risk = entry - sl
            reward = tp2 - entry
            rr = reward / risk if risk > 0 else 0

            # USDT.D güçlü longsa RR toleransı biraz daha esnek
            rr_need = MIN_RR_TO_TP2
            if usdtd_regime == "STRONG_LONG":
                rr_need = 1.18
            elif usdtd_regime == "LONG":
                rr_need = 1.22
            elif usdtd_regime == "WEAK_SHORT":
                rr_need = 1.35
            elif usdtd_regime == "SHORT":
                rr_need = 1.45

            if rr >= rr_need:
                long_signal = build_signal_payload(
                    direction="LONG",
                    entry=entry,
                    sl=sl,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                    score=long_score,
                    reason=" | ".join(reasons_long[:20]),
                    extra={
                        "btc_bias": btc_bias,
                        "usdtd_bias": usdtd_bias,
                        "usdtd_strength": usdtd_strength,
                        "usdtd_regime": usdtd_regime,
                        "rr": round(rr, 2),
                        "pre_15m_ok": long_confirm_15m_ok(tf15)
                    }
                )

        if allow_short and short_score >= SHORT_SCORE_THRESHOLD:
            entry = current_price
            sl = entry + (atr_now * ATR_SL_MULTIPLIER)
            tp1 = entry - (atr_now * ATR_TP1_MULTIPLIER)
            tp2 = entry - (atr_now * ATR_TP2_MULTIPLIER)
            tp3 = entry - (atr_now * ATR_TP3_MULTIPLIER)

            risk = sl - entry
            reward = entry - tp2
            rr = reward / risk if risk > 0 else 0

            rr_need = MIN_RR_TO_TP2
            if usdtd_regime == "STRONG_SHORT":
                rr_need = 1.18
            elif usdtd_regime == "SHORT":
                rr_need = 1.22
            elif usdtd_regime == "WEAK_LONG":
                rr_need = 1.35
            elif usdtd_regime == "LONG":
                rr_need = 1.45

            if rr >= rr_need:
                short_signal = build_signal_payload(
                    direction="SHORT",
                    entry=entry,
                    sl=sl,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                    score=short_score,
                    reason=" | ".join(reasons_short[:20]),
                    extra={
                        "btc_bias": btc_bias,
                        "usdtd_bias": usdtd_bias,
                        "usdtd_strength": usdtd_strength,
                        "usdtd_regime": usdtd_regime,
                        "rr": round(rr, 2),
                        "pre_15m_ok": short_confirm_15m_ok(tf15)
                    }
                )

        signal = None
        info = {
            "btc_bias": btc_bias,
            "usdtd_bias": usdtd_bias,
            "usdtd_strength": usdtd_strength,
            "usdtd_regime": usdtd_regime,
            "long_score": round(long_score, 2),
            "short_score": round(short_score, 2),
        }

        if long_signal and short_signal:
            signal = long_signal if long_signal["score"] >= short_signal["score"] else short_signal
        elif long_signal:
            signal = long_signal
        elif short_signal:
            signal = short_signal

        return signal, current_price, info, atr_now

    except Exception as e:
        log(f"build_trade_signal exception: {e}")
        return None, None, {"reason": f"EXCEPTION:{e}"}, None

# =========================================================
# SEND / UPGRADE RULES
# =========================================================
def signal_upgrade_needed(old_signal, new_signal):
    if not old_signal or not new_signal:
        return False, "MISSING"

    score_old = safe_float(old_signal.get("score"), 0.0)
    score_new = safe_float(new_signal.get("score"), 0.0)
    entry_move = entry_distance_pct(old_signal, new_signal)

    if score_new >= score_old + RESEND_IF_SCORE_IMPROVED_BY:
        return True, "SCORE_IMPROVED"

    if entry_move >= RESEND_IF_ENTRY_MOVED_PCT:
        return True, "ENTRY_MOVED"

    return False, "NO_UPGRADE"

def should_send_signal(state, new_signal, current_price):
    last_signal = state.get("last_signal")
    active_signal = state.get("active_signal")

    if not new_signal:
        return False, "NO_SIGNAL"

    if active_signal:
        if is_same_direction(active_signal, new_signal):
            upgraded, reason = signal_upgrade_needed(active_signal, new_signal)
            if upgraded:
                return True, f"SAME_DIR_UPGRADE:{reason}"
            return False, "SAME_DIR_BLOCK"

        if is_opposite_direction(active_signal, new_signal):
            if safe_float(new_signal.get("score"), 0.0) >= safe_float(active_signal.get("score"), 0.0) + 1.2:
                close_active_signal(state, "REVERSED_BY_STRONGER_SIGNAL", current_price, notify_telegram=True)
                return True, "REVERSED"
            return False, "OPPOSITE_NOT_STRONG_ENOUGH"

    if last_signal:
        mins = minutes_since(last_signal.get("created_ts"))
        if mins < MIN_SIGNAL_GAP_MINUTES and is_same_direction(last_signal, new_signal):
            if entry_distance_pct(last_signal, new_signal) < MIN_PRICE_DISTANCE_PCT:
                return False, "MIN_GAP_BLOCK"

    return True, "ALLOW"

def register_sent_signal(state, signal):
    state["last_signal"] = signal
    state["active_signal"] = signal

# =========================================================
# COMMANDS
# =========================================================
def handle_command(state, text):
    text = (text or "").strip().lower()

    if text == "/panel":
        return format_panel(state)
    if text == "/gunluk":
        return aggregate_period_summary(state, "daily")
    if text == "/haftalik":
        return aggregate_period_summary(state, "weekly")
    if text == "/aktif":
        return format_active_signal(state.get("active_signal"))
    if text == "/yardim":
        return help_text()
    if text == "/usdtd":
        info = get_usdtd_bias(state)
        regime = get_usdtd_regime_label(info)
        return (
            f"USDT.D\n\n"
            f"Rejim: {regime}\n"
            f"Bias: {info['bias']}\n"
            f"Strength: {info['strength']}\n"
            f"Now: {fmt_price(info['now']) if info['now'] is not None else '-'}\n"
            f"Fast: {fmt_price(info['fast']) if info['fast'] is not None else '-'}\n"
            f"Slow: {fmt_price(info['slow']) if info['slow'] is not None else '-'}\n"
            f"Fast-Slow %: {info.get('fast_slow_diff_pct', 0.0):.3f}\n"
            f"Now-Fast %: {info.get('now_fast_diff_pct', 0.0):.3f}\n"
            f"Reason: {info['reason']}"
        )
    if text == "/fiyat":
        try:
            p = get_last_price(SYMBOL)
            return f"ETHUSDT: {fmt_price(p)}"
        except Exception as e:
            return f"Fiyat alınamadı: {e}"

    return None

def process_telegram_commands(state):
    offset = int(state.get("last_update_id", 0)) + 1
    updates = tg_get_updates(offset)

    for upd in updates:
        update_id = int(upd.get("update_id", 0))
        state["last_update_id"] = max(int(state.get("last_update_id", 0)), update_id)

        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))

        if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
            continue

        text = msg.get("text", "")
        reply = handle_command(state, text)
        if reply:
            tg_send(reply)

# =========================================================
# MAIN LOOP
# =========================================================
def run_once():
    state = load_state()

    try:
        process_telegram_commands(state)

        current_price = get_last_price(SYMBOL)

        atr_now = None
        try:
            candles_5m_for_mgmt = get_klines(SYMBOL, "5m", 80)
            tf5_mgmt = analyze_timeframe(candles_5m_for_mgmt)
            atr_now = tf5_mgmt["atr14"]
        except Exception as e:
            log(f"ATR management fetch exception: {e}")

        refresh_active_signal_if_needed(state, current_price, atr_now)

        signal, signal_price, info, _ = build_trade_signal(state)
        current_price = signal_price if signal_price else current_price

        if signal:
            old_active = state.get("active_signal")

            can_send, reason_code = should_send_signal(
                state=state,
                new_signal=signal,
                current_price=current_price
            )

            if can_send:
                if old_active and is_same_direction(old_active, signal):
                    upgraded, upgrade_reason = signal_upgrade_needed(old_active, signal)
                    if upgraded:
                        tg_send(format_upgraded_signal_message(old_active, signal, upgrade_reason))
                        register_sent_signal(state, signal)
                        log(f"Sinyal update gönderildi: {reason_code}")
                else:
                    tg_send(format_signal_message(signal))
                    register_sent_signal(state, signal)
                    log(f"Yeni sinyal gönderildi: {reason_code}")
            else:
                log(f"Sinyal engellendi: {reason_code}")

    except Exception as e:
        log(f"run_once exception: {e}")

    finally:
        save_state(state)

def main():
    log("ETH MEXC USDTD CORE BOT başladı")
    while True:
        run_once()
        time.sleep(CHECK_EVERY_SECONDS)

if __name__ == "__main__":
    main()
