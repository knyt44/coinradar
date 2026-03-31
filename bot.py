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

# =========================================================
# ENV
# =========================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# =========================================================
# HTTP
# =========================================================
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "crypto-pro-futures-debug-bot/3.0"})

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
LOG_FILE = "crypto_pro_futures_debug.log"
STATE_FILE = "crypto_pro_futures_debug_state.json"

BASE_URL_SPOT = "https://api.binance.com"
BASE_URL_FUTURES = "https://fapi.binance.com"

MARKET_TYPE = "futures"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "PAXGUSDT",
]

BTC_SYMBOL = "BTCUSDT"

CHECK_EVERY_SECONDS = 60

TF_TREND = "4h"
TF_SETUP = "1h"
TF_ENTRY = "30m"

LIMIT_4H = 320
LIMIT_1H = 420
LIMIT_30M = 520

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

MIN_30M_RSI_LONG = 45
MAX_30M_RSI_LONG = 66
MIN_30M_RSI_SHORT = 34
MAX_30M_RSI_SHORT = 55

VOLUME_SURGE_MULT = 1.12
BOLL_SQUEEZE_Q = 0.30
BREAKOUT_LOOKBACK = 20

ATR_SL_MULTIPLIER = 1.25
ATR_TP1_MULTIPLIER = 1.50
ATR_TP2_MULTIPLIER = 2.40
ATR_TP3_MULTIPLIER = 3.40

MIN_RR_TO_TP2 = 1.10
MAX_LAST_CANDLE_RANGE_ATR = 2.40
MAX_DISTANCE_FROM_EMA20_ATR = 2.10
MAX_BREAKOUT_WICK_BODY_RATIO = 2.60
MIN_BREAKOUT_BODY_ATR = 0.15

USE_BTC_FILTER = True
BTC_30M_TREND_THRESHOLD = 0.10

MIN_SIGNAL_GAP_MINUTES = 35
MAX_ACTIVE_SIGNAL_AGE_MINUTES = 240
SIGNAL_TIMEOUT_MINUTES = 240
MIN_PRICE_DISTANCE_PCT = 0.55
REVERSE_SIGNAL_STRENGTH_BONUS = 1.75
SAME_DIRECTION_SCORE_BONUS = 2.50

TOP_N_SIGNALS = 3
MIN_SCORE_SEND = 9

# debug / summary
SUMMARY_EVERY_MINUTES = 120
SEND_EMPTY_STATUS = True

# =========================================================
# LOGGING
# =========================================================
logger = logging.getLogger("crypto_pro_futures_debug_bot")
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
        "last_summary_ts": 0,
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
            data.setdefault("last_summary_ts", 0)
            return data
    except Exception as e:
        logger.exception("State load exception: %s", e)
        return default_state()

def save_state(state):
    tmp_file = STATE_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, STATE_FILE)

def minutes_since(ts):
    if not ts:
        return 999999
    return (now_ts() - int(ts)) / 60.0

# =========================================================
# MARKET DATA
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

# =========================================================
# CANDLE HELPERS
# =========================================================
def candle_parts(row):
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])
    body = abs(c - o)
    rng = max(h - l, 1e-12)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return o, h, l, c, body, rng, upper_wick, lower_wick

