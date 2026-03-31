import os
import time
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import pandas as pd

from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, ADXIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import winreg
except ImportError:
    winreg = None

# =========================================================
# GLOBAL
# =========================================================
TZ = ZoneInfo("Europe/Istanbul")

def now_dt():
    return datetime.now(TZ)

def now_ts() -> int:
    return int(now_dt().timestamp())

def now_str() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")

def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def pct_change(a: float, b: float) -> float:
    if a in (None, 0) or b is None:
        return 0.0
    return ((b / a) - 1.0) * 100.0

def pct_diff(a, b):
    if a in (None, 0) or b is None:
        return 999.0
    return abs(a - b) / a * 100.0

def read_windows_env(name: str, default: str = "") -> str:
    val = os.getenv(name)
    if val is not None and str(val).strip():
        return str(val).strip()

    if winreg is None:
        return default

    reg_paths = [
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ]

    for root, path in reg_paths:
        try:
            with winreg.OpenKey(root, path) as key:
                value, _ = winreg.QueryValueEx(key, name)
                if value is not None and str(value).strip():
                    return str(value).strip()
        except Exception:
            continue

    return default

# =========================================================
# ENV
# =========================================================
TELEGRAM_BOT_TOKEN = read_windows_env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = read_windows_env("TELEGRAM_CHAT_ID", "")

# =========================================================
# HTTP
# =========================================================
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "crypto-propp-bot/1.0"})

retry = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=1.0,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)
adapter = HTTPAdapter(max_retries=retry)
HTTP.mount("https://", adapter)
HTTP.mount("http://", adapter)

# =========================================================
# CONFIG
# =========================================================
LOG_FILE = "crypto_propp_bot.log"
STATE_FILE = "crypto_propp_state.json"

BASE_URL_SPOT = "https://api.binance.com"
BASE_URL_FUTURES = "https://fapi.binance.com"

MARKET_TYPE = "spot"  # "spot" veya "futures"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "PAXGUSDT",
]

BTC_SYMBOL = "BTCUSDT"

CHECK_EVERY_SECONDS = 90

TF_TREND = "4h"
TF_SETUP = "1h"
TF_ENTRY = "30m"

LIMIT_4H = 300
LIMIT_1H = 400
LIMIT_30M = 500

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

MIN_4H_ADX = 18
MIN_1H_ADX = 16

MIN_4H_RSI_LONG = 52
MAX_4H_RSI_LONG = 74
MIN_4H_RSI_SHORT = 26
MAX_4H_RSI_SHORT = 48

MIN_1H_RSI_LONG = 48
MAX_1H_RSI_LONG = 66
MIN_1H_RSI_SHORT = 34
MAX_1H_RSI_SHORT = 52

MIN_30M_RSI_LONG = 46
MAX_30M_RSI_LONG = 64
MIN_30M_RSI_SHORT = 36
MAX_30M_RSI_SHORT = 54

VOLUME_SURGE_MULT = 1.18
BOLL_SQUEEZE_Q = 0.30
BREAKOUT_LOOKBACK = 20

ATR_SL_MULTIPLIER = 1.25
ATR_TP1_MULTIPLIER = 1.50
ATR_TP2_MULTIPLIER = 2.40
ATR_TP3_MULTIPLIER = 3.40

MIN_RR_TO_TP2 = 1.25
MAX_LAST_CANDLE_RANGE_ATR = 2.30
MAX_DISTANCE_FROM_EMA20_ATR = 1.80

USE_BTC_FILTER = True
BTC_30M_TREND_THRESHOLD = 0.18

MIN_SIGNAL_GAP_MINUTES = 30
MAX_ACTIVE_SIGNAL_AGE_MINUTES = 240
SIGNAL_TIMEOUT_MINUTES = 240
MIN_PRICE_DISTANCE_PCT = 0.55
REVERSE_SIGNAL_STRENGTH_BONUS = 1.75
SAME_DIRECTION_SCORE_BONUS = 2.50

TOP_N_SIGNALS = 3
MIN_SCORE_SEND = 10

# =========================================================
# LOGGING
# =========================================================
logger = logging.getLogger("crypto_propp_bot")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

