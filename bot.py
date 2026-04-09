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

CHECK_EVERY_SECONDS = 15
STATE_FILE = "eth_mexc_superbot_state.json"

# ---------------------------------------------------------
# STATE / SIGNAL LIFECYCLE
# ---------------------------------------------------------
MIN_SIGNAL_GAP_MINUTES = 25
MAX_ACTIVE_SIGNAL_AGE_MINUTES = 180
SIGNAL_TIMEOUT_MINUTES = 120
MIN_PRICE_DISTANCE_PCT = 0.45
REVERSE_SIGNAL_STRENGTH_BONUS = 1.75
SAME_DIRECTION_SCORE_BONUS = 2.5

# ---------------------------------------------------------
# SIGNAL THRESHOLDS
# ---------------------------------------------------------
LONG_SCORE_THRESHOLD = 9.5
SHORT_SCORE_THRESHOLD = 9.5
MIN_RR_TO_TP2 = 1.35

# ---------------------------------------------------------
# RISK
# ---------------------------------------------------------
ATR_SL_MULTIPLIER = 1.20
ATR_TP1_MULTIPLIER = 1.25
ATR_TP2_MULTIPLIER = 2.20
ATR_TP3_MULTIPLIER = 3.30
TRAIL_AFTER_TP2_ATR = 1.05

# ---------------------------------------------------------
# FILTERS
# ---------------------------------------------------------
USE_BTC_FILTER = True
BTC_15M_TREND_THRESHOLD = 0.12

USE_USDTD_FILTER = True
USDTD_FAST_EMA = 6
USDTD_SLOW_EMA = 18
USDTD_HISTORY_LIMIT = 240
CG_CACHE_TTL_SECONDS = 180
USDTD_LONG_SCORE_BONUS = 1.25
USDTD_SHORT_SCORE_BONUS = 1.25
USDTD_OPPOSITE_PENALTY = 0.90

MAX_HTTP_RETRY = 3
HTTP_TIMEOUT = 15

# ---------------------------------------------------------
# MESSAGE / ENTRY LOGIC
# ---------------------------------------------------------
ENTRY_NEAR_PCT = 0.12
CANCEL_IF_TP1_BEFORE_ENTRY = True

# ---------------------------------------------------------
# PRO++ SMART RESEND
# ---------------------------------------------------------
RESEND_IF_SCORE_IMPROVED_BY = 1.0
RESEND_IF_ENTRY_MOVED_PCT = 0.22
RESEND_IF_REASON_CHANGED = True
ALLOW_SMART_REPEAT_SIGNAL = True

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
        log("Telegram ENV eksik: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(url, json=payload, timeout=20)
        log(f"Telegram status={r.status_code}")
        if r.status_code == 200:
            return True
        log(f"Telegram hata cevabi: {r.text}")
        return False
    except Exception as e:
        log(f"Telegram exception: {e}")
        return False

def tg_get_updates(offset):
    if not TELEGRAM_TOKEN:
        return []

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {
        "offset": offset,
        "timeout": 1
    }

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
# MEXC MARKET DATA
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
# COINGECKO -> USDT.D PROXY
# =========================================================
def cg_get_global_total_market_cap():
    url = f"{COINGECKO_BASE_URL}/global"
    data = http_get_json(url)
    total_mc = safe_float(data.get("data", {}).get("total_market_cap", {}).get("usd"))
    if total_mc is None or total_mc <= 0:
        raise RuntimeError("CoinGecko total market cap alınamadı")
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
        raise RuntimeError("CoinGecko tether market cap alınamadı")
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
def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

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
        "stops": 0
    }

