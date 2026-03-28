# -*- coding: utf-8 -*-
import os
import json
import traceback
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np

# =========================================================
# CONFIG
# =========================================================

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SYMBOL = os.getenv("SYMBOL", "XAU/USD").strip()
TIMEZONE = os.getenv("MARKET_TIMEZONE", "UTC").strip()

HTF_INTERVAL = "4h"
LTF_INTERVAL = "15min"
DAY_INTERVAL = "1day"

HTF_OUTPUTSIZE = 300
LTF_OUTPUTSIZE = 500
DAY_OUTPUTSIZE = 30

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

MIN_ADX = 18
MIN_RR = 1.8

PIVOT_LEFT = 3
PIVOT_RIGHT = 3
MAX_HTF_LEVEL_AGE = 140

LEVEL_NEAR_ATR = 0.45
LEVEL_MERGE_ATR = 0.35
SWEEP_MIN_ATR = 0.18
ATR_SL_BUFFER = 0.25
MAX_ENTRY_DISTANCE_ATR = 1.10

MIN_PIN_WICK_RATIO = 2.0
MAX_BODY_TO_RANGE_FOR_PIN = 0.42

ASIA_START = 0
ASIA_END = 6

LONDON_START = 7
LONDON_END = 16
NEWYORK_START = 12
NEWYORK_END = 21
TRAP_LOOK_WINDOW_START = 6
TRAP_LOOK_WINDOW_END = 15

STATE_FILE = "signal_state.json"

# =========================================================
# HELPERS
# =========================================================

def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def safe_float(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def now_utc():
    return datetime.now(timezone.utc)


def utc_hour():
    return now_utc().hour


def in_main_sessions():
    h = utc_hour()
    return (LONDON_START <= h < LONDON_END) or (NEWYORK_START <= h < NEWYORK_END)


def in_trap_window():
    h = utc_hour()
    return TRAP_LOOK_WINDOW_START <= h < TRAP_LOOK_WINDOW_END


def ensure_env():
    missing = []
    if not TWELVE_DATA_API_KEY:
        missing.append("TWELVE_DATA_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError(f"Eksik environment/secrets: {', '.join(missing)}")


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(msg: str) -> bool:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=20
        )
        log(f"Telegram status: {resp.status_code}")
        log(f"Telegram response: {resp.text[:400]}")
        if resp.status_code != 200:
            return False
        data = resp.json()
        return bool(data.get("ok", False))
    except Exception as e:
        log(f"Telegram hata: {e}")
        return False


# =========================================================
# TWELVE DATA
# =========================================================

def fetch_twelve_data(symbol: str, interval: str, outputsize: int) -> pd.DataFrame:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "timezone": TIMEZONE,
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON",
        "order": "ASC",
    }

    resp = requests.get(url, params=params, timeout=30)
    log(f"Twelve Data [{interval}] status: {resp.status_code}")
    log(resp.text[:500])

    if resp.status_code != 200:
        raise RuntimeError(f"Twelve Data HTTP hata: {resp.status_code}")

    data = resp.json()
    if "status" in data and data["status"] == "error":
        raise RuntimeError(f"Twelve Data API hata: {data}")

    values = data.get("values")
    if not values:
        raise RuntimeError(f"Twelve Data veri yok: {data}")

    rows = []
    for x in values:
        rows.append({
            "time": pd.to_datetime(x["datetime"], utc=True),
            "open": safe_float(x["open"]),
            "high": safe_float(x["high"]),
            "low": safe_float(x["low"]),
            "close": safe_float(x["close"]),
            "volume": safe_float(x.get("volume"))
        })

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("Boş dataframe döndü")
    return df


# =========================================================
# INDICATORS
# =========================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema20"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=EMA_MID, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(RSI_PERIOD).mean()
    avg_loss = loss.rolling(RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = df["tr"].rolling(ATR_PERIOD).mean()

    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_smooth = df["tr"].rolling(ADX_PERIOD).sum()
    plus_di = 100 * pd.Series(plus_dm).rolling(ADX_PERIOD).sum() / tr_smooth
    minus_di = 100 * pd.Series(minus_dm).rolling(ADX_PERIOD).sum() / tr_smooth
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
    df["adx"] = dx.rolling(ADX_PERIOD).mean()

    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = df["high"] - df["low"]
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]

    return df


