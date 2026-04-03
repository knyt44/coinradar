import os
import time
import json
import hashlib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import pandas as pd

from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, ADXIndicator
from ta.volatility import AverageTrueRange
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


def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def pct_change(a: float, b: float) -> float:
    if a in (None, 0) or b is None:
        return 0.0
    return ((b / a) - 1.0) * 100.0


def fmt_price(v: float) -> str:
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


# =========================================================
# ENV
# =========================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# =========================================================
# HTTP
# =========================================================
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "mexc-pro-signal-bot/4.0"})

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
LOG_FILE = "mexc_pro_signal_bot.log"
STATE_FILE = "mexc_pro_signal_bot_state.json"
MEXC_BASE_URL = "https://api.mexc.com"

CHECK_EVERY_SECONDS = 300
TOP_N_SIGNALS = 3
MAX_SYMBOLS_TO_SCAN = 90
NO_SIGNAL_MESSAGE = False

SPECIAL_SYMBOLS = ["BTC_USDT", "ETH_USDT", "PAXG_USDT", "XRP_USDT"]
EXCLUDED_SYMBOLS = set(SPECIAL_SYMBOLS)

EXCLUDED_KEYWORDS = (
    "_USDC", "_FDUSD", "_TUSD",
    "_BULL", "_BEAR", "_UP", "_DOWN",
)
EXCLUDED_BASES = (
    "XAU", "XAUT", "GOLD", "SILVER", "XAG",
    "OIL", "UKOIL", "USOIL", "BRENT", "WTI",
    "GAS", "NATGAS", "POWER", "COPPER",
)

TF_TREND = "Hour4"
TF_SETUP = "Min60"
TF_ENTRY = "Min15"

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

MIN_4H_ADX = 16
MIN_1H_ADX = 14

MIN_4H_RSI_LONG = 48
MAX_4H_RSI_LONG = 76
MIN_4H_RSI_SHORT = 24
MAX_4H_RSI_SHORT = 52

MIN_1H_RSI_LONG = 42
MAX_1H_RSI_LONG = 72
MIN_1H_RSI_SHORT = 28
MAX_1H_RSI_SHORT = 58

MIN_15M_RSI_LONG = 43
MAX_15M_RSI_LONG = 68
MIN_15M_RSI_SHORT = 32
MAX_15M_RSI_SHORT = 59

VOLUME_SURGE_MULT = 1.12
BREAKOUT_LOOKBACK = 20

MAX_LAST_CANDLE_RANGE_ATR = 2.70
MAX_DISTANCE_FROM_EMA20_ATR = 2.60
MAX_BREAKOUT_WICK_BODY_RATIO = 2.80
MIN_BREAKOUT_BODY_ATR = 0.12

MIN_ATR_PCT = 0.0020
MAX_ATR_PCT = 0.0450

ATR_SL_MULTIPLIER = 1.20
ATR_TP1_MULTIPLIER = 1.40
ATR_TP2_MULTIPLIER = 2.20
ATR_TP3_MULTIPLIER = 3.00
MIN_RR_TO_TP2 = 1.45

FULL_MIN_SCORE = 7.5
EARLY_MIN_SCORE = 6.5

USE_BTC_FILTER = True
BTC_15M_TREND_THRESHOLD = 0.10
BLOCK_COUNTERTREND_SIGNALS = False

MAX_NEW_SIGNALS_PER_RUN_IF_BTC_BAD = 1
COOLDOWN_MINUTES_SAME_SYMBOL = 180

