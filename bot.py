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
HTTP.headers.update({"User-Agent": "mexc-clean-signal-bot/1.0"})

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
LOG_FILE = "mexc_clean_signal_bot.log"
STATE_FILE = "mexc_clean_signal_bot_state.json"
MEXC_BASE_URL = "https://api.mexc.com"

REQUESTED_SYMBOLS = [
    "BTC_USDT",
    "ETH_USDT",
    "PAXG_USDT",
]
BTC_SYMBOL = "BTC_USDT"

CHECK_EVERY_SECONDS = 60

TF_TREND = "Hour4"
TF_SETUP = "Min60"
TF_ENTRY = "Min30"

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

MIN_4H_ADX = 16
MIN_1H_ADX = 14

MIN_4H_RSI_LONG = 50
MAX_4H_RSI_LONG = 76
MIN_4H_RSI_SHORT = 24
MAX_4H_RSI_SHORT = 50

MIN_1H_RSI_LONG = 45
MAX_1H_RSI_LONG = 69
MIN_1H_RSI_SHORT = 31
MAX_1H_RSI_SHORT = 55

MIN_30M_RSI_LONG = 42
MAX_30M_RSI_LONG = 67
MIN_30M_RSI_SHORT = 33
MAX_30M_RSI_SHORT = 58

VOLUME_SURGE_MULT = 1.06
BOLL_SQUEEZE_Q = 0.32
BREAKOUT_LOOKBACK = 20

ATR_SL_MULTIPLIER = 1.25
ATR_TP1_MULTIPLIER = 1.50
ATR_TP2_MULTIPLIER = 2.40
ATR_TP3_MULTIPLIER = 3.40

FULL_MIN_RR_TO_TP2 = 1.10
EARLY_MIN_RR_TO_TP2 = 1.00

MAX_LAST_CANDLE_RANGE_ATR = 2.60
MAX_DISTANCE_FROM_EMA20_ATR = 2.30
MAX_BREAKOUT_WICK_BODY_RATIO = 2.60
MIN_BREAKOUT_BODY_ATR = 0.15

USE_BTC_FILTER = True
BTC_30M_TREND_THRESHOLD = 0.08

FULL_MIN_SCORE = 9.0
EARLY_MIN_SCORE = 6.0
WATCHLIST_MIN_SCORE = 5.0

MIN_SIGNAL_GAP_MINUTES = 35
MAX_ACTIVE_SIGNAL_AGE_MINUTES = 240
SIGNAL_TIMEOUT_MINUTES = 240
MIN_PRICE_DISTANCE_PCT = 0.55
REVERSE_SIGNAL_STRENGTH_BONUS = 1.75
SAME_DIRECTION_SCORE_BONUS = 2.50

TOP_N_SIGNALS = 3
WATCHLIST_COOLDOWN_MINUTES = 180

# Telegram'a debug özeti kapalı
SEND_SUMMARY_TO_TELEGRAM = False

# =========================================================
# LOGGING
# =========================================================
logger = logging.getLogger("mexc_clean_signal_bot")
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
        "last_watchlist_sent": {},
        "active_symbols": [],
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
            data.setdefault("last_watchlist_sent", {})
            data.setdefault("active_symbols", [])
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
# MEXC API
# =========================================================
def mexc_get(path: str, params=None):
    url = f"{MEXC_BASE_URL}{path}"
    try:
        r = HTTP.get(url, params=params, timeout=20)
        if r.status_code != 200:
            logger.error("MEXC HTTP ERROR | path=%s status=%s body=%s", path, r.status_code, r.text[:400])
            return None
        return r.json()
    except Exception as e:
        logger.exception("MEXC GET exception | %s", e)
        return None

def get_contract_detail():
    data = mexc_get("/api/v1/contract/detail")
    if not data or not isinstance(data, dict):
        return []
    if not data.get("success", False):
        return []
    result = data.get("data", [])
    return result if isinstance(result, list) else []