# =========================================================
# LEVELS
# =========================================================

def is_pivot_high(df: pd.DataFrame, i: int) -> bool:
    if i - PIVOT_LEFT < 0 or i + PIVOT_RIGHT >= len(df):
        return False
    h = df.iloc[i]["high"]
    for j in range(i - PIVOT_LEFT, i + PIVOT_RIGHT + 1):
        if j == i:
            continue
        if df.iloc[j]["high"] >= h:
            return False
    return True


def is_pivot_low(df: pd.DataFrame, i: int) -> bool:
    if i - PIVOT_LEFT < 0 or i + PIVOT_RIGHT >= len(df):
        return False
    l = df.iloc[i]["low"]
    for j in range(i - PIVOT_LEFT, i + PIVOT_RIGHT + 1):
        if j == i:
            continue
        if df.iloc[j]["low"] <= l:
            return False
    return True


def merge_levels(levels, atr: float):
    if not levels:
        return []

    levels = sorted(levels, key=lambda x: x["price"])
    merged = [levels[0].copy()]
    merge_dist = atr * LEVEL_MERGE_ATR

    for lv in levels[1:]:
        if abs(lv["price"] - merged[-1]["price"]) <= merge_dist:
            total = merged[-1]["touches"] + lv["touches"]
            merged[-1]["price"] = (
                merged[-1]["price"] * merged[-1]["touches"] +
                lv["price"] * lv["touches"]
            ) / total
            merged[-1]["touches"] = total
            merged[-1]["last_index"] = max(merged[-1]["last_index"], lv["last_index"])
        else:
            merged.append(lv.copy())
    return merged


def build_htf_levels(df: pd.DataFrame):
    atr = safe_float(df.iloc[-1]["atr"])
    if atr <= 0:
        return [], []

    supports = []
    resistances = []

    start = max(PIVOT_LEFT, len(df) - MAX_HTF_LEVEL_AGE)
    end = len(df) - PIVOT_RIGHT - 1

    for i in range(start, end):
        if is_pivot_low(df, i):
            supports.append({"price": float(df.iloc[i]["low"]), "touches": 1, "last_index": i})
        if is_pivot_high(df, i):
            resistances.append({"price": float(df.iloc[i]["high"]), "touches": 1, "last_index": i})

    return merge_levels(supports, atr), merge_levels(resistances, atr)


def nearest_level(levels, price: float):
    if not levels:
        return None
    return min(levels, key=lambda x: abs(x["price"] - price))


def near_level(price: float, level_price: float, atr: float) -> bool:
    return abs(price - level_price) <= atr * LEVEL_NEAR_ATR


# =========================================================
# TREND / ZONE
# =========================================================

def get_htf_trend(df: pd.DataFrame) -> str:
    last = df.iloc[-1]

    bull = (
        last["ema20"] > last["ema50"] > last["ema200"] and
        last["close"] > last["ema20"] and
        last["rsi"] >= 52
    )
    bear = (
        last["ema20"] < last["ema50"] < last["ema200"] and
        last["close"] < last["ema20"] and
        last["rsi"] <= 48
    )

    if bull:
        return "bull"
    if bear:
        return "bear"
    return "sideways"


def premium_discount_zone(htf_df: pd.DataFrame, price: float):
    swing_low = float(htf_df.iloc[-40:]["low"].min())
    swing_high = float(htf_df.iloc[-40:]["high"].max())
    eq = (swing_low + swing_high) / 2.0

    if price < eq:
        return "discount", swing_low, swing_high, eq
    return "premium", swing_low, swing_high, eq


# =========================================================
# PATTERNS
# =========================================================