if not logger.handlers:
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logger.addHandler(sh)

# =========================================================
# TELEGRAM
# =========================================================
def tg_send(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID eksik")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 3900
    parts = [text[i:i + max_len] for i in range(0, len(text), max_len)] if text else [""]

    ok = True
    for part in parts:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": part,
            "disable_web_page_preview": True,
        }
        try:
            r = HTTP.post(url, json=payload, timeout=20)
            if r.status_code != 200:
                logger.error("Telegram hata: %s", r.text)
                ok = False
        except Exception as e:
            logger.exception("Telegram exception: %s", e)
            ok = False
    return ok

# =========================================================
# STATE
# =========================================================
def default_state():
    return {
        "last_signal": {},
        "active_signal": {},
        "signal_history": [],
    }

def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return default_state()
            data.setdefault("last_signal", {})
            data.setdefault("active_signal", {})
            data.setdefault("signal_history", [])
            return data
    except Exception as e:
        logger.exception("State load exception: %s", e)
        return default_state()

def save_state(state):
    tmp_file = STATE_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, STATE_FILE)

# =========================================================
# BINANCE DATA
# =========================================================
def get_klines(symbol: str, interval: str, limit: int = 300):
    if MARKET_TYPE == "futures":
        url = f"{BASE_URL_FUTURES}/fapi/v1/klines"
    else:
        url = f"{BASE_URL_SPOT}/api/v3/klines"

    try:
        r = HTTP.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("Kline alınamadı %s %s: %s", symbol, interval, e)
        return None

    if not data or not isinstance(data, list):
        return None

    rows = []
    try:
        for x in data:
            rows.append({
                "open_time": pd.to_datetime(x[0], unit="ms", utc=True).tz_convert(TZ).tz_localize(None),
                "open": float(x[1]),
                "high": float(x[2]),
                "low": float(x[3]),
                "close": float(x[4]),
                "volume": float(x[5]),
                "close_time": pd.to_datetime(x[6], unit="ms", utc=True).tz_convert(TZ).tz_localize(None),
                "quote_volume": float(x[7]),
                "trades": int(x[8]),
            })

        df = pd.DataFrame(rows)
        df.set_index("open_time", inplace=True)
        return df
    except Exception as e:
        logger.warning("Kline parse hata %s %s: %s", symbol, interval, e)
        return None

# =========================================================
# INDICATORS
# =========================================================
class Ind:
    def __init__(self, close, rsi, ema20, ema50, ema200, atr, adx):
        self.close = float(close)
        self.rsi = float(rsi)
        self.ema20 = float(ema20)
        self.ema50 = float(ema50)
        self.ema200 = float(ema200)
        self.atr = float(atr)
        self.adx = float(adx)

def compute_ind(df: pd.DataFrame):
    if df is None or df.empty:
        return None

    need = max(EMA_SLOW, RSI_PERIOD, ATR_PERIOD, ADX_PERIOD) + 20
    if len(df) < need:
        return None

    close = df["close"]
    high = df["high"]
    low = df["low"]

    rsi = RSIIndicator(close=close, window=RSI_PERIOD).rsi()
    ema20 = EMAIndicator(close=close, window=EMA_FAST).ema_indicator()
    ema50 = EMAIndicator(close=close, window=EMA_MID).ema_indicator()
    ema200 = EMAIndicator(close=close, window=EMA_SLOW).ema_indicator()
    atr = AverageTrueRange(high=high, low=low, close=close, window=ATR_PERIOD).average_true_range()
    adx = ADXIndicator(high=high, low=low, close=close, window=ADX_PERIOD).adx()

    vals = [rsi.iloc[-1], ema20.iloc[-1], ema50.iloc[-1], ema200.iloc[-1], atr.iloc[-1], adx.iloc[-1]]
    if any(pd.isna(v) for v in vals):
        return None

    last = df.iloc[-1]
    return Ind(
        close=last["close"],
        rsi=rsi.iloc[-1],
        ema20=ema20.iloc[-1],
        ema50=ema50.iloc[-1],
        ema200=ema200.iloc[-1],
        atr=atr.iloc[-1],
        adx=adx.iloc[-1],
    )

