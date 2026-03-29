# -*- coding: utf-8 -*-
import os
import json
import traceback
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# =========================================================
# CONFIG
# =========================================================

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SYMBOL = os.getenv("SYMBOL", "XAU/USD").strip()
MARKET_TIMEZONE = os.getenv("MARKET_TIMEZONE", "Europe/London").strip()

HTF_INTERVAL = "4h"
LTF_INTERVAL = "15min"
DAY_INTERVAL = "1day"

HTF_OUTPUTSIZE = 300
LTF_OUTPUTSIZE = 500
DAY_OUTPUTSIZE = 60

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

# Daha esnek eşikler
MIN_ADX = 15
MIN_RR = 1.45
MIN_SIGNAL_SCORE = 4.5

PIVOT_LEFT = 3
PIVOT_RIGHT = 3
MAX_HTF_LEVEL_AGE = 160

LEVEL_NEAR_ATR = 0.60
LEVEL_MERGE_ATR = 0.40
SWEEP_MIN_ATR = 0.15
ATR_SL_BUFFER = 0.22
MAX_ENTRY_DISTANCE_ATR = 1.35

MIN_PIN_WICK_RATIO = 2.0
MAX_BODY_TO_RANGE_FOR_PIN = 0.45

ASIA_START = 0
ASIA_END = 6
LONDON_START = 7
LONDON_END = 16
NEWYORK_START = 12
NEWYORK_END = 21
TRAP_LOOK_WINDOW_START = 6
TRAP_LOOK_WINDOW_END = 16

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


def get_tz():
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(MARKET_TIMEZONE)
    except Exception:
        return timezone.utc


def now_market():
    return datetime.now(get_tz())


def market_hour():
    return now_market().hour


def in_main_sessions():
    h = market_hour()
    return (LONDON_START <= h < LONDON_END) or (NEWYORK_START <= h < NEWYORK_END)