def bullish_pin_bar(row) -> bool:
    body = safe_float(row["body"])
    rng = safe_float(row["range"])
    uw = safe_float(row["upper_wick"])
    lw = safe_float(row["lower_wick"])
    if rng <= 0:
        return False
    if body / rng > MAX_BODY_TO_RANGE_FOR_PIN:
        return False
    return lw >= body * MIN_PIN_WICK_RATIO and uw <= body * 1.25


def bearish_pin_bar(row) -> bool:
    body = safe_float(row["body"])
    rng = safe_float(row["range"])
    uw = safe_float(row["upper_wick"])
    lw = safe_float(row["lower_wick"])
    if rng <= 0:
        return False
    if body / rng > MAX_BODY_TO_RANGE_FOR_PIN:
        return False
    return uw >= body * MIN_PIN_WICK_RATIO and lw <= body * 1.25


def detect_inside_bar(df: pd.DataFrame):
    if len(df) < 4:
        return None
    mb = df.iloc[-3]
    ib = df.iloc[-2]

    if ib["high"] < mb["high"] and ib["low"] > mb["low"]:
        return {
            "mother_high": float(mb["high"]),
            "mother_low": float(mb["low"]),
            "inside_high": float(ib["high"]),
            "inside_low": float(ib["low"]),
        }
    return None


def detect_inside_break(df: pd.DataFrame):
    st = detect_inside_bar(df)
    if not st:
        return None
    last = df.iloc[-1]
    if last["close"] > st["inside_high"]:
        return {"direction": "long", "pattern": "inside_break_long", **st}
    if last["close"] < st["inside_low"]:
        return {"direction": "short", "pattern": "inside_break_short", **st}
    return None


def detect_fakey(df: pd.DataFrame):
    if len(df) < 5:
        return None

    mb = df.iloc[-4]
    ib = df.iloc[-3]
    fake = df.iloc[-2]
    conf = df.iloc[-1]
    atr = safe_float(conf["atr"])
    if atr <= 0:
        return None

    if not (ib["high"] < mb["high"] and ib["low"] > mb["low"]):
        return None

    if fake["high"] > ib["high"] and fake["close"] <= ib["high"]:
        if (fake["high"] - ib["high"]) >= atr * SWEEP_MIN_ATR:
            return {
                "direction": "short",
                "pattern": "fakey_short_pin" if bearish_pin_bar(fake) else "fakey_short",
                "mother_high": float(mb["high"]),
                "mother_low": float(mb["low"]),
                "inside_high": float(ib["high"]),
                "inside_low": float(ib["low"]),
                "fake_high": float(fake["high"]),
                "fake_low": float(fake["low"]),
            }

    if fake["low"] < ib["low"] and fake["close"] >= ib["low"]:
        if (ib["low"] - fake["low"]) >= atr * SWEEP_MIN_ATR:
            return {
                "direction": "long",
                "pattern": "fakey_long_pin" if bullish_pin_bar(fake) else "fakey_long",
                "mother_high": float(mb["high"]),
                "mother_low": float(mb["low"]),
                "inside_high": float(ib["high"]),
                "inside_low": float(ib["low"]),
                "fake_high": float(fake["high"]),
                "fake_low": float(fake["low"]),
            }

    return None


def get_previous_day_levels(day_df: pd.DataFrame):
    if day_df is None or len(day_df) < 3:
        return None
    prev = day_df.iloc[-2]
    return {
        "pdh": float(prev["high"]),
        "pdl": float(prev["low"]),
        "pdo": float(prev["open"]),
        "pdc": float(prev["close"]),
    }


def get_today_ltf_df(ltf_df: pd.DataFrame):
    today = now_utc().date()
    return ltf_df[ltf_df["time"].dt.date == today].copy()


def get_asian_range(ltf_df: pd.DataFrame):
    today_df = get_today_ltf_df(ltf_df)
    if today_df.empty:
        return None

    asia = today_df[
        (today_df["time"].dt.hour >= ASIA_START) &
        (today_df["time"].dt.hour < ASIA_END)
    ]
    if asia.empty:
        return None

    return {
        "high": float(asia["high"].max()),
        "low": float(asia["low"].min()),
    }