def trend_up(ind: Ind) -> bool:
    return ind.ema20 > ind.ema50 > ind.ema200 and ind.close > ind.ema20

def trend_down(ind: Ind) -> bool:
    return ind.ema20 < ind.ema50 < ind.ema200 and ind.close < ind.ema20

def bollinger_squeeze(df: pd.DataFrame):
    if df is None or len(df) < 40:
        return False

    bb = BollingerBands(close=df["close"], window=20, window_dev=2)
    mid = bb.bollinger_mavg()
    upper = bb.bollinger_hband()
    lower = bb.bollinger_lband()

    width = ((upper - lower) / mid.replace(0, pd.NA)).dropna()
    if len(width) < 20:
        return False

    tail = width.iloc[-20:]
    return float(tail.iloc[-1]) <= float(tail.quantile(BOLL_SQUEEZE_Q))

def breakout_long(df: pd.DataFrame, lookback: int = BREAKOUT_LOOKBACK):
    if df is None or len(df) < lookback + 2:
        return False
    prev_high = float(df["high"].iloc[-(lookback + 1):-1].max())
    return float(df["close"].iloc[-1]) > prev_high

def breakout_short(df: pd.DataFrame, lookback: int = BREAKOUT_LOOKBACK):
    if df is None or len(df) < lookback + 2:
        return False
    prev_low = float(df["low"].iloc[-(lookback + 1):-1].min())
    return float(df["close"].iloc[-1]) < prev_low

def volume_surge(df: pd.DataFrame, mult: float = VOLUME_SURGE_MULT):
    if df is None or len(df) < 25:
        return False
    vma = float(df["volume"].rolling(20).mean().iloc[-1])
    if vma <= 0:
        return False
    return float(df["volume"].iloc[-1]) > vma * mult

def last_candle_range_atr(df: pd.DataFrame, atr: float):
    if df is None or len(df) < 1 or atr <= 0:
        return None
    last = df.iloc[-1]
    return float(last["high"] - last["low"]) / atr

def distance_from_ema20_atr(ind: Ind):
    if ind.atr <= 0:
        return 999.0
    return abs(ind.close - ind.ema20) / ind.atr

# =========================================================
# BTC FILTER
# =========================================================
def get_btc_filter_bias():
    if not USE_BTC_FILTER:
        return "NEUTRAL", "BTC_FILTER_DISABLED"

    try:
        df = get_klines(BTC_SYMBOL, "30m", 100)
        if df is None or len(df) < 8:
            return "NEUTRAL", "BTC_FILTER_DATA_FAIL"

        closed = df.iloc[:-1].copy() if len(df) > 5 else df.copy()
        prev_close = float(closed["close"].iloc[-4])
        last_close = float(closed["close"].iloc[-1])
        move = pct_change(prev_close, last_close)

        ind = compute_ind(closed)
        if ind is None:
            return "NEUTRAL", f"BTC_FLAT:{round(move, 3)}%"

        if move >= BTC_30M_TREND_THRESHOLD and ind.close > ind.ema20:
            return "LONG", f"BTC_BULL:{round(move, 3)}%"
        elif move <= -BTC_30M_TREND_THRESHOLD and ind.close < ind.ema20:
            return "SHORT", f"BTC_BEAR:{round(move, 3)}%"
        else:
            return "NEUTRAL", f"BTC_FLAT:{round(move, 3)}%"
    except Exception as e:
        return "NEUTRAL", f"BTC_FILTER_FAIL_OPEN:{e}"

# =========================================================
# RISK
# =========================================================
def calc_levels(direction: str, entry: float, atr: float):
    if direction == "LONG":
        sl = entry - (ATR_SL_MULTIPLIER * atr)
        tp1 = entry + (ATR_TP1_MULTIPLIER * atr)
        tp2 = entry + (ATR_TP2_MULTIPLIER * atr)
        tp3 = entry + (ATR_TP3_MULTIPLIER * atr)
    else:
        sl = entry + (ATR_SL_MULTIPLIER * atr)
        tp1 = entry - (ATR_TP1_MULTIPLIER * atr)
        tp2 = entry - (ATR_TP2_MULTIPLIER * atr)
        tp3 = entry - (ATR_TP3_MULTIPLIER * atr)
    return sl, tp1, tp2, tp3

