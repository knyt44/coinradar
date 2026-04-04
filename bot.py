# -*- coding: utf-8 -*-
"""
COINRADAR TURBO - MEXC FUTURES SIGNAL BOT
Tek dosya / tek parça / daha hızlı ve daha gerçekçi sinyal akışı

Kurulum:
    pip install requests pandas

Çalıştırma:
    python coinradar_turbo.py
"""

import os
import time
import html
import json
import math
import traceback
from typing import List, Optional, Dict, Tuple

import requests
import pandas as pd


# =========================================================
# TELEGRAM
# =========================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "BURAYA_TOKEN").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID").strip()

# =========================================================
# GENEL
# =========================================================
MEXC_BASE = "https://contract.mexc.com"
HTTP_TIMEOUT = 15
CHECK_EVERY_SECONDS = 70

STATE_FILE = "coinradar_turbo_state.json"

SEND_STARTUP_MESSAGE = True
SEND_HEARTBEAT_IF_NO_SIGNAL = True
HEARTBEAT_EVERY_MINUTES = 25

TOP_N_SIGNALS = 3
SIGNAL_COOLDOWN_MINUTES = 45

# Tarama mimarisi
MAX_SYMBOLS_TO_SCAN = 180        # ilk evren
STAGE1_KEEP = 36                 # 15m ön elemeden sonra kalacak aday
KLINE_LIMIT = 260

TF_ENTRY = "Min5"
TF_CONFIRM = "Min15"

# =========================================================
# EVREN / MARKET FİLTRESİ
# =========================================================
REQUIRE_USDT_PERP = True

MIN_AMOUNT24_USDT = 1_800_000
MIN_HOLDVOL = 18_000
MAX_SPREAD_PCT = 0.45
MAX_24H_PUMP_PCT = 24.0
MIN_PRICE = 0.00001

BLACKLIST_KEYWORDS = {
    "1000", "10000", "100000",
    "BULL", "BEAR",
    "USTC"
}

# =========================================================
# İNDİKATÖRLER
# =========================================================
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
VOL_MA_PERIOD = 20
BREAKOUT_LOOKBACK = 20

SL_ATR_MULT = 1.12
TP1_ATR_MULT = 1.10
TP2_ATR_MULT = 1.85
TP3_ATR_MULT = 2.80

MIN_RR_TO_TP2 = 1.05
MIN_FINAL_SCORE = 5.9

MAX_DISTANCE_FROM_EMA20_ATR = 3.2
MAX_LAST_CANDLE_RANGE_ATR = 3.5

BTC_SYMBOL = "BTC_USDT"
USE_BTC_BIAS = True

# =========================================================
# SESSION
# =========================================================
session = requests.Session()
session.headers.update({"User-Agent": "CoinRadar-Turbo/4.0"})


# =========================================================
# YARDIMCI
# =========================================================
def now_ts() -> int:
    return int(time.time())


def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def fmt_price(x: float) -> str:
    if x >= 1000:
        return f"{x:.2f}"
    if x >= 100:
        return f"{x:.3f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.5f}"
    return f"{x:.8f}".rstrip("0").rstrip(".")


def validate_telegram_config():
    if (
        not TELEGRAM_BOT_TOKEN
        or TELEGRAM_BOT_TOKEN == "BURAYA_TOKEN"
        or ":" not in TELEGRAM_BOT_TOKEN
    ):
        raise ValueError("TELEGRAM_BOT_TOKEN geçersiz.")
    if (
        not TELEGRAM_CHAT_ID
        or TELEGRAM_CHAT_ID == "BURAYA_CHAT_ID"
    ):
        raise ValueError("TELEGRAM_CHAT_ID geçersiz.")


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "last_sent": {},
            "last_heartbeat": 0
        }


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# =========================================================
# TELEGRAM
# =========================================================
def telegram_send_html(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=payload, timeout=HTTP_TIMEOUT)
        if r.ok:
            return True
        print("Telegram status:", r.status_code)
        print("Telegram response:", r.text[:400])
        return False
    except Exception as e:
        print("Telegram exception:", e)
        return False