def in_trap_window():
    h = market_hour()
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
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "disable_web_page_preview": True
            },
            timeout=20
        )
        log(f"Telegram status: {resp.status_code}")
        log(f"Telegram response: {resp.text[:500]}")
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
        "timezone": MARKET_TIMEZONE,
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
    if data.get("status") == "error":
        raise RuntimeError(f"Twelve Data API hata: {data}")

    values = data.get("values")
    if not values:
        raise RuntimeError(f"Twelve Data veri yok: {data}")

    rows = []
    tz = get_tz()
    for x in values:
        dt = pd.to_datetime(x["datetime"])
        if getattr(dt, "tzinfo", None) is None:
            try:
                dt = dt.tz_localize(tz)
            except Exception:
                dt = dt.tz_localize("UTC")
        rows.append({
            "time": dt,
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
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(ADX_PERIOD).sum() / tr_smooth
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(ADX_PERIOD).sum() / tr_smooth
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
        last["close"] >= last["ema20"] and
        safe_float(last["rsi"]) >= 50
    )
    bear = (
        last["ema20"] < last["ema50"] < last["ema200"] and
        last["close"] <= last["ema20"] and
        safe_float(last["rsi"]) <= 50
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
    return lw >= max(body * MIN_PIN_WICK_RATIO, rng * 0.35) and uw <= body * 1.35


def bearish_pin_bar(row) -> bool:
    body = safe_float(row["body"])
    rng = safe_float(row["range"])
    uw = safe_float(row["upper_wick"])
    lw = safe_float(row["lower_wick"])
    if rng <= 0:
        return False
    if body / rng > MAX_BODY_TO_RANGE_FOR_PIN:
        return False
    return uw >= max(body * MIN_PIN_WICK_RATIO, rng * 0.35) and lw <= body * 1.35


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
    today = now_market().date()
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
    if asian_range is None or not in_trap_window():
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
            extra = bool(pdh is not None and last["high"] > pdh and last["close"] < pdh)
            return {
                "direction": "short",
                "pattern": "session_trap_short_pdh" if extra else "session_trap_short",
                "swept_level": max(asia_high, pdh if pdh else asia_high)
            }

    if last["low"] < asia_low and last["close"] > asia_low:
        if (asia_low - last["low"]) >= atr * SWEEP_MIN_ATR:
            extra = bool(pdl is not None and last["low"] < pdl and last["close"] > pdl)
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


def detect_ema_pullback_continuation(ltf_df: pd.DataFrame, htf_trend: str):
    if len(ltf_df) < 6:
        return None

    last = ltf_df.iloc[-1]
    prev = ltf_df.iloc[-2]
    atr = safe_float(last["atr"])
    if atr <= 0:
        return None

    if htf_trend == "bull":
        touched_ema20 = (
            prev["low"] <= prev["ema20"] + atr * 0.12 and
            prev["close"] >= prev["ema20"] - atr * 0.12
        )
        reclaim = (
            last["close"] > last["ema20"] and
            last["close"] > prev["high"]
        )
        if touched_ema20 and reclaim:
            return {
                "direction": "long",
                "pattern": "ema20_pullback_long",
                "ref_low": float(min(prev["low"], last["low"])),
                "ref_high": float(last["high"])
            }

    if htf_trend == "bear":
        touched_ema20 = (
            prev["high"] >= prev["ema20"] - atr * 0.12 and
            prev["close"] <= prev["ema20"] + atr * 0.12
        )
        reject = (
            last["close"] < last["ema20"] and
            last["close"] < prev["low"]
        )
        if touched_ema20 and reject:
            return {
                "direction": "short",
                "pattern": "ema20_pullback_short",
                "ref_high": float(max(prev["high"], last["high"])),
                "ref_low": float(last["low"])
            }

    return None


def detect_breakout_retest(ltf_df: pd.DataFrame, pd_levels, htf_trend: str):
    if len(ltf_df) < 4 or pd_levels is None:
        return None

    last = ltf_df.iloc[-1]
    prev = ltf_df.iloc[-2]
    atr = safe_float(last["atr"])
    if atr <= 0:
        return None

    pdh = pd_levels["pdh"]
    pdl = pd_levels["pdl"]

    # PDH breakout retest long
    if htf_trend in ["bull", "sideways"]:
        broke = prev["close"] > pdh
        retest = last["low"] <= pdh + atr * 0.18 and last["close"] > pdh
        if broke and retest:
            return {
                "direction": "long",
                "pattern": "pdh_break_retest_long",
                "level": float(pdh),
                "ref_low": float(min(prev["low"], last["low"]))
            }

    # PDL breakout retest short
    if htf_trend in ["bear", "sideways"]:
        broke = prev["close"] < pdl
        retest = last["high"] >= pdl - atr * 0.18 and last["close"] < pdl
        if broke and retest:
            return {
                "direction": "short",
                "pattern": "pdl_break_retest_short",
                "level": float(pdl),
                "ref_high": float(max(prev["high"], last["high"]))
            }

    return None


# =========================================================
# SIGNAL ENGINE
# =========================================================

def calc_rr(entry, stop, tp2, direction):
    if direction == "long":
        risk = entry - stop
        reward = tp2 - entry
    else:
        risk = stop - entry
        reward = entry - tp2
    if risk <= 0:
        return 0.0
    return reward / risk


def base_quality_checks(ltf_df, direction):
    last = ltf_df.iloc[-1]
    adx = safe_float(last["adx"])
    rsi = safe_float(last["rsi"])

    if adx < MIN_ADX:
        return False

    if direction == "long" and rsi < 46:
        return False
    if direction == "short" and rsi > 54:
        return False

    return True


def choose_stop_long(price, atr, candidates):
    vals = [x for x in candidates if x is not None and x < price]
    if not vals:
        return price - atr * 0.95
    return min(vals)


def choose_stop_short(price, atr, candidates):
    vals = [x for x in candidates if x is not None and x > price]
    if not vals:
        return price + atr * 0.95
    return max(vals)


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
    ema_cont = detect_ema_pullback_continuation(ltf_df, htf_trend)
    breakout_retest = detect_breakout_retest(ltf_df, pd_levels, htf_trend)

    near_pdh = pd_levels is not None and abs(price - pd_levels["pdh"]) <= htf_atr * LEVEL_NEAR_ATR
    near_pdl = pd_levels is not None and abs(price - pd_levels["pdl"]) <= htf_atr * LEVEL_NEAR_ATR

    signals = []

    # ---------------- LONG ----------------
    if base_quality_checks(ltf_df, "long"):
        score = 0.0
        reasons = []
        stop_candidates = []

        if htf_trend == "bull":
            score += 1.3
            reasons.append("htf_bull")
        elif htf_trend == "sideways":
            score += 0.4
            reasons.append("htf_sideways")

        if zone == "discount":
            score += 1.0
            reasons.append("discount_zone")

        if near_support:
            score += 1.2
            reasons.append("htf_support")
            stop_candidates.append(ns["price"] - ltf_atr * ATR_SL_BUFFER)

        if near_pdl:
            score += 1.1
            reasons.append("pdl")
            stop_candidates.append(pd_levels["pdl"] - ltf_atr * ATR_SL_BUFFER)

        if near_pdh and htf_trend == "bull":
            score += 0.3
            reasons.append("near_pdh")

        if fakey and fakey["direction"] == "long":
            score += 1.7
            reasons.append(fakey["pattern"])
            stop_candidates.append(min(fakey["fake_low"], fakey["mother_low"]) - ltf_atr * ATR_SL_BUFFER)

        if inside_break and inside_break["direction"] == "long":
            score += 1.1
            reasons.append(inside_break["pattern"])
            stop_candidates.append(inside_break["inside_low"] - ltf_atr * ATR_SL_BUFFER)

        if trap and trap["direction"] == "long":
            score += 1.8
            reasons.append(trap["pattern"])
            stop_candidates.append(float(ltf_last["low"]) - ltf_atr * ATR_SL_BUFFER)

        if choch and choch["direction"] == "long":
            score += 0.9
            reasons.append(choch["pattern"])

        if bos and bos["direction"] == "long":
            score += 0.9
            reasons.append(bos["pattern"])

        if ema_cont and ema_cont["direction"] == "long":
            score += 1.6
            reasons.append(ema_cont["pattern"])
            stop_candidates.append(ema_cont["ref_low"] - ltf_atr * ATR_SL_BUFFER)

        if breakout_retest and breakout_retest["direction"] == "long":
            score += 1.5
            reasons.append(breakout_retest["pattern"])
            stop_candidates.append(breakout_retest["ref_low"] - ltf_atr * ATR_SL_BUFFER)

        if in_main_sessions():
            score += 0.4
            reasons.append("main_session")

        stop = choose_stop_long(price, ltf_atr, stop_candidates)
        risk = price - stop
        if 0 < risk <= ltf_atr * MAX_ENTRY_DISTANCE_ATR:
            tp1 = price + risk * 1.0
            tp2 = price + risk * 1.8
            rr = calc_rr(price, stop, tp2, "long")

            if rr >= MIN_RR and score >= MIN_SIGNAL_SCORE:
                signals.append({
                    "direction": "long",
                    "pattern": " + ".join(reasons[:6]),
                    "entry": price,
                    "stop": stop,
                    "tp1": tp1,
                    "tp2": tp2,
                    "htf_trend": htf_trend,
                    "zone": zone,
                    "level_type": "support/pdl/continuation",
                    "level_price": ns["price"] if ns else (pd_levels["pdl"] if pd_levels else price),
                    "atr": ltf_atr,
                    "score": round(score, 2),
                    "rr": round(rr, 2)
                })

    # ---------------- SHORT ----------------
    if base_quality_checks(ltf_df, "short"):
        score = 0.0
        reasons = []
        stop_candidates = []

        if htf_trend == "bear":
            score += 1.3
            reasons.append("htf_bear")
        elif htf_trend == "sideways":
            score += 0.4
            reasons.append("htf_sideways")

        if zone == "premium":
            score += 1.0
            reasons.append("premium_zone")

        if near_resistance:
            score += 1.2
            reasons.append("htf_resistance")
            stop_candidates.append(nr["price"] + ltf_atr * ATR_SL_BUFFER)

        if near_pdh:
            score += 1.1
            reasons.append("pdh")
            stop_candidates.append(pd_levels["pdh"] + ltf_atr * ATR_SL_BUFFER)

        if near_pdl and htf_trend == "bear":
            score += 0.3
            reasons.append("near_pdl")

        if fakey and fakey["direction"] == "short":
            score += 1.7
            reasons.append(fakey["pattern"])
            stop_candidates.append(max(fakey["fake_high"], fakey["mother_high"]) + ltf_atr * ATR_SL_BUFFER)

        if inside_break and inside_break["direction"] == "short":
            score += 1.1
            reasons.append(inside_break["pattern"])
            stop_candidates.append(inside_break["inside_high"] + ltf_atr * ATR_SL_BUFFER)

        if trap and trap["direction"] == "short":
            score += 1.8
            reasons.append(trap["pattern"])
            stop_candidates.append(float(ltf_last["high"]) + ltf_atr * ATR_SL_BUFFER)

        if choch and choch["direction"] == "short":
            score += 0.9
            reasons.append(choch["pattern"])

        if bos and bos["direction"] == "short":
            score += 0.9
            reasons.append(bos["pattern"])

        if ema_cont and ema_cont["direction"] == "short":
            score += 1.6
            reasons.append(ema_cont["pattern"])
            stop_candidates.append(ema_cont["ref_high"] + ltf_atr * ATR_SL_BUFFER)

        if breakout_retest and breakout_retest["direction"] == "short":
            score += 1.5
            reasons.append(breakout_retest["pattern"])
            stop_candidates.append(breakout_retest["ref_high"] + ltf_atr * ATR_SL_BUFFER)

        if in_main_sessions():
            score += 0.4
            reasons.append("main_session")

        stop = choose_stop_short(price, ltf_atr, stop_candidates)
        risk = stop - price
        if 0 < risk <= ltf_atr * MAX_ENTRY_DISTANCE_ATR:
            tp1 = price - risk * 1.0
            tp2 = price - risk * 1.8
            rr = calc_rr(price, stop, tp2, "short")

            if rr >= MIN_RR and score >= MIN_SIGNAL_SCORE:
                signals.append({
                    "direction": "short",
                    "pattern": " + ".join(reasons[:6]),
                    "entry": price,
                    "stop": stop,
                    "tp1": tp1,
                    "tp2": tp2,
                    "htf_trend": htf_trend,
                    "zone": zone,
                    "level_type": "resistance/pdh/continuation",
                    "level_price": nr["price"] if nr else (pd_levels["pdh"] if pd_levels else price),
                    "atr": ltf_atr,
                    "score": round(score, 2),
                    "rr": round(rr, 2)
                })

    if not signals:
        return None

    signals = sorted(signals, key=lambda x: (x["score"], x["rr"]), reverse=True)
    return signals[0]


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
    side = signal["direction"]
    entry = round(signal["entry"], 2)
    return f"{bar_time}|{side}|{entry}|{signal['pattern'][:80]}"


# =========================================================
# MAIN
# =========================================================

def run():
    ensure_env()
    log("Bot çalışıyor...")

    htf_df = add_indicators(fetch_twelve_data(SYMBOL, HTF_INTERVAL, HTF_OUTPUTSIZE))
    ltf_df = add_indicators(fetch_twelve_data(SYMBOL, LTF_INTERVAL, LTF_OUTPUTSIZE))
    day_df = add_indicators(fetch_twelve_data(SYMBOL, DAY_INTERVAL, DAY_OUTPUTSIZE))

    signal = build_signal(htf_df, ltf_df, day_df)
    if signal is None:
        log("Sinyal yok, mesaj gönderilmedi.")
        return

    state = load_state()
    key = make_signal_key(signal, ltf_df)

    if key == state.get("last_signal_key"):
        log("Aynı sinyal daha önce gönderilmiş, tekrar atılmadı.")
        return

    state["last_signal_key"] = key
    save_state(state)

    msg = (
        f"🚨 XAU/USD İŞLEM SİNYALİ\n"
        f"Yön: {signal['direction'].upper()}\n"
        f"Setup: {signal['pattern']}\n"
        f"HTF Trend: {signal['htf_trend']}\n"
        f"Zone: {signal['zone']}\n"
        f"Seviye: {signal['level_type']} @ {signal['level_price']:.2f}\n"
        f"Entry: {signal['entry']:.2f}\n"
        f"SL: {signal['stop']:.2f}\n"
        f"TP1: {signal['tp1']:.2f}\n"
        f"TP2: {signal['tp2']:.2f}\n"
        f"RR: {signal['rr']:.2f}\n"
        f"Skor: {signal['score']:.2f}\n"
        f"ATR: {signal['atr']:.2f}\n"
        f"Sembol: {SYMBOL}\n"
        f"Saat Dilimi: {MARKET_TIMEZONE}"
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