def rr(direction: str, entry: float, sl: float, target: float):
    if direction == "LONG":
        risk = entry - sl
        reward = target - entry
    else:
        risk = sl - entry
        reward = entry - target

    if risk <= 0:
        return None
    return reward / risk

def fmt_price(v):
    try:
        v = float(v)
        if v >= 10000:
            return f"{v:,.2f}"
        if v >= 1000:
            return f"{v:,.2f}"
        if v >= 100:
            return f"{v:,.3f}"
        if v >= 1:
            return f"{v:,.4f}"
        return f"{v:,.6f}"
    except Exception:
        return str(v)

# =========================================================
# SIGNAL PAYLOAD
# =========================================================
def build_signal_payload(symbol, direction, entry, sl, tp1, tp2, tp3, score, strategy_tag, reason, tags):
    return {
        "symbol": symbol,
        "direction": direction.upper(),
        "entry": round(float(entry), 8),
        "sl": round(float(sl), 8),
        "tp1": round(float(tp1), 8),
        "tp2": round(float(tp2), 8),
        "tp3": round(float(tp3), 8),
        "score": round(float(score), 2),
        "strategy_tag": strategy_tag,
        "reason": reason,
        "tags": tags,
        "created_ts": now_ts(),
        "updated_ts": now_ts(),
        "status": "OPEN"
    }

def signal_key(symbol):
    return symbol.upper()

def minutes_since(ts):
    if not ts:
        return 999999
    return (now_ts() - int(ts)) / 60.0

def is_same_direction(a, b):
    return bool(a and b and a.get("direction") == b.get("direction"))

def is_opposite_direction(a, b):
    return bool(a and b and a.get("direction") != b.get("direction"))

def entry_distance_pct(sig_a, sig_b):
    if not sig_a or not sig_b:
        return 999.0
    return pct_diff(safe_float(sig_a.get("entry")), safe_float(sig_b.get("entry")))

# =========================================================
# ACTIVE SIGNAL MGMT
# =========================================================
def close_active_signal(state, symbol, close_reason, current_price=None, notify_telegram=True):
    key = signal_key(symbol)
    active = state.get("active_signal", {}).get(key)
    if not active:
        return

    active["status"] = "CLOSED"
    active["close_reason"] = close_reason
    active["closed_ts"] = now_ts()

    if current_price is not None:
        active["close_price"] = round(float(current_price), 8)

    if notify_telegram:
        tg_send(format_close_message(active, close_reason, current_price))

    state["signal_history"].append(active)
    state["active_signal"].pop(key, None)
    logger.info("Aktif sinyal kapatıldı %s: %s", symbol, close_reason)

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

def is_expired(signal):
    if not signal:
        return True
    return minutes_since(signal.get("created_ts")) >= SIGNAL_TIMEOUT_MINUTES

def refresh_active_signal_if_needed(state, symbol, current_price):
    key = signal_key(symbol)
    active = state.get("active_signal", {}).get(key)
    if not active:
        return

    if is_sl_hit(active, current_price):
        close_active_signal(state, symbol, "SL_HIT", current_price, notify_telegram=True)
        return

    if is_tp1_hit(active, current_price):
        close_active_signal(state, symbol, "TP1_HIT", current_price, notify_telegram=True)
        return

    if is_expired(active):
        close_active_signal(state, symbol, "TIMEOUT", current_price, notify_telegram=True)
        return

def recent_gap_blocked(state, symbol, new_signal):
    key = signal_key(symbol)
    last_signal = state.get("last_signal", {}).get(key)
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