def detect_session_trap(ltf_df: pd.DataFrame, asian_range, pd_levels):
    if asian_range is None:
        return None
    if not in_trap_window():
        return None

    last = ltf_df.iloc[-1]
    atr = safe_float(last["atr"])
    if atr <= 0:
        return None

    asia_high = asian_range["high"]
    asia_low = asian_range["low"]
    pdh = pd_levels["pdh"] if pd_levels else None
    pdl = pd_levels["pdl"] if pd_levels else None

    if last["high"] > asia_high and last["close"] < asia_high:
        if (last["high"] - asia_high) >= atr * SWEEP_MIN_ATR:
            extra = False
            if pdh is not None and last["high"] > pdh and last["close"] < pdh:
                extra = True
            return {
                "direction": "short",
                "pattern": "session_trap_short_pdh" if extra else "session_trap_short",
                "swept_level": max(asia_high, pdh if pdh else asia_high)
            }

    if last["low"] < asia_low and last["close"] > asia_low:
        if (asia_low - last["low"]) >= atr * SWEEP_MIN_ATR:
            extra = False
            if pdl is not None and last["low"] < pdl and last["close"] > pdl:
                extra = True
            return {
                "direction": "long",
                "pattern": "session_trap_long_pdl" if extra else "session_trap_long",
                "swept_level": min(asia_low, pdl if pdl else asia_low)
            }

    return None


def recent_structure_points(df: pd.DataFrame, lookback=25):
    sub = df.iloc[-lookback:].copy()
    highs = []
    lows = []

    for i in range(2, len(sub) - 2):
        if sub.iloc[i]["high"] > sub.iloc[i - 1]["high"] and sub.iloc[i]["high"] > sub.iloc[i + 1]["high"]:
            highs.append((sub.iloc[i]["time"], float(sub.iloc[i]["high"])))
        if sub.iloc[i]["low"] < sub.iloc[i - 1]["low"] and sub.iloc[i]["low"] < sub.iloc[i + 1]["low"]:
            lows.append((sub.iloc[i]["time"], float(sub.iloc[i]["low"])))
    return highs, lows


def detect_bos(df: pd.DataFrame):
    if len(df) < 35:
        return None

    highs, lows = recent_structure_points(df, 30)
    if not highs or not lows:
        return None

    last = df.iloc[-1]
    recent_high = highs[-1][1]
    recent_low = lows[-1][1]

    if last["close"] > recent_high:
        return {"direction": "long", "pattern": "bullish_bos", "level": recent_high}
    if last["close"] < recent_low:
        return {"direction": "short", "pattern": "bearish_bos", "level": recent_low}
    return None


def detect_choch_like(df: pd.DataFrame):
    if len(df) < 12:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-6:-1]

    local_high = float(prev["high"].max())
    local_low = float(prev["low"].min())

    if last["close"] > local_high and prev.iloc[-1]["low"] < prev["low"].iloc[:-1].min():
        return {"direction": "long", "pattern": "choch_like_long", "level": local_high}

    if last["close"] < local_low and prev.iloc[-1]["high"] > prev["high"].iloc[:-1].max():
        return {"direction": "short", "pattern": "choch_like_short", "level": local_low}

    return None


# =========================================================
# SIGNAL
# =========================================================

def ltf_quality_ok(df: pd.DataFrame, direction: str) -> bool:
    last = df.iloc[-1]

    if safe_float(last["adx"]) < MIN_ADX:
        return False

    if direction == "long":
        return safe_float(last["rsi"]) >= 49
    return safe_float(last["rsi"]) <= 51