def default_state():
    return {
        "last_signal": None,
        "active_signal": None,
        "signal_history": [],
        "cg_cache": {},
        "history": {
            "usdtd": []
        },
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
        return "NEUTRAL", "USDTD_FILTER_DISABLED", None, None, None

    try:
        info = get_usdtd_proxy(state)
        usdtd_now = float(info["usdtd"])
        append_usdtd_history(state, usdtd_now)
        values = get_usdtd_values(state)

        if len(values) < USDTD_SLOW_EMA:
            return "NEUTRAL", f"USDTD_WAIT_HISTORY:{len(values)}", usdtd_now, None, None

        fast_series = ema_series(values, USDTD_FAST_EMA)
        slow_series = ema_series(values, USDTD_SLOW_EMA)

        usdtd_fast = fast_series[-1]
        usdtd_slow = slow_series[-1]

        if usdtd_fast is None or usdtd_slow is None:
            return "NEUTRAL", "USDTD_EMA_NOT_READY", usdtd_now, usdtd_fast, usdtd_slow

        if usdtd_fast < usdtd_slow and usdtd_now <= usdtd_fast:
            return "LONG", f"USDTD_RISK_ON:{round(usdtd_now, 3)}", usdtd_now, usdtd_fast, usdtd_slow
        elif usdtd_fast > usdtd_slow and usdtd_now >= usdtd_fast:
            return "SHORT", f"USDTD_RISK_OFF:{round(usdtd_now, 3)}", usdtd_now, usdtd_fast, usdtd_slow
        else:
            return "NEUTRAL", f"USDTD_NEUTRAL:{round(usdtd_now, 3)}", usdtd_now, usdtd_fast, usdtd_slow

    except Exception as e:
        return "NEUTRAL", f"USDTD_FAIL_OPEN:{e}", None, None, None

# =========================================================
# MESSAGE HELPERS
# =========================================================
def classify_signal_strength(score):
    s = safe_float(score, 0.0)
    if s >= 12.0:
        return "GUCLU"
    elif s >= 10.0:
        return "ORTA"
    return "ZAYIF"

def build_signal_signature(sig):
    if not sig:
        return ""
    return (
        f"{sig.get('direction','')}"
        f"|{round(safe_float(sig.get('entry'), 0.0), 2)}"
        f"|{round(safe_float(sig.get('score'), 0.0), 2)}"
        f"|{sig.get('strategy_tag','')}"
    )

def signal_upgrade_needed(old_sig, new_sig):
    if not old_sig or not new_sig:
        return False, "NO_COMPARE"

    old_score = safe_float(old_sig.get("score"), 0.0)
    new_score = safe_float(new_sig.get("score"), 0.0)

    old_entry = safe_float(old_sig.get("entry"), 0.0)
    new_entry = safe_float(new_sig.get("entry"), 0.0)

    old_reason = str(old_sig.get("reason", "")).strip()
    new_reason = str(new_sig.get("reason", "")).strip()

    score_diff = new_score - old_score
    entry_diff_pct = pct_diff(old_entry, new_entry) if old_entry else 999.0

    if score_diff >= RESEND_IF_SCORE_IMPROVED_BY:
        return True, f"SCORE_UPGRADE:+{round(score_diff, 2)}"

    if entry_diff_pct >= RESEND_IF_ENTRY_MOVED_PCT:
        return True, f"ENTRY_SHIFT:{round(entry_diff_pct, 3)}%"

    if RESEND_IF_REASON_CHANGED and old_reason != new_reason:
        return True, "REASON_CHANGED"

    return False, "NO_MEANINGFUL_UPGRADE"

def build_signal_payload(direction, entry, sl, tp1, tp2, tp3, score, strategy_tag, reason):
    return {
        "direction": direction.upper(),
        "entry": round(float(entry), 4),
        "sl": round(float(sl), 4),
        "tp1": round(float(tp1), 4),
        "tp2": round(float(tp2), 4),
        "tp3": round(float(tp3), 4),
        "score": round(float(score), 2),
        "strategy_tag": strategy_tag,
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
        "signal_signature": ""
    }

def is_same_direction(a, b):
    return bool(a and b and a.get("direction") == b.get("direction"))

def is_opposite_direction(a, b):
    return bool(a and b and a.get("direction") != b.get("direction"))

def entry_distance_pct(sig_a, sig_b):
    if not sig_a or not sig_b:
        return 999.0
    return pct_diff(sig_a.get("entry"), sig_b.get("entry"))

def format_signal_message(sig, current_price):
    direction = sig["direction"].upper()
    emoji = "🟢" if direction == "LONG" else "🔴"

    entry = sig["entry"]
    sl = sig["sl"]
    tp1 = sig["tp1"]
    tp2 = sig["tp2"]
    tp3 = sig["tp3"]

    risk = abs(entry - sl)
    reward = abs(tp2 - entry)
    rr = reward / risk if risk > 0 else 0

    strength = classify_signal_strength(sig["score"])

    return (
        f"{emoji} ETHUSDT {direction}\n\n"
        f"Entry: {fmt_price(entry)}\n"
        f"SL: {fmt_price(sl)}\n\n"
        f"TP1: {fmt_price(tp1)}\n"
        f"TP2: {fmt_price(tp2)}\n"
        f"TP3: {fmt_price(tp3)}\n\n"
        f"RR: {rr:.2f}\n"
        f"Güç: {strength}"
    )

def format_upgraded_signal_message(old_sig, new_sig, current_price, upgrade_reason):
    direction = new_sig["direction"].upper()

    return (
        f"🔄 ETHUSDT {direction} (UPDATE)\n\n"
        f"Skor: {old_sig['score']} → {new_sig['score']}\n"
        f"Güç: {classify_signal_strength(new_sig['score'])}\n\n"
        f"Entry: {fmt_price(new_sig['entry'])}\n"
        f"SL: {fmt_price(new_sig['sl'])}\n\n"
        f"TP1: {fmt_price(new_sig['tp1'])}\n"
        f"TP2: {fmt_price(new_sig['tp2'])}\n"
        f"TP3: {fmt_price(new_sig['tp3'])}"
    )

def format_close_message(sig, close_reason, close_price=None):
    if close_reason == "TP3_HIT":
        return "🏁 TP3 HIT\n\nTrade tamamlandı"
    if close_reason == "SL_HIT":
        return "❌ STOP\n\nTrade kapandı"
    if close_reason == "TIMEOUT":
        return "⌛ TIMEOUT\n\nSinyal süresi doldu"
    if close_reason == "CANCELLED_BEFORE_ENTRY_TP1":
        return "⚠️ SETUP İPTAL\n\nEntry gelmeden TP1 oldu"
    if close_reason == "REVERSED_BY_STRONGER_SIGNAL":
        return "↩️ TERS SİNYAL\n\nDaha güçlü ters sinyal geldi"
    return "Trade kapandı"

def format_active_signal(sig):
    if not sig:
        return "Aktif işlem yok."

    risk = abs(sig["entry"] - sig["sl"])
    reward = abs(sig["tp2"] - sig["entry"])
    rr = reward / risk if risk > 0 else 0.0

    filled = "EVET" if sig.get("active") else "BEKLIYOR"
    return (
        f"📌 AKTIF İSLEM\n\n"
        f"Yön: {sig['direction']}\n"
        f"Durum: {filled}\n"
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
        title = "📅 GÜNLÜK ÖZET"
        match_fn = lambda ts: day_key_from_ts(ts) == key
    else:
        key = week_key_from_ts(now)
        title = "📈 HAFTALIK ÖZET"
        match_fn = lambda ts: week_key_from_ts(ts) == key

    total = 0
    wins = 0
    losses = 0
    tp1 = 0
    tp2 = 0
    tp3 = 0
    stops = 0

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

    win_rate = (wins / total * 100.0) if total > 0 else 0.0

    return (
        f"{title}\n\n"
        f"Toplam Sinyal: {total}\n"
        f"Kazanan: {wins}\n"
        f"Kaybeden: {losses}\n"
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
        "/yardim"
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
        tg_send(format_close_message(active, close_reason, current_price))

    state["signal_history"].append(active)
    update_stats_for_closed_signal(state, active)
    state["active_signal"] = None
    log(f"Aktif sinyal kapatildi: {close_reason}")

def is_tp1_hit(signal, current_price):
    if not signal:
        return False

    direction = signal["direction"]
    tp1 = safe_float(signal.get("tp1"))
    cp = safe_float(current_price)

    if tp1 is None or cp is None:
        return False

    if direction == "LONG":
        return cp >= tp1
    return cp <= tp1

def is_tp2_hit(signal, current_price):
    if not signal:
        return False

    direction = signal["direction"]
    tp2 = safe_float(signal.get("tp2"))
    cp = safe_float(current_price)

    if tp2 is None or cp is None:
        return False

    if direction == "LONG":
        return cp >= tp2
    return cp <= tp2

def is_tp3_hit(signal, current_price):
    if not signal:
        return False

    direction = signal["direction"]
    tp3 = safe_float(signal.get("tp3"))
    cp = safe_float(current_price)

    if tp3 is None or cp is None:
        return False

    if direction == "LONG":
        return cp >= tp3
    return cp <= tp3

def is_sl_hit(signal, current_price):
    if not signal:
        return False

    direction = signal["direction"]
    sl = safe_float(signal.get("sl"))
    cp = safe_float(current_price)

    if sl is None or cp is None:
        return False

    if direction == "LONG":
        return cp <= sl
    return cp >= sl

def is_entry_filled(signal, current_price):
    if not signal:
        return False
    entry = safe_float(signal.get("entry"))
    cp = safe_float(current_price)
    if entry is None or cp is None or entry == 0:
        return False
    return (abs(cp - entry) / entry * 100.0) <= ENTRY_NEAR_PCT

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

def refresh_active_signal_if_needed(state, current_price, atr_now=None):
    active = state.get("active_signal")
    if not active:
        return

    if not active.get("active"):
        if CANCEL_IF_TP1_BEFORE_ENTRY and is_tp1_hit(active, current_price):
            close_active_signal(state, "CANCELLED_BEFORE_ENTRY_TP1", current_price, notify_telegram=True)
            return

        if is_entry_filled(active, current_price):
            active["active"] = True
            active["status"] = "OPEN"
            active["updated_ts"] = utc_ts()
            tg_send(
                f"✅ ENTRY FILLED\n\n"
                f"{active['direction']} @ {fmt_price(active['entry'])}"
            )
            return

        if is_expired(active):
            close_active_signal(state, "TIMEOUT", current_price, notify_telegram=True)
            return

        return

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
            tg_send("🎯 TP1 HIT\n\nBE aktif (risk sıfır)")

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

def recent_gap_blocked(state, new_signal):
    last_signal = state.get("last_signal")
    if not last_signal:
        return False

    mins = minutes_since(last_signal.get("created_ts"))
    if mins >= MIN_SIGNAL_GAP_MINUTES:
        return False

    if is_same_direction(last_signal, new_signal):
        dist = entry_distance_pct(last_signal, new_signal)
        if dist < MIN_PRICE_DISTANCE_PCT:
            return True

    return False

def should_send_signal(state, new_signal, current_price):
    active = state.get("active_signal")

    if not active:
        if recent_gap_blocked(state, new_signal):
            return False, "RECENT_DUPLICATE_GAP"
        return True, "NO_ACTIVE_SIGNAL"

    active_age = minutes_since(active.get("created_ts"))
    if active_age >= MAX_ACTIVE_SIGNAL_AGE_MINUTES:
        close_active_signal(state, "MAX_ACTIVE_AGE_EXCEEDED", current_price, notify_telegram=True)
        if recent_gap_blocked(state, new_signal):
            return False, "RECENT_DUPLICATE_GAP"
        return True, "ACTIVE_TOO_OLD"

    if is_opposite_direction(active, new_signal):
        active_score = safe_float(active.get("score"), 0.0)
        new_score = safe_float(new_signal.get("score"), 0.0)

        if new_score >= active_score + REVERSE_SIGNAL_STRENGTH_BONUS:
            close_active_signal(state, "REVERSED_BY_STRONGER_SIGNAL", current_price, notify_telegram=True)
            return True, "OPPOSITE_STRONG_SIGNAL"

        return False, "OPPOSITE_SIGNAL_NOT_STRONG_ENOUGH"

    dist_pct = entry_distance_pct(active, new_signal)
    active_score = safe_float(active.get("score"), 0.0)
    new_score = safe_float(new_signal.get("score"), 0.0)

    if dist_pct >= MIN_PRICE_DISTANCE_PCT:
        return True, "SAME_DIRECTION_NEW_ZONE"

    if new_score >= active_score + SAME_DIRECTION_SCORE_BONUS:
        return True, "SAME_DIRECTION_MUCH_STRONGER"

    if ALLOW_SMART_REPEAT_SIGNAL:
        upgraded, why = signal_upgrade_needed(active, new_signal)
        if upgraded:
            return True, why

    return False, "SIMILAR_ACTIVE_SIGNAL_STILL_OPEN"

def register_sent_signal(state, sent_signal):
    sent_signal["created_ts"] = utc_ts()
    sent_signal["updated_ts"] = utc_ts()
    sent_signal["status"] = "PENDING"
    sent_signal["active"] = False
    sent_signal["signal_signature"] = build_signal_signature(sent_signal)
    state["active_signal"] = sent_signal
    state["last_signal"] = sent_signal

# =========================================================
# ANALYSIS HELPERS
# =========================================================
def analyze_timeframe(candles):
    closed = candles[:-1] if len(candles) > 2 else candles[:]
    if len(closed) < 60:
        raise ValueError("Yeterli kapalı mum verisi yok")

    closes = [c["close"] for c in closed]
    highs = [c["high"] for c in closed]
    lows = [c["low"] for c in closed]
    volumes = [c["volume"] for c in closed]

    ema9 = ema_series(closes, 9)
    ema21 = ema_series(closes, 21)
    ema50 = ema_series(closes, 50)
    rsi14 = rsi_series(closes, 14)
    macd_line, macd_signal, macd_hist = macd_series(closes, 12, 26, 9)

    return {
        "close": closes[-1],
        "prev_close": closes[-2] if len(closes) >= 2 else closes[-1],
        "ema9": ema9[-1],
        "ema21": ema21[-1],
        "ema50": ema50[-1],
        "rsi14": rsi14[-1],
        "macd": macd_line[-1],
        "macd_signal": macd_signal[-1],
        "macd_hist": macd_hist[-1],
        "atr14": atr(closed, 14),
        "swing_high_20": highest_high(closed, 20),
        "swing_low_20": lowest_low(closed, 20),
        "vol_last": volumes[-1],
        "vol_sma20": sma(volumes, 20),
        "high_last": highs[-1],
        "low_last": lows[-1],
    }

def get_btc_filter_bias():
    if not USE_BTC_FILTER:
        return "NEUTRAL", "BTC_FILTER_DISABLED"

    try:
        btc_15m = get_klines(BTC_SYMBOL, "15m", 80)
        closed = btc_15m[:-1] if len(btc_15m) > 6 else btc_15m

        btc_close_prev = closed[-5]["close"]
        btc_close_now = closed[-1]["close"]
        btc_move = pct_change(btc_close_prev, btc_close_now)

        if btc_move >= BTC_15M_TREND_THRESHOLD:
            return "LONG", f"BTC_BULL:{round(btc_move, 3)}%"
        elif btc_move <= -BTC_15M_TREND_THRESHOLD:
            return "SHORT", f"BTC_BEAR:{round(btc_move, 3)}%"
        else:
            return "NEUTRAL", f"BTC_FLAT:{round(btc_move, 3)}%"
    except Exception as e:
        return "NEUTRAL", f"BTC_FILTER_FAIL_OPEN:{e}"

# =========================================================
# SIGNAL ENGINE
# =========================================================
def build_trade_signal(state):
    candles_15m = get_klines(SYMBOL, "15m", 200)
    candles_5m = get_klines(SYMBOL, "5m", 200)
    candles_1h = get_klines(SYMBOL, "60m", 150)

    tf15 = analyze_timeframe(candles_15m)
    tf5 = analyze_timeframe(candles_5m)
    tf1h = analyze_timeframe(candles_1h)

    current_price = tf5["close"]
    btc_bias, btc_reason = get_btc_filter_bias()
    usdtd_bias, usdtd_reason, usdtd_now, usdtd_fast, usdtd_slow = get_usdtd_bias(state)

    long_score = 0.0
    short_score = 0.0
    reasons_long = []
    reasons_short = []

    if tf15["ema9"] and tf15["ema21"] and tf15["ema50"]:
        if tf15["ema9"] > tf15["ema21"] > tf15["ema50"]:
            long_score += 3.0
            reasons_long.append("15m ema bull stack")

        if tf15["ema9"] < tf15["ema21"] < tf15["ema50"]:
            short_score += 3.0
            reasons_short.append("15m ema bear stack")

    if tf15["close"] > (tf15["ema21"] or 0):
        long_score += 1.5
        reasons_long.append("15m close > ema21")
    else:
        short_score += 1.5
        reasons_short.append("15m close < ema21")

    if tf15["rsi14"] is not None:
        if 52 <= tf15["rsi14"] <= 72:
            long_score += 1.5
            reasons_long.append(f"15m rsi strong {round(tf15['rsi14'], 2)}")
        if 28 <= tf15["rsi14"] <= 48:
            short_score += 1.5
            reasons_short.append(f"15m rsi weak {round(tf15['rsi14'], 2)}")

    if tf15["macd_hist"] is not None:
        if tf15["macd_hist"] > 0:
            long_score += 1.5
            reasons_long.append("15m macd hist > 0")
        if tf15["macd_hist"] < 0:
            short_score += 1.5
            reasons_short.append("15m macd hist < 0")

    if tf1h["ema21"] and tf1h["ema50"]:
        if tf1h["close"] > tf1h["ema21"] and tf1h["ema21"] > tf1h["ema50"]:
            long_score += 1.5
            reasons_long.append("60m strong long trend")

        if tf1h["close"] < tf1h["ema21"] and tf1h["ema21"] < tf1h["ema50"]:
            short_score += 1.5
            reasons_short.append("60m strong short trend")

    if tf1h["rsi14"] is not None:
        if tf1h["rsi14"] >= 52:
            long_score += 0.5
            reasons_long.append(f"60m rsi bullish {round(tf1h['rsi14'], 2)}")
        elif tf1h["rsi14"] <= 48:
            short_score += 0.5
            reasons_short.append(f"60m rsi bearish {round(tf1h['rsi14'], 2)}")

    if btc_bias == "LONG":
        long_score += 1.0
        short_score -= 0.75
        reasons_long.append("btc supports long")
        reasons_short.append("btc against short")
    elif btc_bias == "SHORT":
        short_score += 1.0
        long_score -= 0.75
        reasons_short.append("btc supports short")
        reasons_long.append("btc against long")
    else:
        long_score -= 0.20
        short_score -= 0.20

    if usdtd_bias == "LONG":
        long_score += USDTD_LONG_SCORE_BONUS
        short_score -= USDTD_OPPOSITE_PENALTY
        reasons_long.append("usdtd risk-on")
        reasons_short.append("usdtd against short")
    elif usdtd_bias == "SHORT":
        short_score += USDTD_SHORT_SCORE_BONUS
        long_score -= USDTD_OPPOSITE_PENALTY
        reasons_short.append("usdtd risk-off")
        reasons_long.append("usdtd against long")
    else:
        reasons_long.append("usdtd neutral")
        reasons_short.append("usdtd neutral")

    long_entry_ok = False
    short_entry_ok = False

    if tf5["ema9"] and tf5["ema21"] and tf5["ema50"]:
        if (
            current_price > tf5["ema21"] and
            tf5["ema9"] >= tf5["ema21"] and
            tf5["ema21"] >= tf5["ema50"] and
            tf5["rsi14"] is not None and 48 <= tf5["rsi14"] <= 68 and
            tf5["macd"] is not None and tf5["macd_signal"] is not None and
            tf5["macd"] >= tf5["macd_signal"]
        ):
            long_score += 3.0
            reasons_long.append("5m entry aligned")
            long_entry_ok = True

        if (
            current_price < tf5["ema21"] and
            tf5["ema9"] <= tf5["ema21"] and
            tf5["ema21"] <= tf5["ema50"] and
            tf5["rsi14"] is not None and 32 <= tf5["rsi14"] <= 52 and
            tf5["macd"] is not None and tf5["macd_signal"] is not None and
            tf5["macd"] <= tf5["macd_signal"]
        ):
            short_score += 3.0
            reasons_short.append("5m entry aligned")
            short_entry_ok = True

    if tf5["vol_last"] and tf5["vol_sma20"]:
        if tf5["vol_last"] > tf5["vol_sma20"] * 1.08:
            if long_entry_ok:
                long_score += 1.0
                reasons_long.append("5m vol strong confirm")
            if short_entry_ok:
                short_score += 1.0
                reasons_short.append("5m vol strong confirm")

    atr5 = tf5["atr14"]
    if atr5 is None or atr5 <= 0:
        return None, current_price, "ATR_UNAVAILABLE", atr5

    candidates = []

    if long_entry_ok and long_score >= LONG_SCORE_THRESHOLD:
        entry = current_price
        sl = entry - (atr5 * ATR_SL_MULTIPLIER)
        tp1 = entry + (atr5 * ATR_TP1_MULTIPLIER)
        tp2 = entry + (atr5 * ATR_TP2_MULTIPLIER)
        tp3 = entry + (atr5 * ATR_TP3_MULTIPLIER)

        risk = entry - sl
        reward_tp2 = tp2 - entry
        rr = reward_tp2 / risk if risk > 0 else 0

        if rr >= MIN_RR_TO_TP2:
            reasons = reasons_long[:8] + [btc_reason, usdtd_reason, f"RR:{round(rr, 2)}"]
            if usdtd_now is not None and usdtd_fast is not None and usdtd_slow is not None:
                reasons.append(f"USDTD:{round(usdtd_now,3)} F/S:{round(usdtd_fast,3)}/{round(usdtd_slow,3)}")

            candidates.append(build_signal_payload(
                direction="LONG",
                entry=entry,
                sl=sl,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                score=long_score,
                strategy_tag="ETH_MEXC_15M_TREND_5M_ENTRY_USDTD",
                reason=" | ".join(reasons)
            ))

    if short_entry_ok and short_score >= SHORT_SCORE_THRESHOLD:
        entry = current_price
        sl = entry + (atr5 * ATR_SL_MULTIPLIER)
        tp1 = entry - (atr5 * ATR_TP1_MULTIPLIER)
        tp2 = entry - (atr5 * ATR_TP2_MULTIPLIER)
        tp3 = entry - (atr5 * ATR_TP3_MULTIPLIER)

        risk = sl - entry
        reward_tp2 = entry - tp2
        rr = reward_tp2 / risk if risk > 0 else 0

        if rr >= MIN_RR_TO_TP2:
            reasons = reasons_short[:8] + [btc_reason, usdtd_reason, f"RR:{round(rr, 2)}"]
            if usdtd_now is not None and usdtd_fast is not None and usdtd_slow is not None:
                reasons.append(f"USDTD:{round(usdtd_now,3)} F/S:{round(usdtd_fast,3)}/{round(usdtd_slow,3)}")

            candidates.append(build_signal_payload(
                direction="SHORT",
                entry=entry,
                sl=sl,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                score=short_score,
                strategy_tag="ETH_MEXC_15M_TREND_5M_ENTRY_USDTD",
                reason=" | ".join(reasons)
            ))

    if not candidates:
        return None, current_price, f"NO_VALID_SIGNAL | long={round(long_score, 2)} short={round(short_score, 2)}", atr5

    candidates.sort(key=lambda x: x["score"], reverse=True)
    best = candidates[0]
    return best, current_price, f"SIGNAL_READY | long={round(long_score, 2)} short={round(short_score, 2)}", atr5

# =========================================================
# COMMANDS
# =========================================================
def handle_command(state, text):
    text = (text or "").strip().lower()

    if text in ("/yardim", "/help", "/start"):
        return help_text()

    if text == "/panel":
        return format_panel(state)

    if text == "/gunluk":
        return aggregate_period_summary(state, "daily")

    if text == "/haftalik":
        return aggregate_period_summary(state, "weekly")

    if text == "/aktif":
        return format_active_signal(state.get("active_signal"))

    return None

def process_telegram_commands(state):
    updates = tg_get_updates(int(state.get("last_update_id", 0)) + 1)

    for upd in updates:
        state["last_update_id"] = upd.get("update_id", state.get("last_update_id", 0))

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
                        message = format_upgraded_signal_message(
                            old_sig=old_active,
                            new_sig=signal,
                            current_price=current_price,
                            upgrade_reason=upgrade_reason
                        )
                    else:
                        message = format_signal_message(signal, current_price)
                else:
                    message = format_signal_message(signal, current_price)

                sent = tg_send(message)

                if sent:
                    register_sent_signal(state, signal)
                    log(
                        f"YENI/UPDATED SINYAL GONDERILDI | {signal['direction']} | "
                        f"entry={signal['entry']} sl={signal['sl']} tp1={signal['tp1']} "
                        f"tp2={signal['tp2']} tp3={signal['tp3']} score={signal['score']} | {reason_code}"
                    )
                else:
                    log("Sinyal bulundu ama Telegram gönderimi başarısız.")
            else:
                log(
                    f"Sinyal var ama gönderilmedi: {reason_code} | "
                    f"Yön={signal['direction']} Entry={signal['entry']} Score={signal['score']}"
                )
        else:
            log(f"Yeni valid sinyal yok. {info}")

    except Exception as e:
        log(f"Analiz exception: {e}")

    try:
        save_state(state)
    except Exception as e:
        log(f"State save exception: {e}")

def send_startup_message():
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        tg_send(
            "🤖 CoinRadar PRO++ başladı\n\n"
            "Kaynak: MEXC Spot\n"
            "Filtre: BTC + USDT.D\n"
            "Komutlar: /panel /gunluk /haftalik /aktif /yardim"
        )
        log("Başlangıç mesajı gönderildi.")
    else:
        log("Başlangıç mesajı gönderilemedi: Telegram ENV eksik.")

def main():
    log("Bot başlıyor...")
    log(f"Telegram token var mi: {'EVET' if TELEGRAM_TOKEN else 'HAYIR'}")
    log(f"Telegram chat id: {TELEGRAM_CHAT_ID if TELEGRAM_CHAT_ID else 'BOS'}")
    send_startup_message()

    while True:
        log("Analiz yapılıyor...")
        run_once()
        time.sleep(CHECK_EVERY_SECONDS)

if __name__ == "__main__":
    main()