def should_send_signal(state, symbol, new_signal, current_price):
    key = signal_key(symbol)
    refresh_active_signal_if_needed(state, symbol, current_price)
    active = state.get("active_signal", {}).get(key)

    if not active:
        if recent_gap_blocked(state, symbol, new_signal):
            return False, "RECENT_DUPLICATE_GAP"
        return True, "NO_ACTIVE_SIGNAL"

    active_age = minutes_since(active.get("created_ts"))
    if active_age >= MAX_ACTIVE_SIGNAL_AGE_MINUTES:
        close_active_signal(state, symbol, "MAX_ACTIVE_AGE_EXCEEDED", current_price, notify_telegram=True)
        if recent_gap_blocked(state, symbol, new_signal):
            return False, "RECENT_DUPLICATE_GAP"
        return True, "ACTIVE_TOO_OLD"

    if is_opposite_direction(active, new_signal):
        active_score = safe_float(active.get("score"), 0.0)
        new_score = safe_float(new_signal.get("score"), 0.0)

        if new_score >= active_score + REVERSE_SIGNAL_STRENGTH_BONUS:
            close_active_signal(state, symbol, "REVERSED_BY_STRONGER_SIGNAL", current_price, notify_telegram=True)
            return True, "OPPOSITE_STRONG_SIGNAL"

        return False, "OPPOSITE_SIGNAL_NOT_STRONG_ENOUGH"

    dist_pct = entry_distance_pct(active, new_signal)
    if dist_pct >= MIN_PRICE_DISTANCE_PCT:
        return True, "SAME_DIRECTION_NEW_ZONE"

    active_score = safe_float(active.get("score"), 0.0)
    new_score = safe_float(new_signal.get("score"), 0.0)
    if new_score >= active_score + SAME_DIRECTION_SCORE_BONUS:
        return True, "SAME_DIRECTION_MUCH_STRONGER"

    return False, "SIMILAR_ACTIVE_SIGNAL_STILL_OPEN"

def register_sent_signal(state, symbol, sent_signal):
    key = signal_key(symbol)
    sent_signal["created_ts"] = now_ts()
    sent_signal["updated_ts"] = now_ts()
    sent_signal["status"] = "OPEN"
    state["active_signal"][key] = sent_signal
    state["last_signal"][key] = sent_signal