# =========================================================
# LOGGING
# =========================================================
logger = logging.getLogger("mexc_pro_signal_bot")
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
# STATE
# =========================================================
def default_state():
    return {
        "last_batch_hash": "",
        "last_sent_by_symbol": {},
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return default_state()
            data.setdefault("last_batch_hash", "")
            data.setdefault("last_sent_by_symbol", {})
            return data
    except Exception as e:
        logger.exception("State load error: %s", e)
        return default_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def minutes_since(ts):
    if not ts:
        return 999999
    return (now_ts() - int(ts)) / 60.0

# =========================================================
# TELEGRAM
# =========================================================
def tg_send(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram env eksik")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = HTTP.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            logger.error("Telegram error: %s", r.text[:500])
            return False
        return True
    except Exception as e:
        logger.exception("Telegram exception: %s", e)
        return False

# =========================================================
# SYMBOL HELPERS
# =========================================================
def normalize_symbol(symbol: str) -> str:
    s = str(symbol).upper().strip()
    s = s.replace("-", "_").replace("/", "_").replace(" ", "")
    if s.endswith("USDT") and not s.endswith("_USDT"):
        s = s[:-4] + "_USDT"
    while "__" in s:
        s = s.replace("__", "_")
    return s


def symbol_allowed(symbol: str) -> bool:
    symbol = normalize_symbol(symbol)
    if not symbol or not symbol.endswith("_USDT"):
        return False

    if symbol in EXCLUDED_SYMBOLS:
        return False

    if any(k in symbol for k in EXCLUDED_KEYWORDS):
        return False

    base = symbol.split("_")[0].strip()
    if not base:
        return False

    if any(base == bad or bad in base for bad in EXCLUDED_BASES):
        return False

    if len(base) > 15:
        return False

    return True

# =========================================================
# MEXC API
# =========================================================
def mexc_get(path: str, params=None):
    url = f"{MEXC_BASE_URL}{path}"
    try:
        r = HTTP.get(url, params=params, timeout=20)
        if r.status_code != 200:
            logger.error("MEXC HTTP ERROR | %s | %s | %s", path, r.status_code, r.text[:300])
            return None
        return r.json()
    except Exception as e:
        logger.exception("MEXC GET exception: %s", e)
        return None


def get_contract_detail():
    data = mexc_get("/api/v1/contract/detail")
    if not data or not isinstance(data, dict) or not data.get("success", False):
        return []
    result = data.get("data", [])
    return result if isinstance(result, list) else []


def get_active_symbols():
    details = get_contract_detail()
    if not details:
        return []

    symbols = []
    for item in details:
        if not isinstance(item, dict):
            continue

        raw_symbol = (
            item.get("symbol")
            or item.get("displayName")
            or item.get("display_name")
            or item.get("contractCode")
            or item.get("contract_code")
            or ""
        )

        symbol = normalize_symbol(raw_symbol)
        api_allowed = item.get("apiAllowed", True)
        if str(api_allowed).lower() in ("false", "0", "none", "null"):
            continue

        if symbol_allowed(symbol):
            symbols.append(symbol)

    symbols = sorted(list(set(symbols)))
    symbols = symbols[:MAX_SYMBOLS_TO_SCAN]
    logger.info("Aktif sembol sayisi (filtreli): %s", len(symbols))
    return symbols


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
# FILTER HELPERS
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
    body_atr = body / atr
    prev_high = float(df["high"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].max())
    return c > prev_high and wick_body_ratio <= MAX_BREAKOUT_WICK_BODY_RATIO and body_atr >= MIN_BREAKOUT_BODY_ATR


def breakout_quality_short(df: pd.DataFrame, atr: float):
    if df is None or len(df) < BREAKOUT_LOOKBACK + 2 or atr <= 0:
        return False
    last = df.iloc[-1]
    _, _, _, c, body, _, _, lower_wick = candle_parts(last)
    wick_body_ratio = lower_wick / max(body, 1e-12)
    body_atr = body / atr
    prev_low = float(df["low"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].min())
    return c < prev_low and wick_body_ratio <= MAX_BREAKOUT_WICK_BODY_RATIO and body_atr >= MIN_BREAKOUT_BODY_ATR

# =========================================================
# BTC FILTER
# =========================================================
def get_btc_filter_bias():
    if not USE_BTC_FILTER:
        return "NEUTRAL"

    df = get_klines("BTC_USDT", "Min15", 120)
    if df is None or len(df) < 20:
        return "NEUTRAL"

    prev_close = float(df["close"].iloc[-4])
    last_close = float(df["close"].iloc[-1])
    move = pct_change(prev_close, last_close)

    ind = compute_ind(df)
    if ind is None:
        return "NEUTRAL"

    if move >= BTC_15M_TREND_THRESHOLD and ind.close > ind.ema20 and ind.rsi >= 51:
        return "LONG"
    if move <= -BTC_15M_TREND_THRESHOLD and ind.close < ind.ema20 and ind.rsi <= 49:
        return "SHORT"
    return "NEUTRAL"

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

# =========================================================
# MESSAGE FORMAT
# =========================================================
def format_single_signal(sig: dict) -> str:
    return (
        f"{sig['direction']} {sig['symbol']}\n"
        f"Entry: {fmt_price(sig['entry'])}\n"
        f"SL: {fmt_price(sig['sl'])}\n"
        f"TP1: {fmt_price(sig['tp1'])}\n"
        f"TP2: {fmt_price(sig['tp2'])}\n"
        f"TP3: {fmt_price(sig['tp3'])}"
    )


def format_batch_message(special_signals, main_signals) -> str:
    parts = []
    for sig in special_signals:
        parts.append(format_single_signal(sig))
    for sig in main_signals:
        parts.append(format_single_signal(sig))
    return "\n\n".join(parts).strip()


def batch_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

# =========================================================
# STRATEGY
# =========================================================
def evaluate_symbol(symbol: str, btc_bias: str, relaxed: bool = False):
    df_4h = get_klines(symbol, TF_TREND, 320)
    df_1h = get_klines(symbol, TF_SETUP, 420)
    df_15m = get_klines(symbol, TF_ENTRY, 520)

    if df_4h is None or df_1h is None or df_15m is None:
        return None

    ind_4h = compute_ind(df_4h)
    ind_1h = compute_ind(df_1h)
    ind_15m = compute_ind(df_15m)

    if not ind_4h or not ind_1h or not ind_15m:
        return None

    atr_pct = ind_15m.atr / max(ind_15m.close, 1e-12)
    if atr_pct < MIN_ATR_PCT or atr_pct > MAX_ATR_PCT:
        return None

    last_range_atr = last_candle_range_atr(df_15m, ind_15m.atr)
    if last_range_atr is not None and last_range_atr > MAX_LAST_CANDLE_RANGE_ATR:
        return None

    dist_ema20_atr = distance_from_ema20_atr(ind_15m)
    if dist_ema20_atr > MAX_DISTANCE_FROM_EMA20_ATR:
        return None

    trigger_breakout_long = breakout_long(df_15m, BREAKOUT_LOOKBACK)
    trigger_breakout_short = breakout_short(df_15m, BREAKOUT_LOOKBACK)
    trigger_volume = volume_surge(df_15m, VOLUME_SURGE_MULT)
    breakout_long_ok = breakout_quality_long(df_15m, ind_15m.atr)
    breakout_short_ok = breakout_quality_short(df_15m, ind_15m.atr)

    long_score = 0.0
    short_score = 0.0

    if trend_up(ind_4h) and ind_4h.adx >= MIN_4H_ADX and MIN_4H_RSI_LONG <= ind_4h.rsi <= MAX_4H_RSI_LONG:
        long_score += 4.0
    elif ind_4h.close > ind_4h.ema20 and ind_4h.rsi >= 46:
        long_score += 1.5

    if trend_down(ind_4h) and ind_4h.adx >= MIN_4H_ADX and MIN_4H_RSI_SHORT <= ind_4h.rsi <= MAX_4H_RSI_SHORT:
        short_score += 4.0
    elif ind_4h.close < ind_4h.ema20 and ind_4h.rsi <= 54:
        short_score += 1.5

    if (
        ind_1h.close > ind_1h.ema50 and
        ind_1h.ema20 > ind_1h.ema50 > ind_1h.ema200 and
        ind_1h.adx >= MIN_1H_ADX and
        MIN_1H_RSI_LONG <= ind_1h.rsi <= MAX_1H_RSI_LONG
    ):
        long_score += 3.0

    if (
        ind_1h.close < ind_1h.ema50 and
        ind_1h.ema20 < ind_1h.ema50 < ind_1h.ema200 and
        ind_1h.adx >= MIN_1H_ADX and
        MIN_1H_RSI_SHORT <= ind_1h.rsi <= MAX_1H_RSI_SHORT
    ):
        short_score += 3.0

    entry_15m_long_ok = (
        ind_15m.close > ind_15m.ema20 > ind_15m.ema50 > ind_15m.ema200 and
        MIN_15M_RSI_LONG <= ind_15m.rsi <= MAX_15M_RSI_LONG
    )
    entry_15m_short_ok = (
        ind_15m.close < ind_15m.ema20 < ind_15m.ema50 < ind_15m.ema200 and
        MIN_15M_RSI_SHORT <= ind_15m.rsi <= MAX_15M_RSI_SHORT
    )

    if entry_15m_long_ok:
        long_score += 2.0
    if entry_15m_short_ok:
        short_score += 2.0

    if trigger_breakout_long and breakout_long_ok:
        long_score += 1.5
    if trigger_breakout_short and breakout_short_ok:
        short_score += 1.5

    if trigger_volume and entry_15m_long_ok:
        long_score += 0.75
    if trigger_volume and entry_15m_short_ok:
        short_score += 0.75

    if btc_bias == "LONG":
        long_score += 0.75
        short_score -= 0.50
    elif btc_bias == "SHORT":
        short_score += 0.75
        long_score -= 0.50

    direction = None
    score = 0.0

    if long_score >= short_score and long_score >= EARLY_MIN_SCORE:
        direction = "LONG"
        score = long_score
    elif short_score > long_score and short_score >= EARLY_MIN_SCORE:
        direction = "SHORT"
        score = short_score
    else:
        return None

    if BLOCK_COUNTERTREND_SIGNALS:
        if btc_bias == "SHORT" and direction == "LONG":
            return None
        if btc_bias == "LONG" and direction == "SHORT":
            return None

    min_score_to_use = FULL_MIN_SCORE if not relaxed else (FULL_MIN_SCORE - 1.0)
    if score < min_score_to_use:
        return None

    entry = ind_15m.close
    sl, tp1, tp2, tp3 = calc_levels(direction, entry, ind_15m.atr)
    rr_tp2 = rr(direction, entry, sl, tp2)

    min_rr = MIN_RR_TO_TP2 if not relaxed else 1.30
    if rr_tp2 is None or rr_tp2 < min_rr:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "score": round(score, 2),
    }


def evaluate_special_symbol(symbol: str, btc_bias: str):
    sig = evaluate_symbol(symbol, btc_bias, relaxed=True)
    if sig:
        return sig

    df_15m = get_klines(symbol, TF_ENTRY, 220)
    if df_15m is None:
        return None

    ind = compute_ind(df_15m)
    if not ind:
        return None

    if ind.close >= ind.ema20:
        direction = "LONG"
    else:
        direction = "SHORT"

    entry = ind.close
    sl, tp1, tp2, tp3 = calc_levels(direction, entry, ind.atr)

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "score": 6.0,
    }

