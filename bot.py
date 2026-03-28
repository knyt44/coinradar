# -*- coding: utf-8 -*-
"""
XAUUSD V2 PRO PRICE ACTION BOT
--------------------------------------------------
Özellikler:
- Enstrüman: XAUUSD
- HTF trend: H4
- Giriş timeframe: M15
- Destek / direnç: H4 pivot cluster
- PDH / PDL (previous day high/low)
- Asian range + London / NY session trap
- Liquidity sweep
- BOS / mini structure break
- FVG filtre
- Premium / discount zone
- Inside bar / fakey / pin bar
- ATR stop
- RR filtresi
- TP1 partial close + SL BE
- Telegram
- Dry run / canlı mod

KURULUM:
pip install MetaTrader5 pandas numpy requests

ÇALIŞTIRMA:
python xauusd_v2_pro_bot.py
"""

import time
import math
import traceback
from datetime import datetime, timezone

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import requests


# =========================================================
# AYARLAR
# =========================================================

LIVE_TRADING = False
SYMBOL = "XAUUSD"

HTF = mt5.TIMEFRAME_H4
LTF = mt5.TIMEFRAME_M15
DAY_TF = mt5.TIMEFRAME_D1

HTF_BARS = 500
LTF_BARS = 700
DAY_BARS = 10

CHECK_EVERY_SECONDS = 20

# Risk
RISK_PER_TRADE = 0.01
MIN_RR = 1.8
PARTIAL_CLOSE_AT_TP1 = 0.50
MOVE_SL_TO_BE_AFTER_TP1 = True

# EMA / RSI / ADX
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ADX_PERIOD = 14
MIN_ADX = 18

# ATR
ATR_PERIOD = 14
ATR_SL_BUFFER = 0.25
LEVEL_NEAR_ATR = 0.45
LEVEL_MERGE_ATR = 0.35
SWEEP_MIN_ATR = 0.18
MAX_ENTRY_DISTANCE_ATR = 1.10

# Pivots
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
MAX_HTF_LEVEL_AGE = 220

# Pin bar
MIN_PIN_WICK_RATIO = 2.0
MAX_BODY_TO_RANGE_FOR_PIN = 0.42

# Spread
MAX_SPREAD_DOLLAR = 0.60

# Sessions UTC
LONDON_START = 7
LONDON_END = 16
NEWYORK_START = 12
NEWYORK_END = 21

# Asian range (UTC)
ASIA_START = 0
ASIA_END = 6

# Session trap hours
TRAP_LOOK_WINDOW_START = 6
TRAP_LOOK_WINDOW_END = 15

# FVG
MIN_FVG_ATR = 0.10

# Telegram
TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"

MAGIC = 440088


# =========================================================
# YARDIMCI
# =========================================================

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log(f"Telegram hata: {e}")