def bullish_engulf(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 3:
        return False

    a = df.iloc[-2]
    b = df.iloc[-1]
    ao, _, _, ac, abody, _, _, _ = candle_parts(a)
    bo, _, _, bc, bbody, _, _, _ = candle_parts(b)

    if ac >= ao:
        return False
    if bc <= bo:
        return False
    if bbody <= abody:
        return False

    prev_body_low = min(ao, ac)
    prev_body_high = max(ao, ac)
    curr_body_low = min(bo, bc)
    curr_body_high = max(bo, bc)

    return curr_body_low <= prev_body_low and curr_body_high >= prev_body_high

def bearish_engulf(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 3:
        return False

    a = df.iloc[-2]
    b = df.iloc[-1]
    ao, _, _, ac, abody, _, _, _ = candle_parts(a)
    bo, _, _, bc, bbody, _, _, _ = candle_parts(b)

    if ac <= ao:
        return False
    if bc >= bo:
        return False
    if bbody <= abody:
        return False

    prev_body_low = min(ao, ac)
    prev_body_high = max(ao, ac)
    curr_body_low = min(bo, bc)
    curr_body_high = max(bo, bc)

    return curr_body_low <= prev_body_low and curr_body_high >= prev_body_high

def bullish_pinbar(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 1:
        return False
    row = df.iloc[-1]
    o, h, l, c, body, rng, upper_wick, lower_wick = candle_parts(row)
    if rng <= 0:
        return False
    return lower_wick >= body * 2.0 and upper_wick <= body * 1.2 and c > o

def bearish_pinbar(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 1:
        return False
    row = df.iloc[-1]
    o, h, l, c, body, rng, upper_wick, lower_wick = candle_parts(row)
    if rng <= 0:
        return False
    return upper_wick >= body * 2.0 and lower_wick <= body * 1.2 and c < o

# =========================================================
# FILTER HELPERS
# =========================================================
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

def breakout_quality_long(df: pd.DataFrame, atr: float):
    if df is None or len(df) < BREAKOUT_LOOKBACK + 2 or atr <= 0:
        return False, "NO_DATA"

    last = df.iloc[-1]
    _, _, _, c, body, _, upper_wick, _ = candle_parts(last)
    wick_body_ratio = upper_wick / max(body, 1e-12)
    body_atr = body / atr if atr > 0 else 0.0
    prev_high = float(df["high"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].max())

    if c <= prev_high:
        return False, "CLOSE_NOT_ABOVE_BREAKOUT"
    if wick_body_ratio > MAX_BREAKOUT_WICK_BODY_RATIO:
        return False, "UPPER_WICK_TOO_HIGH"
    if body_atr < MIN_BREAKOUT_BODY_ATR:
        return False, "BODY_TOO_SMALL"
    return True, "OK"

def breakout_quality_short(df: pd.DataFrame, atr: float):
    if df is None or len(df) < BREAKOUT_LOOKBACK + 2 or atr <= 0:
        return False, "NO_DATA"

    last = df.iloc[-1]
    _, _, _, c, body, _, _, lower_wick = candle_parts(last)
    wick_body_ratio = lower_wick / max(body, 1e-12)
    body_atr = body / atr if atr > 0 else 0.0
    prev_low = float(df["low"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].min())

    if c >= prev_low:
        return False, "CLOSE_NOT_BELOW_BREAKDOWN"
    if wick_body_ratio > MAX_BREAKOUT_WICK_BODY_RATIO:
        return False, "LOWER_WICK_TOO_HIGH"
    if body_atr < MIN_BREAKOUT_BODY_ATR:
        return False, "BODY_TOO_SMALL"
    return True, "OK"

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
    result = {
        "symbol": symbol,
        "ok": False,
        "current_price": None,
        "info": "",
        "long_score": 0.0,
        "short_score": 0.0,
        "signal": None,
        "debug": {},
    }

    df_4h = get_klines(symbol, TF_TREND, LIMIT_4H)
    df_1h = get_klines(symbol, TF_SETUP, LIMIT_1H)
    df_30m = get_klines(symbol, TF_ENTRY, LIMIT_30M)

    if df_4h is None or df_1h is None or df_30m is None:
        result["info"] = "DATA_FAIL"
        return result

    ind_4h = compute_ind(df_4h)
    ind_1h = compute_ind(df_1h)
    ind_30m = compute_ind(df_30m)

    if not ind_4h or not ind_1h or not ind_30m:
        result["info"] = "IND_FAIL"
        return result

    current_price = ind_30m.close
    result["current_price"] = current_price

    btc_bias, btc_reason = get_btc_filter_bias()

    trigger_breakout_long = breakout_long(df_30m, BREAKOUT_LOOKBACK)
    trigger_breakout_short = breakout_short(df_30m, BREAKOUT_LOOKBACK)
    trigger_volume = volume_surge(df_30m, VOLUME_SURGE_MULT)
    trigger_squeeze = bollinger_squeeze(df_30m)

    engulf_long = bullish_engulf(df_30m)
    engulf_short = bearish_engulf(df_30m)
    pinbar_long = bullish_pinbar(df_30m)
    pinbar_short = bearish_pinbar(df_30m)

    breakout_long_ok, breakout_long_reason = breakout_quality_long(df_30m, ind_30m.atr)
    breakout_short_ok, breakout_short_reason = breakout_quality_short(df_30m, ind_30m.atr)

    last_range_atr = last_candle_range_atr(df_30m, ind_30m.atr)
    dist_ema20_atr = distance_from_ema20_atr(ind_30m)

    long_score = 0.0
    short_score = 0.0
    long_reasons = []
    short_reasons = []
    long_tags = []
    short_tags = []

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

    if engulf_long:
        long_score += 2.0
        long_reasons.append("bullish engulf")
        long_tags.append("Engulf")
    if engulf_short:
        short_score += 2.0
        short_reasons.append("bearish engulf")
        short_tags.append("Engulf")

    if pinbar_long:
        long_score += 1.0
        long_reasons.append("bullish pinbar")
        long_tags.append("Pinbar")
    if pinbar_short:
        short_score += 1.0
        short_reasons.append("bearish pinbar")
        short_tags.append("Pinbar")

    if trigger_breakout_long and breakout_long_ok:
        long_score += 2.0
        long_reasons.append("30m breakout valid")
        long_tags.append("Breakout")
    elif trigger_breakout_long and not breakout_long_ok:
        long_score -= 1.0
        long_reasons.append(f"fake long risk:{breakout_long_reason}")
        long_tags.append("FakeRisk")

    if trigger_breakout_short and breakout_short_ok:
        short_score += 2.0
        short_reasons.append("30m breakdown valid")
        short_tags.append("Breakdown")
    elif trigger_breakout_short and not breakout_short_ok:
        short_score -= 1.0
        short_reasons.append(f"fake short risk:{breakout_short_reason}")
        short_tags.append("FakeRisk")

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

    result["long_score"] = round(long_score, 2)
    result["short_score"] = round(short_score, 2)

    result["debug"] = {
        "btc_bias": btc_bias,
        "btc_reason": btc_reason,
        "trend_4h_long_ok": trend_4h_long_ok,
        "trend_4h_short_ok": trend_4h_short_ok,
        "setup_1h_long_ok": setup_1h_long_ok,
        "setup_1h_short_ok": setup_1h_short_ok,
        "entry_30m_long_ok": entry_30m_long_ok,
        "entry_30m_short_ok": entry_30m_short_ok,
        "engulf_long": engulf_long,
        "engulf_short": engulf_short,
        "pinbar_long": pinbar_long,
        "pinbar_short": pinbar_short,
        "breakout_long": trigger_breakout_long,
        "breakout_short": trigger_breakout_short,
        "breakout_long_ok": breakout_long_ok,
        "breakout_short_ok": breakout_short_ok,
        "breakout_long_reason": breakout_long_reason,
        "breakout_short_reason": breakout_short_reason,
        "trigger_volume": trigger_volume,
        "trigger_squeeze": trigger_squeeze,
        "last_range_atr": round(last_range_atr, 3) if last_range_atr is not None else None,
        "dist_ema20_atr": round(dist_ema20_atr, 3) if dist_ema20_atr is not None else None,
        "rsi_4h": round(ind_4h.rsi, 2),
        "rsi_1h": round(ind_1h.rsi, 2),
        "rsi_30m": round(ind_30m.rsi, 2),
        "adx_4h": round(ind_4h.adx, 2),
        "adx_1h": round(ind_1h.adx, 2),
        "price": round(current_price, 6),
    }

    if last_range_atr is not None and last_range_atr > MAX_LAST_CANDLE_RANGE_ATR:
        result["info"] = f"LAST_BAR_TOO_WIDE atr={round(last_range_atr, 2)}"
        return result

    if dist_ema20_atr > MAX_DISTANCE_FROM_EMA20_ATR:
        result["info"] = f"TOO_FAR_FROM_EMA20 atr={round(dist_ema20_atr, 2)}"
        return result

    candidates = []

    long_trigger_ok = (
        (trigger_breakout_long and breakout_long_ok)
        or engulf_long
        or (trigger_volume and trigger_squeeze and entry_30m_long_ok)
    )
    short_trigger_ok = (
        (trigger_breakout_short and breakout_short_ok)
        or engulf_short
        or (trigger_volume and trigger_squeeze and entry_30m_short_ok)
    )

    if trend_4h_long_ok and setup_1h_long_ok and entry_30m_long_ok and long_trigger_ok:
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
                strategy_tag="FUTURES_DEBUG_4H_1H_30M_ENGULF_FAKEFILTER_V3",
                reason=" | ".join(long_reasons[:12]) + f" | {btc_reason} | RR2:{round(rr2, 2)}",
                tags=list(dict.fromkeys(long_tags))
            ))
        else:
            result["info"] = f"LONG_BLOCKED rr2={round(rr2, 2) if rr2 is not None else 'NA'} score={round(long_score,2)}"
    elif trend_4h_short_ok and setup_1h_short_ok and entry_30m_short_ok and short_trigger_ok:
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
                strategy_tag="FUTURES_DEBUG_4H_1H_30M_ENGULF_FAKEFILTER_V3",
                reason=" | ".join(short_reasons[:12]) + f" | {btc_reason} | RR2:{round(rr2, 2)}",
                tags=list(dict.fromkeys(short_tags))
            ))
        else:
            result["info"] = f"SHORT_BLOCKED rr2={round(rr2, 2) if rr2 is not None else 'NA'} score={round(short_score,2)}"
    else:
        blockers = []
        if not trend_4h_long_ok and not trend_4h_short_ok:
            blockers.append("4H_TREND_FAIL")
        if not setup_1h_long_ok and not setup_1h_short_ok:
            blockers.append("1H_SETUP_FAIL")
        if not entry_30m_long_ok and not entry_30m_short_ok:
            blockers.append("30M_ENTRY_FAIL")
        if not long_trigger_ok and not short_trigger_ok:
            blockers.append("TRIGGER_FAIL")

        if not blockers:
            blockers.append("NO_CANDIDATE")
        result["info"] = " | ".join(blockers)

    if not candidates:
        return result

    candidates.sort(key=lambda x: x["score"], reverse=True)
    result["ok"] = True
    result["signal"] = candidates[0]
    result["info"] = "SIGNAL_READY"
    return result