# =========================================================
# MAIN
# =========================================================
def run_once(state):
    logger.info("Tarama basladi")
    btc_bias = get_btc_filter_bias()
    logger.info("BTC bias: %s", btc_bias)

    special_signals = []
    for sp in SPECIAL_SYMBOLS:
        try:
            sig = evaluate_special_symbol(sp, btc_bias)
            if sig:
                special_signals.append(sig)
        except Exception as e:
            logger.exception("Special symbol error %s: %s", sp, e)

    symbols = get_active_symbols()
    candidates = []

    for symbol in symbols:
        try:
            last_sent_ts = state["last_sent_by_symbol"].get(symbol, 0)
            if minutes_since(last_sent_ts) < COOLDOWN_MINUTES_SAME_SYMBOL:
                continue

            sig = evaluate_symbol(symbol, btc_bias, relaxed=False)
            if sig:
                candidates.append(sig)

        except Exception as e:
            logger.exception("Evaluate error %s: %s", symbol, e)

    candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)

    top_n = TOP_N_SIGNALS
    if btc_bias == "SHORT":
        top_n = min(top_n, MAX_NEW_SIGNALS_PER_RUN_IF_BTC_BAD)

    main_signals = candidates[:top_n]

    if not special_signals and not main_signals:
        logger.info("Uygun sinyal yok")
        if NO_SIGNAL_MESSAGE:
            tg_send("Uygun sinyal yok")
        return

    text = format_batch_message(special_signals, main_signals)
    current_hash = batch_hash(text)

    if current_hash == state.get("last_batch_hash", ""):
        logger.info("Ayni batch, tekrar gonderilmedi")
        return

    ok = tg_send(text)
    if ok:
        state["last_batch_hash"] = current_hash
        for sig in special_signals + main_signals:
            state["last_sent_by_symbol"][sig["symbol"]] = now_ts()
        save_state(state)
        logger.info("Sinyal batch gonderildi | special=%s | main=%s", len(special_signals), len(main_signals))


def main():
    logger.info("Bot basladi")
    state = load_state()

    while True:
        try:
            run_once(state)
        except Exception as e:
            logger.exception("Main loop error: %s", e)
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