def get_active_symbols(requested_symbols):
    details = get_contract_detail()
    if not details:
        return []

    active = []
    available = set()

    for item in details:
        symbol = str(item.get("symbol", "")).upper()
        available.add(symbol)
        api_allowed = item.get("apiAllowed", True)
        if symbol in requested_symbols and api_allowed:
            active.append(symbol)

    for s in requested_symbols:
        if s not in available:
            logger.warning("İstenen symbol bulunamadı: %s", s)

    return sorted(list(set(active)))

def get_klines(symbol: str, interval: str, bars: int = 320):
    interval_sec_map = {
        "Min1": 60,
        "Min5": 300,
        "Min15": 900,
        "Min30": 1800,
        "Min60": 3600,
        "Hour4": 14400,
        "Hour8": 28800,
        "Day1": 86400,
    }
    sec = interval_sec_map.get(interval, 60)
    end_ts = int(time.time())
    start_ts = end_ts - (bars * sec)

    data = mexc_get(
        f"/api/v1/contract/kline/{symbol}",
        params={"interval": interval, "start": start_ts, "end": end_ts}
    )
    if not data or not isinstance(data, dict) or not data.get("success", False):
        return None

    k = data.get("data", {})
    if not isinstance(k, dict):
        return None

    times = k.get("time", [])
    opens = k.get("open", [])
    highs = k.get("high", [])
    lows = k.get("low", [])
    closes = k.get("close", [])
    vols = k.get("vol", k.get("volume", []))

    lens = [len(times), len(opens), len(highs), len(lows), len(closes), len(vols)]
    if min(lens) == 0 or len(set(lens)) != 1:
        return None

    rows = []
    for i in range(len(times)):
        rows.append({
            "open_time": pd.to_datetime(int(times[i]), unit="s", utc=True).tz_convert(TZ).tz_localize(None),
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
            "volume": float(vols[i]),
        })

    df = pd.DataFrame(rows)
    df.set_index("open_time", inplace=True)
    return df.sort_index()

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
    return Ind(last["close"], rsi.iloc[-1], ema20.iloc[-1], ema50.iloc[-1], ema200.iloc[-1], atr.iloc[-1], adx.iloc[-1])

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
    if ac >= ao or bc <= bo or bbody <= abody:
        return False
    return min(bo, bc) <= min(ao, ac) and max(bo, bc) >= max(ao, ac)