# =========================================================
# MESSAGE FORMAT
# =========================================================
def format_signal_message(sig, current_price):
    emoji = "🟢" if sig["direction"] == "LONG" else "🔴"
    tags = ", ".join(sig.get("tags", [])) if sig.get("tags") else "Normal"

    return (
        f"{emoji} {sig['symbol']} {sig['direction']} FUTURES SİNYAL\n"
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

def format_status_summary(scan_results):
    ts = now_dt().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"📊 CRYPTO PRO FUTURES DURUM ÖZETİ — {ts} TR",
        f"Market: {MARKET_TYPE}",
        f"Kurgu: 4H trend + 1H setup + 30M entry + engulf + fake filter",
        ""
    ]

    for r in scan_results:
        dbg = r.get("debug", {})
        lines.append(
            f"{r['symbol']}\n"
            f"  Fiyat: {fmt_price(r.get('current_price'))}\n"
            f"  Long skor: {r.get('long_score')} | Short skor: {r.get('short_score')}\n"
            f"  Durum: {r.get('info')}\n"
            f"  BTC: {dbg.get('btc_bias', '-')}\n"
            f"  4H L/S: {dbg.get('trend_4h_long_ok', False)}/{dbg.get('trend_4h_short_ok', False)}\n"
            f"  1H L/S: {dbg.get('setup_1h_long_ok', False)}/{dbg.get('setup_1h_short_ok', False)}\n"
            f"  30M L/S: {dbg.get('entry_30m_long_ok', False)}/{dbg.get('entry_30m_short_ok', False)}\n"
            f"  Engulf L/S: {dbg.get('engulf_long', False)}/{dbg.get('engulf_short', False)}\n"
            f"  Breakout L/S: {dbg.get('breakout_long', False)}/{dbg.get('breakout_short', False)}\n"
            f"  Volume: {dbg.get('trigger_volume', False)} | Squeeze: {dbg.get('trigger_squeeze', False)}"
        )
        lines.append("")

    lines.append("⚠️ Bu bir durum raporudur.")
    return "\n".join(lines)