def safe_float(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except:
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


# =========================================================
# MT5
# =========================================================

def init_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize başarısız: {mt5.last_error()}")

    info = mt5.symbol_info(SYMBOL)
    if info is None:
        raise RuntimeError(f"{SYMBOL} bulunamadı")
    if not info.visible:
        if not mt5.symbol_select(SYMBOL, True):
            raise RuntimeError(f"{SYMBOL} seçilemedi")

    log("MT5 hazır")


def get_rates(symbol, timeframe, count):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


# =========================================================
# İNDİKATÖRLER
# =========================================================

def add_indicators(df):
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

def is_pivot_high(df, i):
    if i - PIVOT_LEFT < 0 or i + PIVOT_RIGHT >= len(df):
        return False
    h = df.iloc[i]["high"]
    for j in range(i - PIVOT_LEFT, i + PIVOT_RIGHT + 1):
        if j == i:
            continue
        if df.iloc[j]["high"] >= h:
            return False
    return True


def is_pivot_low(df, i):
    if i - PIVOT_LEFT < 0 or i + PIVOT_RIGHT >= len(df):
        return False
    l = df.iloc[i]["low"]
    for j in range(i - PIVOT_LEFT, i + PIVOT_RIGHT + 1):
        if j == i:
            continue
        if df.iloc[j]["low"] <= l:
            return False
    return True


def merge_levels(levels, atr):
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


def build_htf_levels(df):
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


def nearest_level(levels, price):
    if not levels:
        return None
    return min(levels, key=lambda x: abs(x["price"] - price))


def near_level(price, level_price, atr):
    return abs(price - level_price) <= atr * LEVEL_NEAR_ATR


# =========================================================
# TREND / PREMIUM-DISCOUNT
# =========================================================

def get_htf_trend(df):
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


def premium_discount_zone(htf_df, price):
    swing_low = float(htf_df.iloc[-40:]["low"].min())
    swing_high = float(htf_df.iloc[-40:]["high"].max())
    eq = (swing_low + swing_high) / 2.0

    if price < eq:
        return "discount", swing_low, swing_high, eq
    return "premium", swing_low, swing_high, eq


# =========================================================
# CANDLE PATTERNS
# =========================================================

def bullish_pin_bar(row):
    body = safe_float(row["body"])
    rng = safe_float(row["range"])
    uw = safe_float(row["upper_wick"])
    lw = safe_float(row["lower_wick"])
    if rng <= 0:
        return False
    if body / rng > MAX_BODY_TO_RANGE_FOR_PIN:
        return False
    return lw >= body * MIN_PIN_WICK_RATIO and uw <= body * 1.25


def bearish_pin_bar(row):
    body = safe_float(row["body"])
    rng = safe_float(row["range"])
    uw = safe_float(row["upper_wick"])
    lw = safe_float(row["lower_wick"])
    if rng <= 0:
        return False
    if body / rng > MAX_BODY_TO_RANGE_FOR_PIN:
        return False
    return uw >= body * MIN_PIN_WICK_RATIO and lw <= body * 1.25


def detect_inside_bar(df):
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


def detect_inside_break(df):
    st = detect_inside_bar(df)
    if not st:
        return None
    last = df.iloc[-1]
    if last["close"] > st["inside_high"]:
        return {"direction": "long", "pattern": "inside_break_long", **st}
    if last["close"] < st["inside_low"]:
        return {"direction": "short", "pattern": "inside_break_short", **st}
    return None


def detect_fakey(df):
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


# =========================================================
# PDH / PDL
# =========================================================

def get_previous_day_levels(day_df):
    if day_df is None or len(day_df) < 3:
        return None
    prev = day_df.iloc[-2]
    return {
        "pdh": float(prev["high"]),
        "pdl": float(prev["low"]),
        "pdo": float(prev["open"]),
        "pdc": float(prev["close"]),
    }


# =========================================================
# ASIAN RANGE / TRAP
# =========================================================

def get_today_ltf_df(ltf_df):
    today = now_utc().date()
    return ltf_df[ltf_df["time"].dt.date == today].copy()


def get_asian_range(ltf_df):
    today_df = get_today_ltf_df(ltf_df)
    if today_df.empty:
        return None
    asia = today_df[(today_df["time"].dt.hour >= ASIA_START) & (today_df["time"].dt.hour < ASIA_END)]
    if asia.empty:
        return None
    return {
        "high": float(asia["high"].max()),
        "low": float(asia["low"].min()),
    }


def detect_session_trap(ltf_df, asian_range, pd_levels):
    """
    London/NY açılışına yakın:
    - Asia high sweep + geri dönüş = short trap
    - Asia low sweep + geri dönüş = long trap
    - PDH/PDL sweep de ek teyit sayılır
    """
    if asian_range is None:
        return None
    if not in_trap_window():
        return None

    last = ltf_df.iloc[-1]
    prev = ltf_df.iloc[-4:-1]
    atr = safe_float(last["atr"])
    if atr <= 0:
        return None

    asia_high = asian_range["high"]
    asia_low = asian_range["low"]

    pdh = pd_levels["pdh"] if pd_levels else None
    pdl = pd_levels["pdl"] if pd_levels else None

    # short trap
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

    # long trap
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


# =========================================================
# FVG
# =========================================================

def detect_recent_fvg(df):
    """
    Basit 3 mumluk FVG:
    bullish: candle[-3].high < candle[-1].low
    bearish: candle[-3].low > candle[-1].high
    """
    if len(df) < 5:
        return None

    a = df.iloc[-3]
    b = df.iloc[-2]
    c = df.iloc[-1]
    atr = safe_float(c["atr"])
    if atr <= 0:
        return None

    # bullish FVG
    if a["high"] < c["low"]:
        gap = c["low"] - a["high"]
        if gap >= atr * MIN_FVG_ATR:
            return {
                "direction": "long",
                "pattern": "bullish_fvg",
                "low": float(a["high"]),
                "high": float(c["low"]),
            }

    # bearish FVG
    if a["low"] > c["high"]:
        gap = a["low"] - c["high"]
        if gap >= atr * MIN_FVG_ATR:
            return {
                "direction": "short",
                "pattern": "bearish_fvg",
                "low": float(c["high"]),
                "high": float(a["low"]),
            }

    return None


def price_in_fvg(price, fvg):
    if not fvg:
        return False
    return fvg["low"] <= price <= fvg["high"]


# =========================================================
# BOS / CHoCH BENZERİ
# =========================================================

def recent_structure_points(df, lookback=25):
    sub = df.iloc[-lookback:].copy()
    highs = []
    lows = []
    for i in range(2, len(sub) - 2):
        if sub.iloc[i]["high"] > sub.iloc[i-1]["high"] and sub.iloc[i]["high"] > sub.iloc[i+1]["high"]:
            highs.append((sub.iloc[i]["time"], float(sub.iloc[i]["high"])))
        if sub.iloc[i]["low"] < sub.iloc[i-1]["low"] and sub.iloc[i]["low"] < sub.iloc[i+1]["low"]:
            lows.append((sub.iloc[i]["time"], float(sub.iloc[i]["low"])))
    return highs, lows


def detect_bos(df):
    """
    Basit kullanım:
    - son kapanış son local high üstünde = bullish BOS
    - son kapanış son local low altında = bearish BOS
    """
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


def detect_choch_like(df):
    """
    Dönüşe yakın yapı:
    - önce lower low sweep, sonra son birkaç mumun tepesini kırarsa bullish reversal
    - önce higher high sweep, sonra son birkaç mumun dibini kırarsa bearish reversal
    """
    if len(df) < 12:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-6:-1]
    atr = safe_float(last["atr"])
    if atr <= 0:
        return None

    local_high = float(prev["high"].max())
    local_low = float(prev["low"].min())

    # bullish reversal
    if last["close"] > local_high and prev.iloc[-1]["low"] < prev["low"].iloc[:-1].min():
        return {"direction": "long", "pattern": "choch_like_long", "level": local_high}

    # bearish reversal
    if last["close"] < local_low and prev.iloc[-1]["high"] > prev["high"].iloc[:-1].max():
        return {"direction": "short", "pattern": "choch_like_short", "level": local_low}

    return None


