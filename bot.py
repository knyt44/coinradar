# -*- coding: utf-8 -*-
import os
import json
import time
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

TREND_INTERVAL = "4h"
SETUP_INTERVAL = "1h"
CONFIRM_INTERVAL = "30min"
DAY_INTERVAL = "1day"

TREND_OUTPUTSIZE = 300
SETUP_OUTPUTSIZE = 400
CONFIRM_OUTPUTSIZE = 400
DAY_OUTPUTSIZE = 60

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

MIN_ADX_1H = 16
MIN_ADX_30M = 14
MIN_RR = 1.30
MIN_SIGNAL_SCORE = 5.0

PIVOT_LEFT = 3
PIVOT_RIGHT = 3
MAX_HTF_LEVEL_AGE = 160

LEVEL_NEAR_ATR = 0.70
LEVEL_MERGE_ATR = 0.40
SWEEP_MIN_ATR = 0.15
ATR_SL_BUFFER = 0.22
MAX_ENTRY_DISTANCE_ATR = 1.60

MIN_PIN_WICK_RATIO = 2.0
MAX_BODY_TO_RANGE_FOR_PIN = 0.45

CHECK_EVERY_SECONDS = 180
STATE_FILE = "signal_state.json"

# =========================================================
# HELPERS
# =========================================================

def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


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
# DATA
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
# STRUCTURE
# =========================================================

def get_trend_4h(df: pd.DataFrame) -> str:
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


def get_previous_day_levels(day_df: pd.DataFrame):
    if len(day_df) < 3:
        return None
    prev = day_df.iloc[-2]
    return {
        "pdh": float(prev["high"]),
        "pdl": float(prev["low"]),
    }


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


def detect_engulfing(df: pd.DataFrame):
    if len(df) < 2:
        return None

    prev = df.iloc[-2]
    last = df.iloc[-1]

    prev_bear = prev["close"] < prev["open"]
    prev_bull = prev["close"] > prev["open"]
    last_bull = last["close"] > last["open"]
    last_bear = last["close"] < last["open"]

    bullish = (
        prev_bear and last_bull and
        last["open"] <= prev["close"] and
        last["close"] >= prev["open"] and
        last["body"] >= prev["body"] * 0.9
    )

    bearish = (
        prev_bull and last_bear and
        last["open"] >= prev["close"] and
        last["close"] <= prev["open"] and
        last["body"] >= prev["body"] * 0.9
    )

    if bullish:
        return {
            "direction": "long",
            "pattern": "bullish_engulf",
            "ref_low": float(min(prev["low"], last["low"])),
        }
    if bearish:
        return {
            "direction": "short",
            "pattern": "bearish_engulf",
            "ref_high": float(max(prev["high"], last["high"])),
        }
    return None


def detect_inside_break(df: pd.DataFrame):
    if len(df) < 3:
        return None

    mother = df.iloc[-3]
    inside = df.iloc[-2]
    confirm = df.iloc[-1]

    if inside["high"] < mother["high"] and inside["low"] > mother["low"]:
        if confirm["close"] > inside["high"]:
            return {
                "direction": "long",
                "pattern": "inside_break_long",
                "inside_low": float(inside["low"]),
            }
        if confirm["close"] < inside["low"]:
            return {
                "direction": "short",
                "pattern": "inside_break_short",
                "inside_high": float(inside["high"]),
            }
    return None


def detect_fakey(df: pd.DataFrame):
    if len(df) < 4:
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
                "ref_high": float(max(fake["high"], mb["high"])),
            }

    if fake["low"] < ib["low"] and fake["close"] >= ib["low"]:
        if (ib["low"] - fake["low"]) >= atr * SWEEP_MIN_ATR:
            return {
                "direction": "long",
                "pattern": "fakey_long_pin" if bullish_pin_bar(fake) else "fakey_long",
                "ref_low": float(min(fake["low"], mb["low"])),
            }

    return None


def detect_ema_pullback(df: pd.DataFrame, trend: str):
    if len(df) < 3:
        return None

    prev = df.iloc[-2]
    last = df.iloc[-1]
    atr = safe_float(last["atr"])
    if atr <= 0:
        return None

    if trend == "bull":
        touched = prev["low"] <= prev["ema20"] + atr * 0.20
        reclaim = last["close"] > last["ema20"] and last["close"] > prev["high"]
        if touched and reclaim:
            return {
                "direction": "long",
                "pattern": "ema20_pullback_long",
                "ref_low": float(min(prev["low"], last["low"]))
            }

    if trend == "bear":
        touched = prev["high"] >= prev["ema20"] - atr * 0.20
        reject = last["close"] < last["ema20"] and last["close"] < prev["low"]
        if touched and reject:
            return {
                "direction": "short",
                "pattern": "ema20_pullback_short",
                "ref_high": float(max(prev["high"], last["high"]))
            }

    return None