def build_signal(htf_df: pd.DataFrame, ltf_df: pd.DataFrame, day_df: pd.DataFrame):
    htf_last = htf_df.iloc[-1]
    ltf_last = ltf_df.iloc[-1]

    htf_atr = safe_float(htf_last["atr"])
    ltf_atr = safe_float(ltf_last["atr"])
    if htf_atr <= 0 or ltf_atr <= 0:
        return None

    price = float(ltf_last["close"])
    htf_trend = get_htf_trend(htf_df)
    zone, *_ = premium_discount_zone(htf_df, price)

    supports, resistances = build_htf_levels(htf_df)
    ns = nearest_level(supports, price)
    nr = nearest_level(resistances, price)
    near_support = ns is not None and near_level(price, ns["price"], htf_atr)
    near_resistance = nr is not None and near_level(price, nr["price"], htf_atr)

    pd_levels = get_previous_day_levels(day_df)
    asian_range = get_asian_range(ltf_df)

    inside_break = detect_inside_break(ltf_df)
    fakey = detect_fakey(ltf_df)
    trap = detect_session_trap(ltf_df, asian_range, pd_levels)
    bos = detect_bos(ltf_df)
    choch = detect_choch_like(ltf_df)

    near_pdh = pd_levels is not None and abs(price - pd_levels["pdh"]) <= htf_atr * LEVEL_NEAR_ATR
    near_pdl = pd_levels is not None and abs(price - pd_levels["pdl"]) <= htf_atr * LEVEL_NEAR_ATR

    long_reasons = []
    long_stop_candidates = []

    if near_support:
        long_reasons.append("htf_support")
        long_stop_candidates.append(ns["price"] - ltf_atr * ATR_SL_BUFFER)
    if near_pdl:
        long_reasons.append("pdl")
        long_stop_candidates.append(pd_levels["pdl"] - ltf_atr * ATR_SL_BUFFER)
    if zone == "discount":
        long_reasons.append("discount_zone")
    if fakey and fakey["direction"] == "long":
        long_reasons.append(fakey["pattern"])
        long_stop_candidates.append(min(fakey["fake_low"], fakey["mother_low"]) - ltf_atr * ATR_SL_BUFFER)
    if inside_break and inside_break["direction"] == "long":
        long_reasons.append(inside_break["pattern"])
        long_stop_candidates.append(inside_break["inside_low"] - ltf_atr * ATR_SL_BUFFER)
    if trap and trap["direction"] == "long":
        long_reasons.append(trap["pattern"])
        long_stop_candidates.append(float(ltf_last["low"]) - ltf_atr * ATR_SL_BUFFER)
    if choch and choch["direction"] == "long":
        long_reasons.append(choch["pattern"])
    if bos and bos["direction"] == "long":
        long_reasons.append(bos["pattern"])

    if (
        ltf_quality_ok(ltf_df, "long")
        and len(long_reasons) >= 3
        and ("htf_support" in long_reasons or "pdl" in long_reasons or zone == "discount")
        and any(k in " ".join(long_reasons) for k in ["fakey_long", "inside_break_long", "session_trap_long", "choch_like_long"])
        and htf_trend in ["bull", "sideways"]
    ):
        stop = min(long_stop_candidates) if long_stop_candidates else price - ltf_atr
        risk = price - stop
        if risk > 0 and risk <= ltf_atr * MAX_ENTRY_DISTANCE_ATR:
            tp1 = price + risk
            tp2 = price + risk * 2.2
            rr = (tp2 - price) / risk
            if rr >= MIN_RR:
                return {
                    "direction": "long",
                    "pattern": " + ".join(long_reasons[:5]),
                    "entry": price,
                    "stop": stop,
                    "tp1": tp1,
                    "tp2": tp2,
                    "htf_trend": htf_trend,
                    "zone": zone,
                    "level_type": "support/pdl",
                    "level_price": ns["price"] if ns else (pd_levels["pdl"] if pd_levels else price),
                    "atr": ltf_atr
                }

    short_reasons = []
    short_stop_candidates = []

    if near_resistance:
        short_reasons.append("htf_resistance")
        short_stop_candidates.append(nr["price"] + ltf_atr * ATR_SL_BUFFER)
    if near_pdh:
        short_reasons.append("pdh")
        short_stop_candidates.append(pd_levels["pdh"] + ltf_atr * ATR_SL_BUFFER)
    if zone == "premium":
        short_reasons.append("premium_zone")
    if fakey and fakey["direction"] == "short":
        short_reasons.append(fakey["pattern"])
        short_stop_candidates.append(max(fakey["fake_high"], fakey["mother_high"]) + ltf_atr * ATR_SL_BUFFER)
    if inside_break and inside_break["direction"] == "short":
        short_reasons.append(inside_break["pattern"])
        short_stop_candidates.append(inside_break["inside_high"] + ltf_atr * ATR_SL_BUFFER)
    if trap and trap["direction"] == "short":
        short_reasons.append(trap["pattern"])
        short_stop_candidates.append(float(ltf_last["high"]) + ltf_atr * ATR_SL_BUFFER)
    if choch and choch["direction"] == "short":
        short_reasons.append(choch["pattern"])
    if bos and bos["direction"] == "short":
        short_reasons.append(bos["pattern"])

    if (
        ltf_quality_ok(ltf_df, "short")
        and len(short_reasons) >= 3
        and ("htf_resistance" in short_reasons or "pdh" in short_reasons or zone == "premium")
        and any(k in " ".join(short_reasons) for k in ["fakey_short", "inside_break_short", "session_trap_short", "choch_like_short"])
        and htf_trend in ["bear", "sideways"]
    ):
        stop = max(short_stop_candidates) if short_stop_candidates else price + ltf_atr
        risk = stop - price
        if risk > 0 and risk <= ltf_atr * MAX_ENTRY_DISTANCE_ATR:
            tp1 = price - risk
            tp2 = price - risk * 2.2
            rr = (price - tp2) / risk
            if rr >= MIN_RR:
                return {
                    "direction": "short",
                    "pattern": " + ".join(short_reasons[:5]),
                    "entry": price,
                    "stop": stop,
                    "tp1": tp1,
                    "tp2": tp2,
                    "htf_trend": htf_trend,
                    "zone": zone,
                    "level_type": "resistance/pdh",
                    "level_price": nr["price"] if nr else (pd_levels["pdh"] if pd_levels else price),
                    "atr": ltf_atr
                }

    return None