# =========================================================
# KALİTE FİLTRELERİ
# =========================================================

def spread_ok():
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return False
    return (tick.ask - tick.bid) <= MAX_SPREAD_DOLLAR


def ltf_quality_ok(df, direction):
    last = df.iloc[-1]

    if safe_float(last["adx"]) < MIN_ADX:
        return False

    if direction == "long":
        if safe_float(last["rsi"]) < 49:
            return False
    else:
        if safe_float(last["rsi"]) > 51:
            return False

    return True


# =========================================================
# SİNYAL BİRLEŞTİRME
# =========================================================

def build_signal(htf_df, ltf_df, day_df):
    htf_last = htf_df.iloc[-1]
    ltf_last = ltf_df.iloc[-1]

    htf_atr = safe_float(htf_last["atr"])
    ltf_atr = safe_float(ltf_last["atr"])
    if htf_atr <= 0 or ltf_atr <= 0:
        return None

    price = float(ltf_last["close"])
    htf_trend = get_htf_trend(htf_df)
    zone, swing_low, swing_high, eq = premium_discount_zone(htf_df, price)

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
    fvg = detect_recent_fvg(ltf_df)

    # PDH/PDL yakınlık
    near_pdh = pd_levels is not None and abs(price - pd_levels["pdh"]) <= htf_atr * LEVEL_NEAR_ATR
    near_pdl = pd_levels is not None and abs(price - pd_levels["pdl"]) <= htf_atr * LEVEL_NEAR_ATR

    # LONG SENARYOSU
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

    if fvg and fvg["direction"] == "long" and price_in_fvg(price, fvg):
        long_reasons.append("in_bullish_fvg")

    if (
        ltf_quality_ok(ltf_df, "long")
        and len(long_reasons) >= 3
        and ("htf_support" in long_reasons or "pdl" in long_reasons or zone == "discount")
        and (("fakey_long" in " ".join(long_reasons)) or ("inside_break_long" in " ".join(long_reasons)) or ("session_trap_long" in " ".join(long_reasons)) or ("choch_like_long" in " ".join(long_reasons)))
        and htf_trend in ["bull", "sideways"]
    ):
        stop = min(long_stop_candidates) if long_stop_candidates else price - ltf_atr
        risk = price - stop
        if risk > 0 and risk <= ltf_atr * MAX_ENTRY_DISTANCE_ATR:
            tp1 = price + risk * 1.0
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

    # SHORT SENARYOSU
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

    if fvg and fvg["direction"] == "short" and price_in_fvg(price, fvg):
        short_reasons.append("in_bearish_fvg")

    if (
        ltf_quality_ok(ltf_df, "short")
        and len(short_reasons) >= 3
        and ("htf_resistance" in short_reasons or "pdh" in short_reasons or zone == "premium")
        and (("fakey_short" in " ".join(short_reasons)) or ("inside_break_short" in " ".join(short_reasons)) or ("session_trap_short" in " ".join(short_reasons)) or ("choch_like_short" in " ".join(short_reasons)))
        and htf_trend in ["bear", "sideways"]
    ):
        stop = max(short_stop_candidates) if short_stop_candidates else price + ltf_atr
        risk = stop - price
        if risk > 0 and risk <= ltf_atr * MAX_ENTRY_DISTANCE_ATR:
            tp1 = price - risk * 1.0
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
# LOT / EMİR
# =========================================================