def detect_breakout_retest(df: pd.DataFrame, pd_levels, trend: str):
    if len(df) < 2 or pd_levels is None:
        return None

    prev = df.iloc[-2]
    last = df.iloc[-1]
    atr = safe_float(last["atr"])
    if atr <= 0:
        return None

    pdh = pd_levels["pdh"]
    pdl = pd_levels["pdl"]

    if trend in ["bull", "sideways"]:
        broke = prev["close"] > pdh
        retest = last["low"] <= pdh + atr * 0.25 and last["close"] > pdh
        if broke and retest:
            return {
                "direction": "long",
                "pattern": "pdh_break_retest_long",
                "ref_low": float(min(prev["low"], last["low"]))
            }

    if trend in ["bear", "sideways"]:
        broke = prev["close"] < pdl
        retest = last["high"] >= pdl - atr * 0.25 and last["close"] < pdl
        if broke and retest:
            return {
                "direction": "short",
                "pattern": "pdl_break_retest_short",
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


def choose_stop_long(price, atr, candidates):
    vals = [x for x in candidates if x is not None and x < price]
    return min(vals) if vals else price - atr * 0.95


def choose_stop_short(price, atr, candidates):
    vals = [x for x in candidates if x is not None and x > price]
    return max(vals) if vals else price + atr * 0.95


def quality_ok(df: pd.DataFrame, direction: str, min_adx: float) -> bool:
    last = df.iloc[-1]
    adx = safe_float(last["adx"])
    rsi = safe_float(last["rsi"])

    if adx < min_adx:
        return False
    if direction == "long" and rsi < 46:
        return False
    if direction == "short" and rsi > 54:
        return False
    return True


def score_setup(df: pd.DataFrame, trend: str, zone: str, near_support: bool, near_resistance: bool, near_pdh: bool, near_pdl: bool, pd_levels):
    price = float(df.iloc[-1]["close"])
    atr = safe_float(df.iloc[-1]["atr"])

    engulf = detect_engulfing(df)
    inside_break = detect_inside_break(df)
    fakey = detect_fakey(df)
    ema_pb = detect_ema_pullback(df, trend)
    br_retest = detect_breakout_retest(df, pd_levels, trend)

    long_score = 0.0
    long_reasons = []
    long_stops = []

    short_score = 0.0
    short_reasons = []
    short_stops = []

    if trend == "bull":
        long_score += 1.4
        long_reasons.append("trend_bull")
    elif trend == "bear":
        short_score += 1.4
        short_reasons.append("trend_bear")

    if zone == "discount":
        long_score += 1.0
        long_reasons.append("discount_zone")
    elif zone == "premium":
        short_score += 1.0
        short_reasons.append("premium_zone")

    if near_support:
        long_score += 1.1
        long_reasons.append("htf_support")
    if near_resistance:
        short_score += 1.1
        short_reasons.append("htf_resistance")

    if near_pdl:
        long_score += 0.9
        long_reasons.append("near_pdl")
    if near_pdh:
        short_score += 0.9
        short_reasons.append("near_pdh")

    if engulf and engulf["direction"] == "long":
        long_score += 1.6
        long_reasons.append(engulf["pattern"])
        long_stops.append(engulf["ref_low"] - atr * ATR_SL_BUFFER)
    elif engulf and engulf["direction"] == "short":
        short_score += 1.6
        short_reasons.append(engulf["pattern"])
        short_stops.append(engulf["ref_high"] + atr * ATR_SL_BUFFER)

    if inside_break and inside_break["direction"] == "long":
        long_score += 1.4
        long_reasons.append(inside_break["pattern"])
        long_stops.append(inside_break["inside_low"] - atr * ATR_SL_BUFFER)
    elif inside_break and inside_break["direction"] == "short":
        short_score += 1.4
        short_reasons.append(inside_break["pattern"])
        short_stops.append(inside_break["inside_high"] + atr * ATR_SL_BUFFER)

    if fakey and fakey["direction"] == "long":
        long_score += 1.5
        long_reasons.append(fakey["pattern"])
        long_stops.append(fakey["ref_low"] - atr * ATR_SL_BUFFER)
    elif fakey and fakey["direction"] == "short":
        short_score += 1.5
        short_reasons.append(fakey["pattern"])
        short_stops.append(fakey["ref_high"] + atr * ATR_SL_BUFFER)

    if ema_pb and ema_pb["direction"] == "long":
        long_score += 1.3
        long_reasons.append(ema_pb["pattern"])
        long_stops.append(ema_pb["ref_low"] - atr * ATR_SL_BUFFER)
    elif ema_pb and ema_pb["direction"] == "short":
        short_score += 1.3
        short_reasons.append(ema_pb["pattern"])
        short_stops.append(ema_pb["ref_high"] + atr * ATR_SL_BUFFER)

    if br_retest and br_retest["direction"] == "long":
        long_score += 1.3
        long_reasons.append(br_retest["pattern"])
        long_stops.append(br_retest["ref_low"] - atr * ATR_SL_BUFFER)
    elif br_retest and br_retest["direction"] == "short":
        short_score += 1.3
        short_reasons.append(br_retest["pattern"])
        short_stops.append(br_retest["ref_high"] + atr * ATR_SL_BUFFER)

    return {
        "long": {
            "score": round(long_score, 2),
            "reasons": long_reasons,
            "stops": long_stops,
        },
        "short": {
            "score": round(short_score, 2),
            "reasons": short_reasons,
            "stops": short_stops,
        }
    }


def build_signal(trend_df: pd.DataFrame, setup_df: pd.DataFrame, confirm_df: pd.DataFrame, day_df: pd.DataFrame):
    trend_last = trend_df.iloc[-1]
    setup_last = setup_df.iloc[-1]
    confirm_last = confirm_df.iloc[-1]

    trend_atr = safe_float(trend_last["atr"])
    setup_atr = safe_float(setup_last["atr"])
    confirm_atr = safe_float(confirm_last["atr"])

    if trend_atr <= 0 or setup_atr <= 0 or confirm_atr <= 0:
        log("DEBUG | ATR invalid")
        return None

    price = float(confirm_last["close"])
    trend = get_trend_4h(trend_df)
    zone, *_ = premium_discount_zone(trend_df, price)
    pd_levels = get_previous_day_levels(day_df)

    supports, resistances = build_htf_levels(trend_df)
    ns = nearest_level(supports, price)
    nr = nearest_level(resistances, price)

    near_support = ns is not None and near_level(price, ns["price"], trend_atr)
    near_resistance = nr is not None and near_level(price, nr["price"], trend_atr)
    near_pdh = pd_levels is not None and abs(price - pd_levels["pdh"]) <= trend_atr * LEVEL_NEAR_ATR
    near_pdl = pd_levels is not None and abs(price - pd_levels["pdl"]) <= trend_atr * LEVEL_NEAR_ATR

    log(f"DEBUG | trend={trend} zone={zone} price={price:.2f} near_support={near_support} near_resistance={near_resistance} near_pdh={near_pdh} near_pdl={near_pdl}")

    setup_scores = score_setup(setup_df, trend, zone, near_support, near_resistance, near_pdh, near_pdl, pd_levels)
    confirm_scores = score_setup(confirm_df, trend, zone, near_support, near_resistance, near_pdh, near_pdl, pd_levels)

    candidates = []

    # LONG
    if trend == "bull":
        if quality_ok(setup_df, "long", MIN_ADX_1H) and quality_ok(confirm_df, "long", MIN_ADX_30M):
            total_score = setup_scores["long"]["score"] * 0.65 + confirm_scores["long"]["score"] * 0.35
            reasons = list(dict.fromkeys(setup_scores["long"]["reasons"] + confirm_scores["long"]["reasons"]))
            stops = setup_scores["long"]["stops"] + confirm_scores["long"]["stops"]

            stop = choose_stop_long(price, confirm_atr, stops)
            risk = price - stop
            if 0 < risk <= confirm_atr * MAX_ENTRY_DISTANCE_ATR:
                tp1 = price + risk * 1.0
                tp2 = price + risk * 1.6
                rr = calc_rr(price, stop, tp2, "long")
                log(f"LONG CHECK | setup_score={setup_scores['long']['score']:.2f} confirm_score={confirm_scores['long']['score']:.2f} total={total_score:.2f} rr={rr:.2f} reasons={reasons}")
                if total_score >= MIN_SIGNAL_SCORE and rr >= MIN_RR and len(reasons) >= 3:
                    candidates.append({
                        "direction": "long",
                        "pattern": " + ".join(reasons[:6]),
                        "entry": price,
                        "stop": stop,
                        "tp1": tp1,
                        "tp2": tp2,
                        "rr": round(rr, 2),
                        "score": round(total_score, 2),
                        "trend": trend,
                        "zone": zone,
                        "level_price": ns["price"] if ns else (pd_levels["pdl"] if pd_levels else price),
                        "level_type": "4H support / PDL",
                        "atr": confirm_atr
                    })
        else:
            log("LONG BLOCKED | 1H/30M quality failed")

    # SHORT
    if trend == "bear":
        if quality_ok(setup_df, "short", MIN_ADX_1H) and quality_ok(confirm_df, "short", MIN_ADX_30M):
            total_score = setup_scores["short"]["score"] * 0.65 + confirm_scores["short"]["score"] * 0.35
            reasons = list(dict.fromkeys(setup_scores["short"]["reasons"] + confirm_scores["short"]["reasons"]))
            stops = setup_scores["short"]["stops"] + confirm_scores["short"]["stops"]

            stop = choose_stop_short(price, confirm_atr, stops)
            risk = stop - price
            if 0 < risk <= confirm_atr * MAX_ENTRY_DISTANCE_ATR:
                tp1 = price - risk * 1.0
                tp2 = price - risk * 1.6
                rr = calc_rr(price, stop, tp2, "short")
                log(f"SHORT CHECK | setup_score={setup_scores['short']['score']:.2f} confirm_score={confirm_scores['short']['score']:.2f} total={total_score:.2f} rr={rr:.2f} reasons={reasons}")
                if total_score >= MIN_SIGNAL_SCORE and rr >= MIN_RR and len(reasons) >= 3:
                    candidates.append({
                        "direction": "short",
                        "pattern": " + ".join(reasons[:6]),
                        "entry": price,
                        "stop": stop,
                        "tp1": tp1,
                        "tp2": tp2,
                        "rr": round(rr, 2),
                        "score": round(total_score, 2),
                        "trend": trend,
                        "zone": zone,
                        "level_price": nr["price"] if nr else (pd_levels["pdh"] if pd_levels else price),
                        "level_type": "4H resistance / PDH",
                        "atr": confirm_atr
                    })
        else:
            log("SHORT BLOCKED | 1H/30M quality failed")

    if not candidates:
        log("DEBUG | no candidate passed final filters")
        return None

    candidates = sorted(candidates, key=lambda x: (x["score"], x["rr"]), reverse=True)
    return candidates[0]


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


def make_signal_key(signal, confirm_df):
    bar_time = str(confirm_df.iloc[-1]["time"])
    return f"{bar_time}|{signal['direction']}|{round(signal['entry'], 2)}|{signal['pattern'][:80]}"


# =========================================================
# MAIN
# =========================================================

def run():
    ensure_env()
    log("Bot çalışıyor...")

    trend_df = add_indicators(fetch_twelve_data(SYMBOL, TREND_INTERVAL, TREND_OUTPUTSIZE))
    setup_df = add_indicators(fetch_twelve_data(SYMBOL, SETUP_INTERVAL, SETUP_OUTPUTSIZE))
    confirm_df = add_indicators(fetch_twelve_data(SYMBOL, CONFIRM_INTERVAL, CONFIRM_OUTPUTSIZE))
    day_df = add_indicators(fetch_twelve_data(SYMBOL, DAY_INTERVAL, DAY_OUTPUTSIZE))

    signal = build_signal(trend_df, setup_df, confirm_df, day_df)
    if signal is None:
        log("Sinyal yok, mesaj gönderilmedi.")
        return

    state = load_state()
    key = make_signal_key(signal, confirm_df)

    if key == state.get("last_signal_key"):
        log("Aynı sinyal tekrar gönderilmedi.")
        return

    state["last_signal_key"] = key
    save_state(state)

    msg = (
        f"🚨 XAU/USD İŞLEM SİNYALİ\n"
        f"Yön: {signal['direction'].upper()}\n"
        f"4H Trend: {signal['trend']}\n"
        f"Zone: {signal['zone']}\n"
        f"Setup: {signal['pattern']}\n"
        f"Seviye: {signal['level_type']} @ {signal['level_price']:.2f}\n"
        f"Entry: {signal['entry']:.2f}\n"
        f"SL: {signal['stop']:.2f}\n"
        f"TP1: {signal['tp1']:.2f}\n"
        f"TP2: {signal['tp2']:.2f}\n"
        f"RR: {signal['rr']:.2f}\n"
        f"Skor: {signal['score']:.2f}\n"
        f"ATR(30m): {signal['atr']:.2f}\n"
        f"Sembol: {SYMBOL}\n"
        f"TF: 4H trend + 1H setup + 30M confirm"
    )

    log(msg)
    send_telegram(msg)


if __name__ == "__main__":
    while True:
        try:
            run()
        except Exception as e:
            err = f"HATA: {e}\n{traceback.format_exc()}"
            log(err)
            try:
                send_telegram(err[:3500])
            except Exception:
                pass
        time.sleep(CHECK_EVERY_SECONDS)