def telegram_test() -> bool:
    return telegram_send_html("✅ <b>COINRADAR TURBO TEST</b>\n\nTelegram bağlantısı başarılı.")


# =========================================================
# MEXC API
# =========================================================
def mexc_get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{MEXC_BASE}{path}"
    r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("Beklenmeyen API cevabı")
    if data.get("success") is False:
        raise ValueError(f"MEXC API error: {data}")
    return data


def get_contract_detail() -> List[dict]:
    data = mexc_get("/api/v1/contract/detail")
    return data.get("data", []) or []


def get_tickers() -> List[dict]:
    data = mexc_get("/api/v1/contract/ticker")
    return data.get("data", []) or []


def get_kline(symbol: str, interval: str, limit: int = 260) -> pd.DataFrame:
    end_ = now_ts()
    seconds_per_bar = {
        "Min1": 60,
        "Min5": 300,
        "Min15": 900,
        "Min30": 1800,
        "Min60": 3600,
        "Hour4": 14400,
        "Day1": 86400,
    }.get(interval, 300)

    start_ = end_ - (limit + 30) * seconds_per_bar

    data = mexc_get(
        f"/api/v1/contract/kline/{symbol}",
        params={"interval": interval, "start": start_, "end": end_}
    ).get("data", {}) or {}

    df = pd.DataFrame({
        "time": data.get("time", []),
        "open": data.get("open", []),
        "high": data.get("high", []),
        "low": data.get("low", []),
        "close": data.get("close", []),
        "vol": data.get("vol", []),
        "amount": data.get("amount", []),
    })

    if df.empty:
        return df

    for col in ["open", "high", "low", "close", "vol", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna().reset_index(drop=True)
    if len(df) > limit:
        df = df.iloc[-limit:].reset_index(drop=True)
    return df


# =========================================================
# İNDİKATÖRLER
# =========================================================
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)

    ma_up = up.ewm(alpha=1 / period, adjust=False).mean()
    ma_down = down.ewm(alpha=1 / period, adjust=False).mean()

    rs = ma_up / ma_down.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    return out.astype("float64").fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean().astype("float64")


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = pd.Series(0.0, index=df.index, dtype="float64")
    minus_dm = pd.Series(0.0, index=df.index, dtype="float64")

    plus_mask = (up_move > down_move) & (up_move > 0)
    minus_mask = (down_move > up_move) & (down_move > 0)

    plus_dm.loc[plus_mask] = up_move.loc[plus_mask].astype("float64")
    minus_dm.loc[minus_mask] = down_move.loc[minus_mask].astype("float64")

    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs()
    ], axis=1).max(axis=1)

    atr_ = tr.ewm(alpha=1 / period, adjust=False).mean().replace(0, pd.NA)
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)) * 100

    return dx.ewm(alpha=1 / period, adjust=False).mean().astype("float64").fillna(0.0)