def get_account_balance():
    acc = mt5.account_info()
    if acc is None:
        return 0.0
    return safe_float(acc.balance)


def get_symbol_info():
    info = mt5.symbol_info(SYMBOL)
    if info is None:
        raise RuntimeError(f"{SYMBOL} info yok")
    return info


def calc_lot(entry, stop):
    info = get_symbol_info()
    balance = get_account_balance()
    if balance <= 0:
        return 0.0

    risk_money = balance * RISK_PER_TRADE
    sl_distance = abs(entry - stop)
    if sl_distance <= 0:
        return 0.0

    tick_value = safe_float(info.trade_tick_value)
    tick_size = safe_float(info.trade_tick_size)
    volume_step = safe_float(info.volume_step, 0.01)
    min_lot = safe_float(info.volume_min, 0.01)
    max_lot = safe_float(info.volume_max, 100.0)

    if tick_value <= 0 or tick_size <= 0:
        return 0.0

    loss_per_lot = (sl_distance / tick_size) * tick_value
    if loss_per_lot <= 0:
        return 0.0

    lot = risk_money / loss_per_lot
    lot = math.floor(lot / volume_step) * volume_step
    lot = max(min_lot, min(lot, max_lot))
    return round(lot, 2)


def current_positions():
    positions = mt5.positions_get(symbol=SYMBOL)
    return [] if positions is None else list(positions)


def direction_position_exists(direction):
    positions = current_positions()
    if direction == "long":
        return any(p.type == mt5.POSITION_TYPE_BUY for p in positions)
    return any(p.type == mt5.POSITION_TYPE_SELL for p in positions)


def send_market_order(signal):
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return None

    direction = signal["direction"]
    entry = tick.ask if direction == "long" else tick.bid
    stop = signal["stop"]
    tp2 = signal["tp2"]

    lot = calc_lot(entry, stop)
    if lot <= 0:
        log("Lot hesaplanamadı")
        return None

    order_type = mt5.ORDER_TYPE_BUY if direction == "long" else mt5.ORDER_TYPE_SELL
    price = tick.ask if direction == "long" else tick.bid

    msg = (
        f"🚨 XAUUSD V2 SİNYAL\n"
        f"Yön: {direction}\n"
        f"Pattern: {signal['pattern']}\n"
        f"HTF Trend: {signal['htf_trend']}\n"
        f"Zone: {signal['zone']}\n"
        f"Seviye: {signal['level_type']} @ {signal['level_price']:.2f}\n"
        f"Entry: {price:.2f}\n"
        f"SL: {stop:.2f}\n"
        f"TP1: {signal['tp1']:.2f}\n"
        f"TP2: {tp2:.2f}\n"
        f"Lot: {lot}"
    )
    log(msg)
    send_telegram(msg)

    if not LIVE_TRADING:
        log("Dry run mod")
        return {"dry_run": True}

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": stop,
        "tp": tp2,
        "deviation": 20,
        "magic": MAGIC,
        "comment": "XAUUSD_V2_PRO",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None:
        return None

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        err = f"Emir reddedildi retcode={result.retcode}"
        log(err)
        send_telegram(err)
        return None

    ok = f"✅ XAUUSD V2 emir açıldı ticket={result.order} lot={lot}"
    log(ok)
    send_telegram(ok)
    return result