# =========================================================
# STATE
# =========================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def make_signal_key(signal, ltf_df):
    bar_time = str(ltf_df.iloc[-1]["time"])
    return f"{bar_time}|{signal['direction']}|{signal['pattern']}"


# =========================================================
# MAIN
# =========================================================

def run():
    ensure_env()
    log("Bot başladı")
    send_telegram("🤖 XAU/USD Signal Bot başlatıldı")

    htf_df = add_indicators(fetch_twelve_data(SYMBOL, HTF_INTERVAL, HTF_OUTPUTSIZE))
    ltf_df = add_indicators(fetch_twelve_data(SYMBOL, LTF_INTERVAL, LTF_OUTPUTSIZE))
    day_df = add_indicators(fetch_twelve_data(SYMBOL, DAY_INTERVAL, DAY_OUTPUTSIZE))

    signal = build_signal(htf_df, ltf_df, day_df)

    if signal is None:
        log("Sinyal yok")
        send_telegram("ℹ️ XAU/USD: uygun sinyal yok")
        return

    state = load_state()
    key = make_signal_key(signal, ltf_df)
    if key == state.get("last_signal_key"):
        log("Aynı sinyal daha önce gönderilmiş")
        return

    state["last_signal_key"] = key
    save_state(state)

    msg = (
        f"🚨 XAU/USD SİNYAL\n"
        f"Yön: {signal['direction'].upper()}\n"
        f"Pattern: {signal['pattern']}\n"
        f"HTF Trend: {signal['htf_trend']}\n"
        f"Zone: {signal['zone']}\n"
        f"Seviye: {signal['level_type']} @ {signal['level_price']:.2f}\n"
        f"Entry: {signal['entry']:.2f}\n"
        f"SL: {signal['stop']:.2f}\n"
        f"TP1: {signal['tp1']:.2f}\n"
        f"TP2: {signal['tp2']:.2f}\n"
        f"ATR: {signal['atr']:.2f}\n"
        f"Sembol: {SYMBOL}"
    )
    log(msg)
    send_telegram(msg)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        err = f"HATA: {e}\n{traceback.format_exc()}"
        log(err)
        try:
            send_telegram(err[:3500])
        except Exception:
            pass
        raise