# =========================================================
# STRATEGY
# =========================================================
def evaluate_symbol(symbol: str):
    df_4h = get_klines(symbol, TF_TREND, LIMIT_4H)
    df_1h = get_klines(symbol, TF_SETUP, LIMIT_1H)
    df_30m = get_klines(symbol, TF_ENTRY, LIMIT_30M)

    if df_4h is None or df_1h is None or df_30m is None:
        return None, None, "DATA_FAIL"

    ind_4h = compute_ind(df_4h)
    ind_1h = compute_ind(df_1h)
    ind_30m = compute_ind(df_30m)

    if not ind_4h or not ind_1h or not ind_30m:
        return None, None, "IND_FAIL"

    current_price = ind_30m.close
    btc_bias, btc_reason = get_btc_filter_bias()

    trigger_breakout_long = breakout_long(df_30m, BREAKOUT_LOOKBACK)
    trigger_breakout_short = breakout_short(df_30m, BREAKOUT_LOOKBACK)
    trigger_volume = volume_surge(df_30m, VOLUME_SURGE_MULT)
    trigger_squeeze = bollinger_squeeze(df_30m)

    last_range_atr = last_candle_range_atr(df_30m, ind_30m.atr)
    if last_range_atr is not None and last_range_atr > MAX_LAST_CANDLE_RANGE_ATR:
        return None, current_price, "LAST_BAR_TOO_WIDE"

    if distance_from_ema20_atr(ind_30m) > MAX_DISTANCE_FROM_EMA20_ATR:
        return None, current_price, "TOO_FAR_FROM_EMA20"

    long_score = 0.0
    short_score = 0.0
    long_reasons = []
    short_reasons = []
    long_tags = []
    short_tags = []

    # 4H TREND
    trend_4h_long_ok = (
        trend_up(ind_4h)
        and ind_4h.adx >= MIN_4H_ADX
        and MIN_4H_RSI_LONG <= ind_4h.rsi <= MAX_4H_RSI_LONG
    )
    trend_4h_short_ok = (
        trend_down(ind_4h)
        and ind_4h.adx >= MIN_4H_ADX
        and MIN_4H_RSI_SHORT <= ind_4h.rsi <= MAX_4H_RSI_SHORT
    )

    if trend_4h_long_ok:
        long_score += 3.0
        long_reasons.append("4h trend bullish")
    if trend_4h_short_ok:
        short_score += 3.0
        short_reasons.append("4h trend bearish")

    if ind_4h.adx >= 22:
        if trend_4h_long_ok:
            long_score += 1.0
            long_reasons.append("4h adx strong")
        if trend_4h_short_ok:
            short_score += 1.0
            short_reasons.append("4h adx strong")

    # 1H SETUP
    setup_1h_long_ok = (
        ind_1h.close > ind_1h.ema50
        and ind_1h.ema20 > ind_1h.ema50 > ind_1h.ema200
        and ind_1h.adx >= MIN_1H_ADX
        and MIN_1H_RSI_LONG <= ind_1h.rsi <= MAX_1H_RSI_LONG
    )
    setup_1h_short_ok = (
        ind_1h.close < ind_1h.ema50
        and ind_1h.ema20 < ind_1h.ema50 < ind_1h.ema200
        and ind_1h.adx >= MIN_1H_ADX
        and MIN_1H_RSI_SHORT <= ind_1h.rsi <= MAX_1H_RSI_SHORT
    )

    if setup_1h_long_ok:
        long_score += 3.0
        long_reasons.append("1h setup aligned")
    if setup_1h_short_ok:
        short_score += 3.0
        short_reasons.append("1h setup aligned")

    if ind_1h.close > ind_1h.ema20 and setup_1h_long_ok:
        long_score += 1.0
        long_reasons.append("1h close above ema20")
    if ind_1h.close < ind_1h.ema20 and setup_1h_short_ok:
        short_score += 1.0
        short_reasons.append("1h close below ema20")

    # BTC FILTER
    if btc_bias == "LONG":
        long_score += 1.0
        short_score -= 0.75
        long_reasons.append("btc supports long")
    elif btc_bias == "SHORT":
        short_score += 1.0
        long_score -= 0.75
        short_reasons.append("btc supports short")
    else:
        long_score -= 0.20
        short_score -= 0.20

    # 30M ENTRY
    entry_30m_long_ok = (
        ind_30m.close > ind_30m.ema20 > ind_30m.ema50 > ind_30m.ema200
        and MIN_30M_RSI_LONG <= ind_30m.rsi <= MAX_30M_RSI_LONG
    )
    entry_30m_short_ok = (
        ind_30m.close < ind_30m.ema20 < ind_30m.ema50 < ind_30m.ema200
        and MIN_30M_RSI_SHORT <= ind_30m.rsi <= MAX_30M_RSI_SHORT
    )

    if entry_30m_long_ok:
        long_score += 2.0
        long_reasons.append("30m entry aligned")
    if entry_30m_short_ok:
        short_score += 2.0
        short_reasons.append("30m entry aligned")

    if trigger_breakout_long:
        long_score += 2.0
        long_reasons.append("30m breakout long")
        long_tags.append("Breakout")
    if trigger_breakout_short:
        short_score += 2.0
        short_reasons.append("30m breakdown short")
        short_tags.append("Breakdown")

    if trigger_volume:
        if entry_30m_long_ok:
            long_score += 1.0
            long_reasons.append("30m volume confirm")
            long_tags.append("Hacim")
        if entry_30m_short_ok:
            short_score += 1.0
            short_reasons.append("30m volume confirm")
            short_tags.append("Hacim")

    if trigger_squeeze:
        if entry_30m_long_ok:
            long_score += 1.0
            long_reasons.append("30m squeeze release")
            long_tags.append("Sıkışma")
        if entry_30m_short_ok:
            short_score += 1.0
            short_reasons.append("30m squeeze release")
            short_tags.append("Sıkışma")

    candidates = []

    # LONG CANDIDATE
    if trend_4h_long_ok and setup_1h_long_ok and entry_30m_long_ok:
        if trigger_breakout_long or (trigger_volume and trigger_squeeze):
            entry = current_price
            sl, tp1, tp2, tp3 = calc_levels("LONG", entry, ind_30m.atr)
            rr2 = rr("LONG", entry, sl, tp2)

            if rr2 is not None and rr2 >= MIN_RR_TO_TP2 and long_score >= MIN_SCORE_SEND:
                candidates.append(build_signal_payload(
                    symbol=symbol,
                    direction="LONG",
                    entry=entry,
                    sl=sl,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                    score=long_score,
                    strategy_tag="PROPP_4H_1H_30M_LONGSHORT_V1",
                    reason=" | ".join(long_reasons[:10]) + f" | {btc_reason} | RR2:{round(rr2, 2)}",
                    tags=list(dict.fromkeys(long_tags))
                ))

    # SHORT CANDIDATE
    if trend_4h_short_ok and setup_1h_short_ok and entry_30m_short_ok:
        if trigger_breakout_short or (trigger_volume and trigger_squeeze):
            entry = current_price
            sl, tp1, tp2, tp3 = calc_levels("SHORT", entry, ind_30m.atr)
            rr2 = rr("SHORT", entry, sl, tp2)

            if rr2 is not None and rr2 >= MIN_RR_TO_TP2 and short_score >= MIN_SCORE_SEND:
                candidates.append(build_signal_payload(
                    symbol=symbol,
                    direction="SHORT",
                    entry=entry,
                    sl=sl,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                    score=short_score,
                    strategy_tag="PROPP_4H_1H_30M_LONGSHORT_V1",
                    reason=" | ".join(short_reasons[:10]) + f" | {btc_reason} | RR2:{round(rr2, 2)}",
                    tags=list(dict.fromkeys(short_tags))
                ))

    if not candidates:
        return None, current_price, f"NO_VALID_SIGNAL | long={round(long_score, 2)} short={round(short_score, 2)}"

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[0], current_price, f"SIGNAL_READY | long={round(long_score, 2)} short={round(short_score, 2)}"