# =========================================================
# POZİSYON YÖNETİMİ
# =========================================================

def modify_position_sl_tp(ticket, new_sl=None, new_tp=None):
    pos = mt5.positions_get(ticket=ticket)
    if pos is None or len(pos) == 0:
        return False
    p = pos[0]

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": p.ticket,
        "symbol": p.symbol,
        "sl": p.sl if new_sl is None else new_sl,
        "tp": p.tp if new_tp is None else new_tp,
        "magic": MAGIC,
    }

    result = mt5.order_send(request)
    return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE


def partial_close(ticket, volume):
    pos = mt5.positions_get(ticket=ticket)
    if pos is None or len(pos) == 0:
        return False
    p = pos[0]

    tick = mt5.symbol_info_tick(p.symbol)
    if tick is None:
        return False

    close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": p.symbol,
        "volume": volume,
        "type": close_type,
        "position": p.ticket,
        "price": price,
        "deviation": 20,
        "magic": MAGIC,
        "comment": "partial_close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE


def manage_positions():
    positions = current_positions()
    if not positions:
        return

    for p in positions:
        if p.symbol != SYMBOL:
            continue

        entry = safe_float(p.price_open)
        sl = safe_float(p.sl)
        volume = safe_float(p.volume)
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            continue

        current_price = tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask
        risk = abs(entry - sl)
        if risk <= 0:
            continue

        if p.type == mt5.POSITION_TYPE_BUY:
            tp1 = entry + risk * 1.0
            if current_price >= tp1:
                partial_vol = round(volume * PARTIAL_CLOSE_AT_TP1, 2)
                if partial_vol > 0 and partial_vol < volume:
                    partial_close(p.ticket, partial_vol)
                if MOVE_SL_TO_BE_AFTER_TP1:
                    modify_position_sl_tp(p.ticket, new_sl=entry)

        else:
            tp1 = entry - risk * 1.0
            if current_price <= tp1:
                partial_vol = round(volume * PARTIAL_CLOSE_AT_TP1, 2)
                if partial_vol > 0 and partial_vol < volume:
                    partial_close(p.ticket, partial_vol)
                if MOVE_SL_TO_BE_AFTER_TP1:
                    modify_position_sl_tp(p.ticket, new_sl=entry)


# =========================================================
# BAR KONTROL
# =========================================================

last_bar_time = None

def is_new_ltf_bar(df):
    global last_bar_time
    t = df.iloc[-1]["time"]
    if last_bar_time is None:
        last_bar_time = t
        return True
    if t != last_bar_time:
        last_bar_time = t
        return True
    return False


# =========================================================
# ANA ÇALIŞMA
# =========================================================

def run_once():
    if not in_main_sessions():
        log("Ana session dışında")
        return

    if not spread_ok():
        log("Spread yüksek")
        return

    htf_df = get_rates(SYMBOL, HTF, HTF_BARS)
    ltf_df = get_rates(SYMBOL, LTF, LTF_BARS)
    day_df = get_rates(SYMBOL, DAY_TF, DAY_BARS)

    if htf_df is None or ltf_df is None or day_df is None:
        log("Veri alınamadı")
        return

    htf_df = add_indicators(htf_df)
    ltf_df = add_indicators(ltf_df)
    day_df = add_indicators(day_df)

    manage_positions()

    if not is_new_ltf_bar(ltf_df):
        return

    signal = build_signal(htf_df, ltf_df, day_df)
    if signal is None:
        log("Sinyal yok")
        return

    if direction_position_exists(signal["direction"]):
        log(f"Aynı yönde açık pozisyon var: {signal['direction']}")
        return

    send_market_order(signal)


def main():
    init_mt5()
    log("XAUUSD V2 PRO başladı")
    send_telegram("🤖 XAUUSD V2 PRO başlatıldı")

    while True:
        try:
            run_once()
        except Exception as e:
            err = f"HATA: {e}\n{traceback.format_exc()}"
            log(err)
            send_telegram(err)
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