def bearish_engulf(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 3:
        return False
    a = df.iloc[-2]
    b = df.iloc[-1]
    ao, _, _, ac, abody, _, _, _ = candle_parts(a)
    bo, _, _, bc, bbody, _, _, _ = candle_parts(b)
    if ac <= ao or bc >= bo or bbody <= abody:
        return False
    return min(bo, bc) <= min(ao, ac) and max(bo, bc) >= max(ao, ac)

def bullish_pinbar(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 1:
        return False
    row = df.iloc[-1]
    o, h, l, c, body, rng, upper_wick, lower_wick = candle_parts(row)
    return rng > 0 and lower_wick >= body * 2.0 and upper_wick <= body * 1.2 and c > o

def bearish_pinbar(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 1:
        return False
    row = df.iloc[-1]
    o, h, l, c, body, rng, upper_wick, lower_wick = candle_parts(row)
    return rng > 0 and upper_wick >= body * 2.0 and lower_wick <= body * 1.2 and c < o

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
    return vma > 0 and float(df["volume"].iloc[-1]) > vma * mult

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
        return False
    last = df.iloc[-1]
    _, _, _, c, body, _, upper_wick, _ = candle_parts(last)
    wick_body_ratio = upper_wick / max(body, 1e-12)
    body_atr = body / atr if atr > 0 else 0.0
    prev_high = float(df["high"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].max())
    return c > prev_high and wick_body_ratio <= MAX_BREAKOUT_WICK_BODY_RATIO and body_atr >= MIN_BREAKOUT_BODY_ATR

def breakout_quality_short(df: pd.DataFrame, atr: float):
    if df is None or len(df) < BREAKOUT_LOOKBACK + 2 or atr <= 0:
        return False
    last = df.iloc[-1]
    _, _, _, c, body, _, _, lower_wick = candle_parts(last)
    wick_body_ratio = lower_wick / max(body, 1e-12)
    body_atr = body / atr if atr > 0 else 0.0
    prev_low = float(df["low"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].min())
    return c < prev_low and wick_body_ratio <= MAX_BREAKOUT_WICK_BODY_RATIO and body_atr >= MIN_BREAKOUT_BODY_ATR

# =========================================================
# BTC FILTER
# =========================================================
def get_btc_filter_bias():
    if not USE_BTC_FILTER:
        return "NEUTRAL", "BTC_FILTER_DISABLED"

    df = get_klines(BTC_SYMBOL, "Min30", 100)
    if df is None or len(df) < 8:
        return "NEUTRAL", "BTC_FILTER_DATA_FAIL"

    prev_close = float(df["close"].iloc[-4])
    last_close = float(df["close"].iloc[-1])
    move = pct_change(prev_close, last_close)
    ind = compute_ind(df)
    if ind is None:
        return "NEUTRAL", f"BTC_FLAT:{round(move, 3)}%"

    if move >= BTC_30M_TREND_THRESHOLD and ind.close > ind.ema20:
        return "LONG", f"BTC_BULL:{round(move, 3)}%"
    if move <= -BTC_30M_TREND_THRESHOLD and ind.close < ind.ema20:
        return "SHORT", f"BTC_BEAR:{round(move, 3)}%"
    return "NEUTRAL", f"BTC_FLAT:{round(move, 3)}%"

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
# SIGNAL / WATCHLIST
# =========================================================
def build_signal_payload(symbol, direction, entry, sl, tp1, tp2, tp3, score, strategy_tag, reason, signal_type):
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
        "signal_type": signal_type,
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

def is_tp1_hit(signal, current_price):
    if not signal:
        return False
    direction = signal["direction"]
    tp1 = safe_float(signal.get("tp1"))
    cp = safe_float(current_price)
    if tp1 is None or cp is None:
        return False
    return cp >= tp1 if direction == "LONG" else cp <= tp1

def is_sl_hit(signal, current_price):
    if not signal:
        return False
    direction = signal["direction"]
    sl = safe_float(signal.get("sl"))
    cp = safe_float(current_price)
    if sl is None or cp is None:
        return False
    return cp <= sl if direction == "LONG" else cp >= sl

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
        close_active_signal(state, symbol, "SL_HIT", current_price, True)
        return
    if is_tp1_hit(active, current_price):
        close_active_signal(state, symbol, "TP1_HIT", current_price, True)
        return
    if is_expired(active):
        close_active_signal(state, symbol, "TIMEOUT", current_price, True)

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
        close_active_signal(state, symbol, "MAX_ACTIVE_AGE_EXCEEDED", current_price, True)
        if recent_gap_blocked(state, symbol, new_signal):
            return False, "RECENT_DUPLICATE_GAP"
        return True, "ACTIVE_TOO_OLD"

    if is_opposite_direction(active, new_signal):
        active_score = safe_float(active.get("score"), 0.0)
        new_score = safe_float(new_signal.get("score"), 0.0)
        if new_score >= active_score + REVERSE_SIGNAL_STRENGTH_BONUS:
            close_active_signal(state, symbol, "REVERSED_BY_STRONGER_SIGNAL", current_price, True)
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
# WATCHLIST CONTROL
# =========================================================
def should_send_watchlist(state, symbol):
    ts = state.get("last_watchlist_sent", {}).get(symbol, 0)
    return minutes_since(ts) >= WATCHLIST_COOLDOWN_MINUTES

def mark_watchlist_sent(state, symbol):
    state["last_watchlist_sent"][symbol] = now_ts()

# =========================================================
# STRATEGY
# =========================================================
def evaluate_symbol(symbol: str):
    result = {
        "symbol": symbol,
        "ok": False,
        "current_price": None,
        "signal": None,
        "watchlist": None,
    }

    df_4h = get_klines(symbol, TF_TREND, 320)
    df_1h = get_klines(symbol, TF_SETUP, 420)
    df_30m = get_klines(symbol, TF_ENTRY, 520)

    if df_4h is None or df_1h is None or df_30m is None:
        logger.info("%s DATA_FAIL", symbol)
        return result

    ind_4h = compute_ind(df_4h)
    ind_1h = compute_ind(df_1h)
    ind_30m = compute_ind(df_30m)

    if not ind_4h or not ind_1h or not ind_30m:
        logger.info("%s IND_FAIL", symbol)
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

    breakout_long_ok = breakout_quality_long(df_30m, ind_30m.atr)
    breakout_short_ok = breakout_quality_short(df_30m, ind_30m.atr)

    last_range_atr = last_candle_range_atr(df_30m, ind_30m.atr)
    dist_ema20_atr = distance_from_ema20_atr(ind_30m)

    if last_range_atr is not None and last_range_atr > MAX_LAST_CANDLE_RANGE_ATR:
        logger.info("%s LAST_BAR_TOO_WIDE", symbol)
        return result

    if dist_ema20_atr > MAX_DISTANCE_FROM_EMA20_ATR:
        logger.info("%s TOO_FAR_FROM_EMA20", symbol)
        return result

    long_score = 0.0
    short_score = 0.0
    long_reasons = []
    short_reasons = []

    trend_4h_long_ok = trend_up(ind_4h) and ind_4h.adx >= MIN_4H_ADX and MIN_4H_RSI_LONG <= ind_4h.rsi <= MAX_4H_RSI_LONG
    trend_4h_short_ok = trend_down(ind_4h) and ind_4h.adx >= MIN_4H_ADX and MIN_4H_RSI_SHORT <= ind_4h.rsi <= MAX_4H_RSI_SHORT

    trend_4h_long_partial = ind_4h.close > ind_4h.ema20 and ind_4h.rsi >= 47
    trend_4h_short_partial = ind_4h.close < ind_4h.ema20 and ind_4h.rsi <= 53

    if trend_4h_long_ok:
        long_score += 3.0
        long_reasons.append("4H trend bullish")
    elif trend_4h_long_partial:
        long_score += 1.0
        long_reasons.append("4H early bullish shift")

    if trend_4h_short_ok:
        short_score += 3.0
        short_reasons.append("4H trend bearish")
    elif trend_4h_short_partial:
        short_score += 1.0
        short_reasons.append("4H early bearish shift")

    setup_1h_long_ok = (
        ind_1h.close > ind_1h.ema50 and
        ind_1h.ema20 > ind_1h.ema50 > ind_1h.ema200 and
        ind_1h.adx >= MIN_1H_ADX and
        MIN_1H_RSI_LONG <= ind_1h.rsi <= MAX_1H_RSI_LONG
    )
    setup_1h_short_ok = (
        ind_1h.close < ind_1h.ema50 and
        ind_1h.ema20 < ind_1h.ema50 < ind_1h.ema200 and
        ind_1h.adx >= MIN_1H_ADX and
        MIN_1H_RSI_SHORT <= ind_1h.rsi <= MAX_1H_RSI_SHORT
    )

    if setup_1h_long_ok:
        long_score += 3.0
        long_reasons.append("1H setup aligned")
    if setup_1h_short_ok:
        short_score += 3.0
        short_reasons.append("1H setup aligned")

    entry_30m_long_ok = (
        ind_30m.close > ind_30m.ema20 > ind_30m.ema50 > ind_30m.ema200 and
        MIN_30M_RSI_LONG <= ind_30m.rsi <= MAX_30M_RSI_LONG
    )
    entry_30m_short_ok = (
        ind_30m.close < ind_30m.ema20 < ind_30m.ema50 < ind_30m.ema200 and
        MIN_30M_RSI_SHORT <= ind_30m.rsi <= MAX_30M_RSI_SHORT
    )

    if entry_30m_long_ok:
        long_score += 2.0
        long_reasons.append("30M entry aligned")
    if entry_30m_short_ok:
        short_score += 2.0
        short_reasons.append("30M entry aligned")

    if engulf_long:
        long_score += 2.0
        long_reasons.append("bullish engulf")
    if engulf_short:
        short_score += 2.0
        short_reasons.append("bearish engulf")

    if pinbar_long:
        long_score += 1.0
        long_reasons.append("bullish pinbar")
    if pinbar_short:
        short_score += 1.0
        short_reasons.append("bearish pinbar")

    if trigger_breakout_long and breakout_long_ok:
        long_score += 2.0
        long_reasons.append("30M breakout valid")
    if trigger_breakout_short and breakout_short_ok:
        short_score += 2.0
        short_reasons.append("30M breakdown valid")

    if trigger_volume:
        if entry_30m_long_ok:
            long_score += 1.0
            long_reasons.append("30M volume confirm")
        if entry_30m_short_ok:
            short_score += 1.0
            short_reasons.append("30M volume confirm")

    if trigger_squeeze:
        if entry_30m_long_ok:
            long_score += 1.0
            long_reasons.append("30M squeeze release")
        if entry_30m_short_ok:
            short_score += 1.0
            short_reasons.append("30M squeeze release")

    if btc_bias == "LONG":
        long_score += 1.0
        short_score -= 0.75
        long_reasons.append("BTC supports long")
    elif btc_bias == "SHORT":
        short_score += 1.0
        long_score -= 0.75
        short_reasons.append("BTC supports short")
    else:
        long_score -= 0.20
        short_score -= 0.20

    long_trigger_ok = (
        (trigger_breakout_long and breakout_long_ok) or
        engulf_long or
        pinbar_long or
        (trigger_volume and trigger_squeeze and entry_30m_long_ok)
    )
    short_trigger_ok = (
        (trigger_breakout_short and breakout_short_ok) or
        engulf_short or
        pinbar_short or
        (trigger_volume and trigger_squeeze and entry_30m_short_ok)
    )

    candidates = []

    # FULL LONG
    if trend_4h_long_ok and setup_1h_long_ok and entry_30m_long_ok and long_trigger_ok:
        entry = current_price
        sl, tp1, tp2, tp3 = calc_levels("LONG", entry, ind_30m.atr)
        rr2 = rr("LONG", entry, sl, tp2)
        if rr2 is not None and rr2 >= FULL_MIN_RR_TO_TP2 and long_score >= FULL_MIN_SCORE:
            candidates.append(build_signal_payload(
                symbol, "LONG", entry, sl, tp1, tp2, tp3, long_score,
                "MEXC_CLEAN_FULL",
                " | ".join(long_reasons[:8]) + f" | {btc_reason}",
                "FULL"
            ))

    # FULL SHORT
    if trend_4h_short_ok and setup_1h_short_ok and entry_30m_short_ok and short_trigger_ok:
        entry = current_price
        sl, tp1, tp2, tp3 = calc_levels("SHORT", entry, ind_30m.atr)
        rr2 = rr("SHORT", entry, sl, tp2)
        if rr2 is not None and rr2 >= FULL_MIN_RR_TO_TP2 and short_score >= FULL_MIN_SCORE:
            candidates.append(build_signal_payload(
                symbol, "SHORT", entry, sl, tp1, tp2, tp3, short_score,
                "MEXC_CLEAN_FULL",
                " | ".join(short_reasons[:8]) + f" | {btc_reason}",
                "FULL"
            ))

    # EARLY LONG
    early_long_allowed = entry_30m_long_ok and long_trigger_ok and (setup_1h_long_ok or trend_4h_long_partial or engulf_long)
    early_long_blocked = ind_30m.rsi > 67 or dist_ema20_atr > 2.0

    if not candidates and early_long_allowed and not early_long_blocked:
        entry = current_price
        sl, tp1, tp2, tp3 = calc_levels("LONG", entry, ind_30m.atr)
        rr2 = rr("LONG", entry, sl, tp2)
        early_score = long_score + (1.0 if trend_4h_long_partial else 0.0) + (0.5 if engulf_long else 0.0)
        if rr2 is not None and rr2 >= EARLY_MIN_RR_TO_TP2 and early_score >= EARLY_MIN_SCORE:
            candidates.append(build_signal_payload(
                symbol, "LONG", entry, sl, tp1, tp2, tp3, early_score,
                "MEXC_CLEAN_EARLY",
                "EARLY | " + " | ".join(long_reasons[:8]) + f" | {btc_reason}",
                "EARLY"
            ))

    # EARLY SHORT
    early_short_allowed = entry_30m_short_ok and short_trigger_ok and (setup_1h_short_ok or trend_4h_short_partial or engulf_short)
    early_short_blocked = ind_30m.rsi < 33 or dist_ema20_atr > 2.0

    if not candidates and early_short_allowed and not early_short_blocked:
        entry = current_price
        sl, tp1, tp2, tp3 = calc_levels("SHORT", entry, ind_30m.atr)
        rr2 = rr("SHORT", entry, sl, tp2)
        early_score = short_score + (1.0 if trend_4h_short_partial else 0.0) + (0.5 if engulf_short else 0.0)
        if rr2 is not None and rr2 >= EARLY_MIN_RR_TO_TP2 and early_score >= EARLY_MIN_SCORE:
            candidates.append(build_signal_payload(
                symbol, "SHORT", entry, sl, tp1, tp2, tp3, early_score,
                "MEXC_CLEAN_EARLY",
                "EARLY | " + " | ".join(short_reasons[:8]) + f" | {btc_reason}",
                "EARLY"
            ))

    # WATCHLIST
    if setup_1h_long_ok or entry_30m_long_ok or engulf_long or trend_4h_long_partial:
        if long_score >= WATCHLIST_MIN_SCORE:
            result["watchlist"] = {
                "symbol": symbol,
                "direction": "LONG",
                "score": round(long_score, 2),
                "reason": " | ".join(long_reasons[:6]) + f" | {btc_reason}"
            }

    if result["watchlist"] is None and (setup_1h_short_ok or entry_30m_short_ok or engulf_short or trend_4h_short_partial):
        if short_score >= WATCHLIST_MIN_SCORE:
            result["watchlist"] = {
                "symbol": symbol,
                "direction": "SHORT",
                "score": round(short_score, 2),
                "reason": " | ".join(short_reasons[:6]) + f" | {btc_reason}"
            }

    if not candidates:
        logger.info(
            "%s | no signal | long=%.2f short=%.2f | 4H=%s/%s 1H=%s/%s 30M=%s/%s",
            symbol,
            long_score,
            short_score,
            trend_4h_long_ok,
            trend_4h_short_ok,
            setup_1h_long_ok,
            setup_1h_short_ok,
            entry_30m_long_ok,
            entry_30m_short_ok,
        )
        return result

    candidates.sort(key=lambda x: (x["signal_type"] == "FULL", x["score"]), reverse=True)
    result["ok"] = True
    result["signal"] = candidates[0]
    return result

# =========================================================
# MESSAGE FORMAT
# =========================================================
def format_signal_message(sig, current_price):
    emoji = "🟢" if sig["direction"] == "LONG" else "🔴"
    signal_type = sig.get("signal_type", "FULL")
    prefix = "✅ FULL" if signal_type == "FULL" else "⚡ EARLY"
    return (
        f"{emoji} {prefix} {sig['symbol']} {sig['direction']}\n"
        f"Fiyat: {fmt_price(current_price)}\n"
        f"Entry: {fmt_price(sig['entry'])}\n"
        f"SL: {fmt_price(sig['sl'])}\n"
        f"TP1: {fmt_price(sig['tp1'])}\n"
        f"TP2: {fmt_price(sig['tp2'])}\n"
        f"TP3: {fmt_price(sig['tp3'])}\n"
        f"Score: {sig['score']}\n"
        f"Neden: {sig['reason']}\n"
        f"Zaman: {now_str()}"
    )

def format_watchlist_message(w):
    return (
        f"👀 WATCHLIST {w['symbol']} {w['direction']}\n"
        f"Score: {w['score']}\n"
        f"Neden: {w['reason']}\n"
        f"Zaman: {now_str()}"
    )

def format_close_message(sig, close_reason, close_price=None):
    direction_emoji = "🟢" if sig["direction"] == "LONG" else "🔴"
    body = (
        f"✅ {sig['symbol']} SİNYAL KAPANDI\n"
        f"Yön: {direction_emoji} {sig['direction']}\n"
        f"Tip: {sig.get('signal_type', 'FULL')}\n"
        f"Entry: {fmt_price(sig.get('entry'))}\n"
        f"SL: {fmt_price(sig.get('sl'))}\n"
        f"TP1: {fmt_price(sig.get('tp1'))}\n"
        f"TP2: {fmt_price(sig.get('tp2'))}\n"
        f"TP3: {fmt_price(sig.get('tp3'))}\n"
        f"Kapanış nedeni: {close_reason}\n"
    )
    if close_price is not None:
        body += f"Kapanış fiyatı: {fmt_price(close_price)}\n"
    body += f"İlk score: {sig.get('score')}\nZaman: {now_str()}"
    return body

# =========================================================
# RUN
# =========================================================
def run_once():
    state = load_state()
    active_symbols = get_active_symbols(REQUESTED_SYMBOLS)
    state["active_symbols"] = active_symbols

    raw_candidates = []

    if not active_symbols:
        save_state(state)
        return

    for symbol in active_symbols:
        try:
            r = evaluate_symbol(symbol)
            current_price = r.get("current_price")

            if current_price is not None:
                refresh_active_signal_if_needed(state, symbol, current_price)

            if r.get("watchlist") and should_send_watchlist(state, symbol):
                tg_send(format_watchlist_message(r["watchlist"]))
                mark_watchlist_sent(state, symbol)

            if r.get("ok") and r.get("signal"):
                sig = r["signal"]
                can_send, reason_code = should_send_signal(state, symbol, sig, current_price)
                if can_send:
                    raw_candidates.append((sig, current_price, reason_code))

        except Exception as e:
            logger.exception("Sembol analiz exception %s: %s", symbol, e)

        time.sleep(0.30)

    if raw_candidates:
        raw_candidates.sort(key=lambda x: (x[0]["signal_type"] == "FULL", x[0]["score"]), reverse=True)
        selected = raw_candidates[:TOP_N_SIGNALS]

        for sig, current_price, reason_code in selected:
            if tg_send(format_signal_message(sig, current_price)):
                register_sent_signal(state, sig["symbol"], sig)
                logger.info("Sinyal gönderildi %s %s %s", sig["symbol"], sig["direction"], reason_code)

    save_state(state)

def send_startup_message(state):
    active_symbols = get_active_symbols(REQUESTED_SYMBOLS)
    state["active_symbols"] = active_symbols
    save_state(state)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        tg_send(
            f"🤖 MEXC SADE SİNYAL BOT başladı.\n"
            f"Zaman: {now_str()}\n"
            f"İstenen: {', '.join(REQUESTED_SYMBOLS)}\n"
            f"Aktif: {', '.join(active_symbols) if active_symbols else 'YOK'}\n"
            f"Mesajlar: WATCHLIST / EARLY / FULL"
        )

def main():
    logger.info("Bot başlıyor...")
    state = load_state()
    send_startup_message(state)

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.exception("Ana döngü hatası: %s", e)
            tg_send(f"❌ Bot ana döngü hatası: {e}")

        time.sleep(CHECK_EVERY_SECONDS)

if __name__ == "__main__":
    main()