# =========================================================
# MESSAGE FORMAT
# =========================================================
def format_signal_message(sig, current_price):
    emoji = "🟢" if sig["direction"] == "LONG" else "🔴"
    tags = ", ".join(sig.get("tags", [])) if sig.get("tags") else "Normal"

    return (
        f"{emoji} {sig['symbol']} {sig['direction']} SİNYAL\n"
        f"Fiyat: {fmt_price(current_price)}\n"
        f"Entry: {fmt_price(sig['entry'])}\n"
        f"SL: {fmt_price(sig['sl'])}\n"
        f"TP1: {fmt_price(sig['tp1'])}\n"
        f"TP2: {fmt_price(sig['tp2'])}\n"
        f"TP3: {fmt_price(sig['tp3'])}\n"
        f"Score: {sig['score']}\n"
        f"Etiket: {tags}\n"
        f"Setup: {sig['strategy_tag']}\n"
        f"Neden: {sig['reason']}\n"
        f"Zaman: {now_str()}"
    )

def format_close_message(sig, close_reason, close_price=None):
    direction_emoji = "🟢" if sig["direction"] == "LONG" else "🔴"
    reason_map = {
        "SL_HIT": "Stop oldu",
        "TP1_HIT": "TP1 görüldü",
        "TIMEOUT": "Süre doldu",
        "MAX_ACTIVE_AGE_EXCEEDED": "Maks aktif süre doldu",
        "REVERSED_BY_STRONGER_SIGNAL": "Daha güçlü ters sinyal geldi",
    }
    reason_text = reason_map.get(close_reason, close_reason)

    body = (
        f"✅ {sig['symbol']} SİNYAL KAPANDI\n"
        f"Yön: {direction_emoji} {sig['direction']}\n"
        f"Entry: {fmt_price(sig.get('entry'))}\n"
        f"SL: {fmt_price(sig.get('sl'))}\n"
        f"TP1: {fmt_price(sig.get('tp1'))}\n"
        f"TP2: {fmt_price(sig.get('tp2'))}\n"
        f"TP3: {fmt_price(sig.get('tp3'))}\n"
        f"Kapanış nedeni: {reason_text}\n"
    )

    if close_price is not None:
        body += f"Kapanış fiyatı: {fmt_price(close_price)}\n"

    body += f"İlk score: {sig.get('score')}\nZaman: {now_str()}"
    return body