def enrich_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = ema(out["close"], EMA_FAST)
    out["ema50"] = ema(out["close"], EMA_MID)
    out["ema200"] = ema(out["close"], EMA_SLOW)
    out["rsi"] = rsi(out["close"], RSI_PERIOD)
    out["atr"] = atr(out, ATR_PERIOD)
    out["adx"] = adx(out, ADX_PERIOD)
    out["vol_ma"] = out["vol"].rolling(VOL_MA_PERIOD).mean()
    out["body"] = (out["close"] - out["open"]).abs()
    out["range"] = (out["high"] - out["low"]).abs()
    out["upper_wick"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_wick"] = out[["open", "close"]].min(axis=1) - out["low"]
    return out


# =========================================================
# EVREN
# =========================================================
def is_blacklisted(symbol: str) -> bool:
    s = symbol.upper()
    return any(k in s for k in BLACKLIST_KEYWORDS)


def build_market_universe() -> List[dict]:
    details = get_contract_detail()
    tickers = get_tickers()
    ticker_map = {x.get("symbol"): x for x in tickers if x.get("symbol")}

    rows = []
    for d in details:
        symbol = d.get("symbol")
        if not symbol or symbol not in ticker_map:
            continue

        if REQUIRE_USDT_PERP and not symbol.endswith("_USDT"):
            continue

        if is_blacklisted(symbol):
            continue

        t = ticker_map[symbol]
        last_price = safe_float(t.get("lastPrice"))
        amount24 = safe_float(t.get("amount24"))
        hold_vol = safe_float(t.get("holdVol"))
        bid1 = safe_float(t.get("bid1"))
        ask1 = safe_float(t.get("ask1"))
        rise_fall_rate = safe_float(t.get("riseFallRate")) * 100.0

        if last_price < MIN_PRICE:
            continue
        if amount24 < MIN_AMOUNT24_USDT:
            continue
        if hold_vol < MIN_HOLDVOL:
            continue
        if abs(rise_fall_rate) > MAX_24H_PUMP_PCT:
            continue

        spread_pct = 999.0
        if bid1 > 0 and ask1 > 0:
            mid = (bid1 + ask1) / 2.0
            if mid > 0:
                spread_pct = ((ask1 - bid1) / mid) * 100.0

        if spread_pct > MAX_SPREAD_PCT:
            continue

        liquidity_score = (
            math.log10(max(amount24, 1.0)) * 0.75 +
            math.log10(max(hold_vol, 1.0)) * 0.25
        )

        rows.append({
            "symbol": symbol,
            "last_price": last_price,
            "amount24": amount24,
            "hold_vol": hold_vol,
            "spread_pct": spread_pct,
            "rise_fall_pct": rise_fall_rate,
            "liquidity_score": liquidity_score,
        })

    rows.sort(key=lambda x: (x["liquidity_score"], x["amount24"]), reverse=True)
    return rows[:MAX_SYMBOLS_TO_SCAN]


# =========================================================
# BTC BIAS
# =========================================================
def get_btc_bias() -> str:
    try:
        df = get_kline(BTC_SYMBOL, TF_CONFIRM, 220)
        if len(df) < 200:
            return "NEUTRAL"

        df = enrich_indicators(df)
        last = df.iloc[-1]

        bullish = (
            last["close"] > last["ema20"] > last["ema50"]
            and last["rsi"] >= 52
        )
        bearish = (
            last["close"] < last["ema20"] < last["ema50"]
            and last["rsi"] <= 48
        )

        if bullish:
            return "BULLISH"
        if bearish:
            return "BEARISH"
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"


# =========================================================
# STAGE 1 - 15M ÖN ELEME
# =========================================================
def trend_points_long(last15) -> float:
    s = 0.0
    if last15["close"] > last15["ema20"]:
        s += 1.0
    if last15["ema20"] > last15["ema50"]:
        s += 1.0
    if last15["ema50"] > last15["ema200"]:
        s += 1.0
    return s


def trend_points_short(last15) -> float:
    s = 0.0
    if last15["close"] < last15["ema20"]:
        s += 1.0
    if last15["ema20"] < last15["ema50"]:
        s += 1.0
    if last15["ema50"] < last15["ema200"]:
        s += 1.0
    return s


def breakout_flags(df: pd.DataFrame) -> Tuple[bool, bool]:
    if len(df) < BREAKOUT_LOOKBACK + 2:
        return False, False
    prev_high = df["high"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].max()
    prev_low = df["low"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].min()
    last = df.iloc[-1]
    return last["close"] > prev_high, last["close"] < prev_low


def stage1_rank_symbol(symbol: str, market_info: dict, btc_bias: str) -> Optional[dict]:
    df15 = get_kline(symbol, TF_CONFIRM, KLINE_LIMIT)
    if len(df15) < 230:
        return None

    df15 = enrich_indicators(df15)
    last = df15.iloc[-1]
    prev = df15.iloc[-2]

    if last["atr"] <= 0:
        return None

    rng_atr = (last["range"] / last["atr"]) if last["atr"] > 0 else 999
    if rng_atr > MAX_LAST_CANDLE_RANGE_ATR:
        return None

    vol_ratio = 1.0
    if pd.notna(last["vol_ma"]) and last["vol_ma"] > 0:
        vol_ratio = float(last["vol"] / last["vol_ma"])

    trend_l = trend_points_long(last)
    trend_s = trend_points_short(last)
    breakout_l, breakout_s = breakout_flags(df15)

    long_score = 0.0
    short_score = 0.0

    # LONG ön puan
    if trend_l >= 2.0:
        long_score += trend_l * 1.1
        long_score += clamp((float(last["rsi"]) - 50.0) * 0.10, 0.0, 1.8)
        long_score += clamp((float(last["adx"]) - 10.0) * 0.10, 0.0, 1.8)
        long_score += clamp((vol_ratio - 1.0) * 1.0, 0.0, 1.4)
        if breakout_l:
            long_score += 1.1
        if last["close"] > last["ema20"] and prev["close"] <= prev["ema20"]:
            long_score += 0.7

    # SHORT ön puan
    if trend_s >= 2.0:
        short_score += trend_s * 1.1
        short_score += clamp((50.0 - float(last["rsi"])) * 0.10, 0.0, 1.8)
        short_score += clamp((float(last["adx"]) - 10.0) * 0.10, 0.0, 1.8)
        short_score += clamp((vol_ratio - 1.0) * 1.0, 0.0, 1.4)
        if breakout_s:
            short_score += 1.1
        if last["close"] < last["ema20"] and prev["close"] >= prev["ema20"]:
            short_score += 0.7

    # BTC bias etkisi
    if USE_BTC_BIAS:
        if btc_bias == "BULLISH":
            long_score += 0.35
            short_score -= 0.20
        elif btc_bias == "BEARISH":
            short_score += 0.35
            long_score -= 0.20

    best_side = None
    best_score = 0.0
    if long_score > short_score and long_score > 0:
        best_side = "LONG"
        best_score = long_score
    elif short_score > long_score and short_score > 0:
        best_side = "SHORT"
        best_score = short_score

    if best_side is None:
        return None

    return {
        "symbol": symbol,
        "market_info": market_info,
        "side_hint": best_side,
        "stage1_score": round(best_score, 2),
        "rsi15": round(float(last["rsi"]), 1),
        "adx15": round(float(last["adx"]), 1),
        "vol_ratio15": round(vol_ratio, 2),
    }


# =========================================================
# STAGE 2 - 5M GİRİŞ
# =========================================================
def pullback_long(last5, prev5) -> bool:
    return (
        last5["close"] > last5["ema20"]
        and last5["low"] <= last5["ema20"] * 1.004
        and last5["close"] > last5["open"]
        and prev5["close"] >= prev5["ema20"] * 0.994
    )


def pullback_short(last5, prev5) -> bool:
    return (
        last5["close"] < last5["ema20"]
        and last5["high"] >= last5["ema20"] * 0.996
        and last5["close"] < last5["open"]
        and prev5["close"] <= prev5["ema20"] * 1.006
    )


def reclaim_long(last5, prev5) -> bool:
    return (
        prev5["close"] <= prev5["ema20"]
        and last5["close"] > last5["ema20"]
        and last5["close"] > last5["open"]
    )


def reject_short(last5, prev5) -> bool:
    return (
        prev5["close"] >= prev5["ema20"]
        and last5["close"] < last5["ema20"]
        and last5["close"] < last5["open"]
    )


def wick_ok_long(last5) -> bool:
    body = max(last5["body"], 1e-12)
    return (last5["upper_wick"] / body) <= 4.5


def wick_ok_short(last5) -> bool:
    body = max(last5["body"], 1e-12)
    return (last5["lower_wick"] / body) <= 4.5


def compute_rr(side: str, entry: float, sl: float, tp2: float) -> float:
    if side == "LONG":
        risk = entry - sl
        reward = tp2 - entry
    else:
        risk = sl - entry
        reward = entry - tp2

    if risk <= 0:
        return 0.0
    return reward / risk


def build_signal_from_candidate(candidate: dict, btc_bias: str) -> Optional[dict]:
    symbol = candidate["symbol"]
    side_hint = candidate["side_hint"]
    market_info = candidate["market_info"]

    df5 = get_kline(symbol, TF_ENTRY, KLINE_LIMIT)
    if len(df5) < 230:
        return None

    df5 = enrich_indicators(df5)
    last5 = df5.iloc[-1]
    prev5 = df5.iloc[-2]

    if last5["atr"] <= 0:
        return None

    rng_atr = (last5["range"] / last5["atr"]) if last5["atr"] > 0 else 999
    if rng_atr > MAX_LAST_CANDLE_RANGE_ATR:
        return None

    dist_ema_atr = abs(last5["close"] - last5["ema20"]) / last5["atr"]
    if dist_ema_atr > MAX_DISTANCE_FROM_EMA20_ATR:
        return None

    vol_ratio = 1.0
    if pd.notna(last5["vol_ma"]) and last5["vol_ma"] > 0:
        vol_ratio = float(last5["vol"] / last5["vol_ma"])

    bo_long, bo_short = breakout_flags(df5)
    pb_long = pullback_long(last5, prev5)
    pb_short = pullback_short(last5, prev5)
    rc_long = reclaim_long(last5, prev5)
    rj_short = reject_short(last5, prev5)

    side = None
    setup = None
    entry_score = 0.0

    if side_hint == "LONG":
        if bo_long:
            side = "LONG"
            setup = "BREAKOUT"
            entry_score += 1.25
        elif pb_long:
            side = "LONG"
            setup = "PULLBACK"
            entry_score += 1.05
        elif rc_long:
            side = "LONG"
            setup = "RECLAIM"
            entry_score += 0.90

        if side == "LONG":
            entry_score += clamp((float(last5["rsi"]) - 50.0) * 0.10, 0.0, 1.5)
            entry_score += clamp((float(last5["adx"]) - 10.0) * 0.10, 0.0, 1.5)
            entry_score += clamp((vol_ratio - 1.0) * 1.0, 0.0, 1.3)
            if not wick_ok_long(last5):
                entry_score -= 0.45

    elif side_hint == "SHORT":
        if bo_short:
            side = "SHORT"
            setup = "BREAKDOWN"
            entry_score += 1.25
        elif pb_short:
            side = "SHORT"
            setup = "PULLBACK"
            entry_score += 1.05
        elif rj_short:
            side = "SHORT"
            setup = "REJECT"
            entry_score += 0.90

        if side == "SHORT":
            entry_score += clamp((50.0 - float(last5["rsi"])) * 0.10, 0.0, 1.5)
            entry_score += clamp((float(last5["adx"]) - 10.0) * 0.10, 0.0, 1.5)
            entry_score += clamp((vol_ratio - 1.0) * 1.0, 0.0, 1.3)
            if not wick_ok_short(last5):
                entry_score -= 0.45

    if side is None:
        return None

    entry = float(last5["close"])
    atrv = float(last5["atr"])

    if side == "LONG":
        sl = entry - atrv * SL_ATR_MULT
        tp1 = entry + atrv * TP1_ATR_MULT
        tp2 = entry + atrv * TP2_ATR_MULT
        tp3 = entry + atrv * TP3_ATR_MULT
    else:
        sl = entry + atrv * SL_ATR_MULT
        tp1 = entry - atrv * TP1_ATR_MULT
        tp2 = entry - atrv * TP2_ATR_MULT
        tp3 = entry - atrv * TP3_ATR_MULT

    rr = compute_rr(side, entry, sl, tp2)
    if rr < MIN_RR_TO_TP2:
        return None

    score = candidate["stage1_score"] + entry_score
    score += clamp(rr * 1.0, 0.0, 2.1)

    if market_info["amount24"] >= 12_000_000:
        score += 0.8
    elif market_info["amount24"] >= 6_000_000:
        score += 0.45
    elif market_info["amount24"] >= 3_000_000:
        score += 0.20

    score -= clamp(market_info["spread_pct"] * 2.2, 0.0, 1.2)

    if USE_BTC_BIAS:
        if btc_bias == "BULLISH":
            if side == "LONG":
                score += 0.35
            else:
                score -= 0.20
        elif btc_bias == "BEARISH":
            if side == "SHORT":
                score += 0.35
            else:
                score -= 0.20

    score = round(clamp(score, 1.0, 10.0), 1)

    if score < MIN_FINAL_SCORE:
        return None

    return {
        "symbol": symbol,
        "side": side,
        "setup": setup,
        "entry": round(entry, 10),
        "sl": round(sl, 10),
        "tp1": round(tp1, 10),
        "tp2": round(tp2, 10),
        "tp3": round(tp3, 10),
        "rr": round(rr, 2),
        "score": score,
        "spread_pct": round(market_info["spread_pct"], 3),
        "amount24": market_info["amount24"],
        "hold_vol": market_info["hold_vol"],
        "rsi15": candidate["rsi15"],
        "adx15": candidate["adx15"],
        "vol_ratio15": candidate["vol_ratio15"],
        "rsi5": round(float(last5["rsi"]), 1),
        "adx5": round(float(last5["adx"]), 1),
        "vol_ratio5": round(vol_ratio, 2),
        "btc_bias": btc_bias,
        "tf": "15m trend / 5m entry",
    }


# =========================================================
# FORMAT
# =========================================================
def format_signal_message(sig: dict) -> str:
    symbol = html.escape(sig["symbol"].replace("_", "/"))
    side = html.escape(sig["side"])
    setup = html.escape(sig["setup"])
    tf = html.escape(sig["tf"])
    emoji = "🟢" if sig["side"] == "LONG" else "🔴"

    return (
        f"🚨 <b>COINRADAR TURBO SIGNAL</b>\n\n"
        f"{emoji} <b>{side} | {symbol}</b>\n"
        f"<b>Setup:</b> {setup}\n"
        f"<b>TF:</b> {tf}\n\n"
        f"<b>Entry:</b> {fmt_price(sig['entry'])}\n"
        f"<b>Stop:</b> {fmt_price(sig['sl'])}\n"
        f"<b>TP1:</b> {fmt_price(sig['tp1'])}\n"
        f"<b>TP2:</b> {fmt_price(sig['tp2'])}\n"
        f"<b>TP3:</b> {fmt_price(sig['tp3'])}\n\n"
        f"<b>R/R:</b> {sig['rr']}\n"
        f"<b>Score:</b> {sig['score']}/10\n"
        f"<b>15m RSI/ADX:</b> {sig['rsi15']} / {sig['adx15']}\n"
        f"<b>5m RSI/ADX:</b> {sig['rsi5']} / {sig['adx5']}\n"
        f"<b>15m Vol x:</b> {sig['vol_ratio15']}\n"
        f"<b>5m Vol x:</b> {sig['vol_ratio5']}\n"
        f"<b>Spread:</b> {sig['spread_pct']}%\n"
        f"<b>BTC Bias:</b> {html.escape(sig['btc_bias'])}"
    )


def format_startup_message() -> str:
    return (
        "✅ <b>COINRADAR TURBO başladı</b>\n\n"
        f"Maks evren: <b>{MAX_SYMBOLS_TO_SCAN}</b>\n"
        f"Stage1 aday: <b>{STAGE1_KEEP}</b>\n"
        f"Telegram sinyal: <b>{TOP_N_SIGNALS}</b>\n"
        "15m ön eleme + 5m giriş sistemi aktif."
    )


def format_heartbeat_message(diag: dict, btc_bias: str) -> str:
    return (
        "ℹ️ <b>COINRADAR TURBO aktif</b>\n\n"
        f"<b>Universe:</b> {diag['universe']}\n"
        f"<b>Stage1 geçen:</b> {diag['stage1_pass']}\n"
        f"<b>Stage2 sinyal:</b> {diag['stage2_pass']}\n"
        f"<b>Cooldown takılan:</b> {diag['cooldown_block']}\n"
        f"<b>BTC Bias:</b> {html.escape(btc_bias)}\n\n"
        "Bu turda gönderilecek net sinyal çıkmadı."
    )


def format_error_message(err: str) -> str:
    txt = html.escape(err[:900])
    return f"⚠️ <b>COINRADAR TURBO HATA</b>\n\n<code>{txt}</code>"


# =========================================================
# COOLDOWN / SEÇİM
# =========================================================
def is_on_cooldown(state: dict, symbol: str, side: str) -> bool:
    key = f"{symbol}:{side}"
    last_ts = state.get("last_sent", {}).get(key, 0)
    return (now_ts() - last_ts) < SIGNAL_COOLDOWN_MINUTES * 60


def mark_sent(state: dict, symbol: str, side: str):
    key = f"{symbol}:{side}"
    state.setdefault("last_sent", {})[key] = now_ts()


def dedupe_signals(signals: List[dict]) -> List[dict]:
    out = []
    seen = set()
    for sig in signals:
        key = sig["symbol"]
        if key in seen:
            continue
        seen.add(key)
        out.append(sig)
    return out


def pick_top_signals(signals: List[dict], n: int) -> List[dict]:
    signals = dedupe_signals(signals)
    signals = sorted(
        signals,
        key=lambda x: (x["score"], x["rr"], x["amount24"], x["vol_ratio5"]),
        reverse=True
    )
    return signals[:n]


# =========================================================
# ANA DÖNGÜ
# =========================================================
def run():
    state = load_state()

    if SEND_STARTUP_MESSAGE:
        telegram_send_html(format_startup_message())

    while True:
        cycle_start = time.time()
        try:
            diag = {
                "universe": 0,
                "stage1_pass": 0,
                "stage2_pass": 0,
                "cooldown_block": 0,
            }

            universe = build_market_universe()
            btc_bias = get_btc_bias()
            diag["universe"] = len(universe)

            print(f"[INFO] Universe={len(universe)} BTC={btc_bias}")

            # Stage 1
            stage1_candidates = []
            for item in universe:
                sym = item["symbol"]
                try:
                    c = stage1_rank_symbol(sym, item, btc_bias)
                    if c:
                        stage1_candidates.append(c)
                except Exception as e:
                    print(f"[WARN] Stage1 fail {sym}: {e}")

            stage1_candidates = sorted(
                stage1_candidates,
                key=lambda x: x["stage1_score"],
                reverse=True
            )[:STAGE1_KEEP]

            diag["stage1_pass"] = len(stage1_candidates)

            # Stage 2
            signals = []
            for c in stage1_candidates:
                sym = c["symbol"]
                try:
                    sig = build_signal_from_candidate(c, btc_bias)
                    if not sig:
                        continue

                    if is_on_cooldown(state, sig["symbol"], sig["side"]):
                        diag["cooldown_block"] += 1
                        continue

                    signals.append(sig)
                except Exception as e:
                    print(f"[WARN] Stage2 fail {sym}: {e}")

            diag["stage2_pass"] = len(signals)

            top_signals = pick_top_signals(signals, TOP_N_SIGNALS)

            if top_signals:
                for sig in top_signals:
                    ok = telegram_send_html(format_signal_message(sig))
                    if ok:
                        mark_sent(state, sig["symbol"], sig["side"])
                        print(
                            f"[SENT] {sig['side']} {sig['symbol']} "
                            f"score={sig['score']} rr={sig['rr']} setup={sig['setup']}"
                        )
                    else:
                        print(f"[FAIL] Telegram gönderilemedi: {sig['symbol']}")
            else:
                print("[INFO] No sendable signal.")
                if SEND_HEARTBEAT_IF_NO_SIGNAL:
                    last_hb = state.get("last_heartbeat", 0)
                    if (now_ts() - last_hb) >= HEARTBEAT_EVERY_MINUTES * 60:
                        if telegram_send_html(format_heartbeat_message(diag, btc_bias)):
                            state["last_heartbeat"] = now_ts()

            save_state(state)

        except Exception as e:
            err = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
            print("[ERROR]", err)
            telegram_send_html(format_error_message(err))

        elapsed = time.time() - cycle_start
        sleep_for = max(5, CHECK_EVERY_SECONDS - int(elapsed))
        time.sleep(sleep_for)


if __name__ == "__main__":
    validate_telegram_config()
    test_ok = telegram_test()
    print("Telegram test:", "OK" if test_ok else "FAIL")
    run()