def should_send_summary(state):
    last_summary_ts = state.get("last_summary_ts", 0)
    return minutes_since(last_summary_ts) >= SUMMARY_EVERY_MINUTES

# =========================================================
# RUN
# =========================================================
def run_once():
    state = load_state()
    raw_candidates = []
    scan_results = []

    for symbol in SYMBOLS:
        try:
            r = evaluate_symbol(symbol)
            scan_results.append(r)

            current_price = r.get("current_price")
            if current_price is not None:
                refresh_active_signal_if_needed(state, symbol, current_price)

            if r.get("ok") and r.get("signal"):
                sig = r["signal"]
                can_send, reason_code = should_send_signal(state, symbol, sig, current_price)
                if can_send:
                    raw_candidates.append((sig, current_price, reason_code))
                else:
                    logger.info(
                        "Sinyal var ama gönderilmedi | %s | %s | score=%s | %s",
                        symbol, sig["direction"], sig["score"], reason_code
                    )
                    r["info"] = f"SIGNAL_BLOCKED:{reason_code}"
            else:
                logger.info("%s valid sinyal yok. %s", symbol, r.get("info"))

        except Exception as e:
            logger.exception("Sembol analiz exception %s: %s", symbol, e)
            scan_results.append({
                "symbol": symbol,
                "ok": False,
                "current_price": None,
                "info": f"EXCEPTION:{e}",
                "long_score": 0.0,
                "short_score": 0.0,
                "signal": None,
                "debug": {},
            })

        time.sleep(0.30)

    if raw_candidates:
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

    if SEND_EMPTY_STATUS and should_send_summary(state):
        tg_send(format_status_summary(scan_results))
        state["last_summary_ts"] = now_ts()

    save_state(state)

def send_startup_message():
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        tg_send(
            f"🤖 CRYPTO PRO FUTURES DEBUG BOT başladı.\n"
            f"Zaman: {now_str()}\n"
            f"Pariteler: {', '.join(SYMBOLS)}\n"
            f"Kurgu: 4H trend + 1H setup + 30M entry + engulf + fake filter + durum özeti\n"
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