def format_top_summary(sent_signals):
    ts = now_dt().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"🚀 CRYPTO PRO++ TOP SİNYALLER — {ts} TR",
        f"🧠 Kurgu: 4H trend + 1H setup + 30M giriş + aktif sinyal yönetimi",
        f"📊 Market: {MARKET_TYPE.upper()}",
        ""
    ]

    for i, sig in enumerate(sent_signals, start=1):
        tags = ", ".join(sig.get("tags", [])) if sig.get("tags") else "Normal"
        lines.append(
            f"{i}) {sig['symbol']} {sig['direction']}\n"
            f"   Score: {sig['score']}\n"
            f"   Entry: {fmt_price(sig['entry'])}\n"
            f"   SL: {fmt_price(sig['sl'])}\n"
            f"   TP1: {fmt_price(sig['tp1'])}\n"
            f"   TP2: {fmt_price(sig['tp2'])}\n"
            f"   TP3: {fmt_price(sig['tp3'])}\n"
            f"   Etiket: {tags}"
        )
        lines.append("")

    lines.append("⚠️ Teknik taramadır, yatırım tavsiyesi değildir.")
    return "\n".join(lines)

# =========================================================
# RUN
# =========================================================
def run_once():
    state = load_state()
    raw_candidates = []

    for symbol in SYMBOLS:
        try:
            sig, current_price, info = evaluate_symbol(symbol)
            refresh_active_signal_if_needed(state, symbol, current_price)

            if sig:
                can_send, reason_code = should_send_signal(state, symbol, sig, current_price)
                if can_send:
                    raw_candidates.append((sig, current_price, reason_code))
                else:
                    logger.info(
                        "Sinyal var ama gönderilmedi | %s | %s | score=%s | %s",
                        symbol, sig["direction"], sig["score"], reason_code
                    )
            else:
                logger.info("%s valid sinyal yok. %s", symbol, info)

        except Exception as e:
            logger.exception("Sembol analiz exception %s: %s", symbol, e)

        time.sleep(0.30)

    if not raw_candidates:
        save_state(state)
        return

    raw_candidates.sort(key=lambda x: x[0]["score"], reverse=True)
    selected = raw_candidates[:TOP_N_SIGNALS]

    sent_list = []

    for sig, current_price, reason_code in selected:
        message = format_signal_message(sig, current_price)
        sent = tg_send(message)

        if sent:
            register_sent_signal(state, sig["symbol"], sig)
            sent_list.append(sig)
            logger.info(
                "YENİ SİNYAL GÖNDERİLDİ | %s | %s | entry=%s sl=%s tp1=%s tp2=%s tp3=%s score=%s | %s",
                sig["symbol"], sig["direction"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"], sig["tp3"], sig["score"], reason_code
            )
        else:
            logger.warning("Telegram gönderimi başarısız: %s", sig["symbol"])

    if sent_list:
        tg_send(format_top_summary(sent_list))

    save_state(state)

def send_startup_message():
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        tg_send(
            f"🤖 CRYPTO PRO++ BOT başladı.\n"
            f"Zaman: {now_str()}\n"
            f"Pariteler: {', '.join(SYMBOLS)}\n"
            f"Kurgu: 4H trend + 1H setup + 30M giriş\n"
            f"Market: {MARKET_TYPE}"
        )
        logger.info("Başlangıç mesajı gönderildi.")
    else:
        logger.warning("Başlangıç mesajı gönderilemedi: Telegram ENV eksik.")

def main():
    logger.info("Bot başlıyor...")
    send_startup_message()

    while True:
        try:
            logger.info("Analiz yapılıyor...")
            run_once()
        except KeyboardInterrupt:
            logger.info("Bot durduruldu.")
            break
        except Exception as e:
            logger.exception("Ana döngü hatası: %s", e)
            tg_send(f"❌ Bot ana döngü hatası: {e}")

        time.sleep(CHECK_EVERY_SECONDS)

if __name__ == "__main__":
    main()
