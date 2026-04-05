# -*- coding: utf-8 -*-
"""
BINANCE DATA PRO SIGNAL BOT V4
- Binance public spot market data
- 1H trend + 15M entry
- Long + Short signal engine
- Fixed whitelist
- Telegram optional
- TP1 / TP2 / TP3 / SL
- Active signal tracking
- Single-file version

KURULUM:
pip install requests pandas numpy

ENV:
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID

ÇALIŞTIR:
python bot.py
"""

import os
import time
import json
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import numpy as np
import pandas as pd


# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://api.binance.com"

SYMBOL_RULES = {
    "BTCUSDT":   {"allow_long": True, "allow_short": True,  "short_tier": "majors"},
    "ETHUSDT":   {"allow_long": True, "allow_short": True,  "short_tier": "majors"},
    "PAXGUSDT":  {"allow_long": True, "allow_short": True,  "short_tier": "safe"},
    "SEIUSDT":   {"allow_long": True, "allow_short": False, "short_tier": "none"},
    "DOGEUSDT":  {"allow_long": True, "allow_short": False, "short_tier": "none"},
    "ARBUSDT":   {"allow_long": True, "allow_short": True,  "short_tier": "alts"},
    "TRUMPUSDT": {"allow_long": False, "allow_short": False, "short_tier": "none"},  # Binance spotta yoksa otomatik geçilecek
    "TAOUSDT":   {"allow_long": True, "allow_short": False, "short_tier": "none"},
    "FETUSDT":   {"allow_long": True, "allow_short": True,  "short_tier": "alts"},
    "RNDRUSDT":  {"allow_long": True, "allow_short": True,  "short_tier": "alts"},
    "APTUSDT":   {"allow_long": True, "allow_short": True,  "short_tier": "alts"},
}

STATE_FILE = "binance_data_pro_signal_bot_v4_state.json"
LOG_FILE = "binance_data_pro_signal_bot_v4.log"

SCAN_INTERVAL_SECONDS = 60
TF_TREND = "1h"
TF_ENTRY = "15m"
LIVE_LIMIT_1H = 320
LIVE_LIMIT_15M = 320

REQUIRE_SESSION_FILTER = False
REQUIRE_BTC_CONFIRMATION = True
BTC_CONFIRMATION_SYMBOL = "BTCUSDT"
ETH_CONFIRMATION_SYMBOL = "ETHUSDT"

BTC_TREND_RSI_MIN_LONG = 53
BTC_TREND_RSI_MAX_SHORT = 47
ETH_TREND_RSI_MIN_LONG = 52
ETH_TREND_RSI_MAX_SHORT = 48

MIN_QUOTE_VOLUME_USDT_24H = 5_000_000
MIN_TRADES_24H = 10_000
MAX_SPREAD_PCT = 0.45
MIN_24H_CHANGE_PCT = -12.0
MAX_24H_PUMP_PCT_FOR_LONG = 12.0
MAX_24H_DUMP_PCT_FOR_SHORT = -12.0
MAX_24H_PUMP_PCT_FOR_SHORT = 8.0

MIN_ATR_PCT_15M_LONG = 0.0025
MAX_ATR_PCT_15M_LONG = 0.0340
MIN_ATR_PCT_15M_SHORT = 0.0028
MAX_ATR_PCT_15M_SHORT = 0.0310

RSI_TREND_LONG_MIN = 54
RSI_TREND_SHORT_MAX = 46

RSI_ENTRY_LONG_MIN = 49
RSI_ENTRY_LONG_MAX = 64
RSI_ENTRY_SHORT_MIN = 34
RSI_ENTRY_SHORT_MAX = 50

ADX_MIN_LONG = 19
ADX_MIN_SHORT = 20

MIN_VOLUME_FACTOR_LONG = 1.08
MIN_VOLUME_FACTOR_SHORT = 1.12

MAX_DISTANCE_FROM_EMA20_ATR_LONG = 1.15
MAX_DISTANCE_FROM_EMA20_ATR_SHORT = 1.00

MAX_LAST_CANDLE_BODY_ATR_LONG = 1.10
MAX_LAST_CANDLE_BODY_ATR_SHORT = 1.00

MIN_RECLAIM_BODY_RATIO_LONG = 0.48
MIN_RECLAIM_BODY_RATIO_SHORT = 0.52

MAX_WICK_TO_BODY_RATIO_LONG = 3.0
MAX_WICK_TO_BODY_RATIO_SHORT = 2.6

SL_ATR_MULT_LONG = 1.15
SL_ATR_MULT_SHORT = 1.10

TP1_R_MULT_LONG = 1.15
TP2_R_MULT_LONG = 2.20
TP3_R_MULT_LONG = 3.20

TP1_R_MULT_SHORT = 1.00
TP2_R_MULT_SHORT = 2.00
TP3_R_MULT_SHORT = 2.90

MIN_RISK_PCT_LONG = 0.28
MAX_RISK_PCT_LONG = 1.90
MIN_RISK_PCT_SHORT = 0.28
MAX_RISK_PCT_SHORT = 1.70

MIN_SCORE_TO_SIGNAL_LONG = 21.0
MIN_SCORE_TO_SIGNAL_SHORT = 23.0

BASE_COOLDOWN_HOURS = 8
LOSS_COOLDOWN_HOURS = 12
WIN_COOLDOWN_HOURS = 5
ACTIVE_TRADE_MAX_AGE_HOURS = 30

MAX_SIGNALS_PER_ROUND = 2
CLOSED_HISTORY_LIMIT = 300

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HTTP_TIMEOUT = 20
session = requests.Session()
session.headers.update({"User-Agent": "binance-data-pro-signal-bot-v4/4.0"})


# =========================================================
# LOG / STATE
# =========================================================

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    default = {
        "last_signal_times": {},
        "active_signals": {},
        "closed_signals": [],
        "last_outcomes": {},
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        for k, v in default.items():
            data.setdefault(k, v)
        if not isinstance(data.get("closed_signals"), list):
            data["closed_signals"] = []
        return data
    except Exception:
        return default


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram kapalı: TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID yok.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        r = session.post(url, data=payload, timeout=HTTP_TIMEOUT)
        if not r.ok:
            log(f"Telegram gönderim hatası: status={r.status_code} body={r.text[:300]}")
        return r.ok
    except Exception as e:
        log(f"Telegram exception: {e}")
        return False


# =========================================================
# BINANCE API
# =========================================================

def binance_get(path: str, params=None):
    url = f"{BASE_URL}{path}"
    r = session.get(url, params=params or {}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_exchange_symbols():
    try:
        data = binance_get("/api/v3/exchangeInfo")
        symbols = set()
        for row in data.get("symbols", []):
            if row.get("status") == "TRADING":
                s = row.get("symbol")
                if s:
                    symbols.add(s)
        return symbols
    except Exception as e:
        log(f"get_exchange_symbols error: {e}")
        return set(SYMBOL_RULES.keys())


def get_24h_tickers():
    try:
        data = binance_get("/api/v3/ticker/24hr")
        out = {}
        for row in data:
            s = row.get("symbol")
            if s:
                out[s] = row
        return out
    except Exception as e:
        log(f"get_24h_tickers error: {e}")
        return {}


def get_orderbook_ticker():
    try:
        data = binance_get("/api/v3/ticker/bookTicker")
        out = {}
        for row in data:
            s = row.get("symbol")
            if s:
                out[s] = row
        return out
    except Exception as e:
        log(f"get_orderbook_ticker error: {e}")
        return {}


def _kline_request(symbol: str, interval: str, limit: int = 200):
    return binance_get(
        "/api/v3/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": min(limit, 1000),
        }
    )


def klines_to_df(raw):
    if not raw:
        return pd.DataFrame()

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trade_count",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ]
    df = pd.DataFrame(raw, columns=cols)

    for c in ["open", "high", "low", "close", "volume", "quote_volume", "trade_count"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["open_time"] = pd.to_datetime(pd.to_numeric(df["open_time"], errors="coerce"), unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(pd.to_numeric(df["close_time"], errors="coerce"), unit="ms", utc=True)

    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    df.dropna(subset=["open", "high", "low", "close", "volume", "quote_volume", "open_time"], inplace=True)

    df = df[
        (df["open"] > 0) &
        (df["high"] > 0) &
        (df["low"] > 0) &
        (df["close"] > 0) &
        (df["volume"] >= 0) &
        (df["quote_volume"] >= 0)
    ].copy()

    if df.empty:
        return df

    df = df.sort_values("open_time").drop_duplicates(subset=["open_time"]).reset_index(drop=True)
    if len(df) > 1:
        df = df.iloc[:-1].copy()  # open candle'ı at
    return df.reset_index(drop=True)


def get_klines(symbol: str, interval: str, limit: int = 300):
    raw = _kline_request(symbol, interval, limit=limit)
    return klines_to_df(raw).reset_index(drop=True)


# =========================================================
# INDICATORS
# =========================================================

def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = np.where((plus_dm > minus_dm) & (plus_dm > 0), plus_dm, 0.0)
    minus_dm = np.where((minus_dm > plus_dm) & (minus_dm > 0), minus_dm, 0.0)

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr_s = tr.ewm(alpha=1 / length, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / length, adjust=False).mean() / atr_s.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / length, adjust=False).mean() / atr_s.replace(0, np.nan)

    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    return dx.ewm(alpha=1 / length, adjust=False).mean().fillna(0)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out.replace([np.inf, -np.inf], np.nan, inplace=True)
    out.dropna(subset=["open", "high", "low", "close", "volume", "quote_volume"], inplace=True)
    out = out.reset_index(drop=True)

    if out.empty:
        return out

    out["ema20"] = ema(out["close"], 20)
    out["ema50"] = ema(out["close"], 50)
    out["ema200"] = ema(out["close"], 200)
    out["rsi14"] = rsi(out["close"], 14)
    out["atr14"] = atr(out, 14)
    out["adx14"] = adx(out, 14)

    out["vol_ma20"] = out["volume"].rolling(20).mean()
    out["quote_vol_ma20"] = out["quote_volume"].rolling(20).mean()

    out["body"] = (out["close"] - out["open"]).abs()
    out["range"] = (out["high"] - out["low"]).replace(0, np.nan)
    out["body_ratio"] = (out["body"] / out["range"]).replace([np.inf, -np.inf], np.nan).fillna(0)

    out["upper_wick"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_wick"] = out[["open", "close"]].min(axis=1) - out["low"]
    out["wick_to_body"] = ((out["upper_wick"] + out["lower_wick"]) / out["body"].replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(99)

    out["atr_pct"] = out["atr14"] / out["close"].replace(0, np.nan)
    return out.reset_index(drop=True)


# =========================================================
# HELPERS
# =========================================================

def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 100:
        return f"{p:,.3f}"
    if p >= 1:
        return f"{p:,.4f}"
    return f"{p:,.6f}"


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def hours_since(ts_iso: str) -> float:
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        return 999999.0


def is_london_session(ts_utc: datetime) -> bool:
    x = ts_utc.astimezone(ZoneInfo("Europe/London"))
    return 8 <= x.hour < 17


def is_newyork_session(ts_utc: datetime) -> bool:
    x = ts_utc.astimezone(ZoneInfo("America/New_York"))
    return 8 <= x.hour < 17


def is_session_active(ts_utc=None) -> bool:
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc)
    return is_london_session(ts_utc) or is_newyork_session(ts_utc)


def active_session_name(ts_utc=None) -> str:
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc)
    if is_london_session(ts_utc):
        return "LONDON"
    if is_newyork_session(ts_utc):
        return "NEW_YORK"
    return "CLOSED"


def cooldown_key(symbol: str, side: str) -> str:
    return f"{symbol}:{side}"


def active_key(symbol: str, side: str) -> str:
    return f"{symbol}:{side}"


def last_outcome_key(symbol: str, side: str) -> str:
    return f"{symbol}:{side}"


def dynamic_cooldown_hours(state, symbol: str, side: str) -> float:
    outcome = state.get("last_outcomes", {}).get(last_outcome_key(symbol, side), "NONE")
    if outcome in ("LOSS", "STOP", "TIMEOUT"):
        return LOSS_COOLDOWN_HOURS
    if outcome in ("WIN", "TP2", "TP3"):
        return WIN_COOLDOWN_HOURS
    return BASE_COOLDOWN_HOURS


def is_in_cooldown(state, symbol: str, side: str) -> bool:
    last_ts = state.get("last_signal_times", {}).get(cooldown_key(symbol, side))
    if not last_ts:
        return False
    return hours_since(last_ts) < dynamic_cooldown_hours(state, symbol, side)


def safe_float(x, default=0.0):
    try:
        if x is None or x == "":
            return default
        v = pd.to_numeric(x, errors="coerce")
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def has_open_signal(state, symbol: str, side: str) -> bool:
    rec = state.get("active_signals", {}).get(active_key(symbol, side))
    return bool(rec and rec.get("status") == "OPEN")


def short_tier_for_symbol(symbol: str) -> str:
    return SYMBOL_RULES.get(symbol, {}).get("short_tier", "none")


def is_alt_symbol(symbol: str) -> bool:
    return symbol not in ("BTCUSDT", "ETHUSDT", "PAXGUSDT")


# =========================================================
# MARKET FILTERS
# =========================================================

def build_fallback_market_meta(symbol: str) -> dict:
    try:
        df = get_klines(symbol, "1h", 30)
        if len(df) < 25:
            return {"ok": False, "reason": "fallback_insufficient"}

        last_close = float(df.iloc[-1]["close"])
        prev_close = float(df.iloc[-24]["close"]) if len(df) >= 24 else float(df.iloc[0]["close"])
        vol24 = float(df.tail(24)["quote_volume"].sum()) if "quote_volume" in df.columns else 0.0

        pct_change = 0.0
        if prev_close > 0:
            pct_change = ((last_close - prev_close) / prev_close) * 100.0

        return {
            "ok": True,
            "quote_volume": vol24,
            "trade_count": 999999,
            "spread_pct": 0.06,
            "pct_change_24h": pct_change,
            "last_price": last_close,
            "reason": "fallback_kline",
        }
    except Exception as e:
        log(f"build_fallback_market_meta error {symbol}: {e}")
        return {"ok": False, "reason": "fallback_error"}


def symbol_market_ok(symbol: str, tickers_24h: dict, books: dict) -> dict:
    row = tickers_24h.get(symbol)
    book = books.get(symbol)

    if not row:
        return build_fallback_market_meta(symbol)

    quote_volume = safe_float(row.get("quoteVolume"))
    trade_count = safe_float(row.get("count"))
    pct_change = safe_float(row.get("priceChangePercent"))
    last_price = safe_float(row.get("lastPrice"))

    ask = safe_float(book.get("askPrice")) if book else 0.0
    bid = safe_float(book.get("bidPrice")) if book else 0.0

    spread_pct = 999.0
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        if mid > 0:
            spread_pct = ((ask - bid) / mid) * 100.0
    else:
        spread_pct = 0.08

    ok = (
        quote_volume >= MIN_QUOTE_VOLUME_USDT_24H and
        trade_count >= MIN_TRADES_24H and
        spread_pct <= MAX_SPREAD_PCT and
        pct_change >= MIN_24H_CHANGE_PCT and
        last_price > 0
    )

    return {
        "ok": bool(ok),
        "quote_volume": quote_volume,
        "trade_count": trade_count,
        "spread_pct": spread_pct,
        "pct_change_24h": pct_change,
        "last_price": last_price,
        "reason": "ok" if ok else "market_filter_fail",
    }


def market_regime_filter(symbol: str = "BTCUSDT") -> dict:
    try:
        df1h = enrich(get_klines(symbol, TF_TREND, LIVE_LIMIT_1H))
        df15 = enrich(get_klines(symbol, TF_ENTRY, LIVE_LIMIT_15M))
        if len(df1h) < 220 or len(df15) < 220:
            return {"ok_long": True, "ok_short": True, "reason": "insufficient_data"}

        h = df1h.iloc[-1]
        h_prev = df1h.iloc[-2]
        e = df15.iloc[-1]

        rsi_long_min = BTC_TREND_RSI_MIN_LONG if symbol == "BTCUSDT" else ETH_TREND_RSI_MIN_LONG
        rsi_short_max = BTC_TREND_RSI_MAX_SHORT if symbol == "BTCUSDT" else ETH_TREND_RSI_MAX_SHORT

        ok_long = (
            h["close"] > h["ema50"] > h["ema200"] and
            h["ema50"] > h_prev["ema50"] and
            h["rsi14"] >= rsi_long_min and
            e["close"] > e["ema20"]
        )

        ok_short = (
            h["close"] < h["ema50"] < h["ema200"] and
            h["ema50"] < h_prev["ema50"] and
            h["rsi14"] <= rsi_short_max and
            e["close"] < e["ema20"]
        )

        return {
            "ok_long": bool(ok_long),
            "ok_short": bool(ok_short),
            "close": float(h["close"]),
            "rsi": float(h["rsi14"]),
            "reason": "ok",
        }
    except Exception as e:
        log(f"market_regime_filter error {symbol}: {e}")
        return {"ok_long": True, "ok_short": True, "reason": "filter_error"}


# =========================================================
# STRATEGY
# =========================================================

def trend_long_ok(df1h: pd.DataFrame) -> dict:
    x = df1h.iloc[-1]
    prev = df1h.iloc[-2]

    ok = (
        x["close"] > x["ema20"] > x["ema50"] > x["ema200"] and
        x["ema50"] > prev["ema50"] and
        x["rsi14"] >= RSI_TREND_LONG_MIN and
        x["adx14"] >= ADX_MIN_LONG
    )
    return {
        "ok": bool(ok),
        "close": float(x["close"]),
        "ema20": float(x["ema20"]),
        "ema50": float(x["ema50"]),
        "ema200": float(x["ema200"]),
        "rsi": float(x["rsi14"]),
        "adx": float(x["adx14"]),
    }


def trend_short_ok(df1h: pd.DataFrame) -> dict:
    x = df1h.iloc[-1]
    prev = df1h.iloc[-2]

    ok = (
        x["close"] < x["ema20"] < x["ema50"] < x["ema200"] and
        x["ema50"] < prev["ema50"] and
        x["rsi14"] <= RSI_TREND_SHORT_MAX and
        x["adx14"] >= ADX_MIN_SHORT
    )
    return {
        "ok": bool(ok),
        "close": float(x["close"]),
        "ema20": float(x["ema20"]),
        "ema50": float(x["ema50"]),
        "ema200": float(x["ema200"]),
        "rsi": float(x["rsi14"]),
        "adx": float(x["adx14"]),
    }


def entry_long_signal(df15: pd.DataFrame, symbol_meta: dict) -> dict:
    x = df15.iloc[-1]
    p1 = df15.iloc[-2]
    p2 = df15.iloc[-3]

    near_ema20 = abs(x["close"] - x["ema20"]) <= (x["atr14"] * MAX_DISTANCE_FROM_EMA20_ATR_LONG)
    bullish_reclaim = x["close"] > x["open"] and x["close"] > x["ema20"] and x["body_ratio"] >= MIN_RECLAIM_BODY_RATIO_LONG
    had_pullback = (
        (p1["low"] <= p1["ema20"] * 1.003) or
        (p2["low"] <= p2["ema20"] * 1.003) or
        (p1["close"] < p1["ema20"]) or
        (p2["close"] < p2["ema20"])
    )
    rsi_ok = RSI_ENTRY_LONG_MIN <= x["rsi14"] <= RSI_ENTRY_LONG_MAX and x["rsi14"] >= p1["rsi14"] - 0.5
    ema_stack = x["ema20"] > x["ema50"] > x["ema200"]
    adx_ok = x["adx14"] >= ADX_MIN_LONG
    vol_ok = x["volume"] >= (x["vol_ma20"] * MIN_VOLUME_FACTOR_LONG if pd.notna(x["vol_ma20"]) else 0)
    candle_not_too_big = x["body"] <= (x["atr14"] * MAX_LAST_CANDLE_BODY_ATR_LONG)
    wick_ok = x["wick_to_body"] <= MAX_WICK_TO_BODY_RATIO_LONG
    atr_ok = MIN_ATR_PCT_15M_LONG <= x["atr_pct"] <= MAX_ATR_PCT_15M_LONG
    not_overextended = symbol_meta.get("pct_change_24h", 0.0) <= MAX_24H_PUMP_PCT_FOR_LONG

    ok = all([
        near_ema20, bullish_reclaim, had_pullback, rsi_ok, ema_stack,
        adx_ok, vol_ok, candle_not_too_big, wick_ok, atr_ok, not_overextended
    ])

    return {
        "ok": bool(ok),
        "close": float(x["close"]),
        "ema20": float(x["ema20"]),
        "rsi": float(x["rsi14"]),
        "adx": float(x["adx14"]),
        "atr": float(x["atr14"]),
        "atr_pct": float(x["atr_pct"]),
        "volume": float(x["volume"]),
        "vol_ma20": float(x["vol_ma20"]) if pd.notna(x["vol_ma20"]) else 0.0,
    }


def entry_short_signal(df15: pd.DataFrame, symbol_meta: dict, symbol: str) -> dict:
    x = df15.iloc[-1]
    p1 = df15.iloc[-2]
    p2 = df15.iloc[-3]

    near_ema20 = abs(x["close"] - x["ema20"]) <= (x["atr14"] * MAX_DISTANCE_FROM_EMA20_ATR_SHORT)
    bearish_reject = x["close"] < x["open"] and x["close"] < x["ema20"] and x["body_ratio"] >= MIN_RECLAIM_BODY_RATIO_SHORT
    had_pullback = (
        (p1["high"] >= p1["ema20"] * 0.997) or
        (p2["high"] >= p2["ema20"] * 0.997) or
        (p1["close"] > p1["ema20"]) or
        (p2["close"] > p2["ema20"])
    )
    rsi_ok = RSI_ENTRY_SHORT_MIN <= x["rsi14"] <= RSI_ENTRY_SHORT_MAX and x["rsi14"] <= p1["rsi14"] + 0.5
    ema_stack = x["ema20"] < x["ema50"] < x["ema200"]
    adx_ok = x["adx14"] >= ADX_MIN_SHORT
    vol_ok = x["volume"] >= (x["vol_ma20"] * MIN_VOLUME_FACTOR_SHORT if pd.notna(x["vol_ma20"]) else 0)
    candle_not_too_big = x["body"] <= (x["atr14"] * MAX_LAST_CANDLE_BODY_ATR_SHORT)
    wick_ok = x["wick_to_body"] <= MAX_WICK_TO_BODY_RATIO_SHORT
    atr_ok = MIN_ATR_PCT_15M_SHORT <= x["atr_pct"] <= MAX_ATR_PCT_15M_SHORT

    pct_24h = symbol_meta.get("pct_change_24h", 0.0)
    not_too_dumped = pct_24h >= MAX_24H_DUMP_PCT_FOR_SHORT
    not_shorting_hyper_pump = pct_24h <= MAX_24H_PUMP_PCT_FOR_SHORT

    tier = short_tier_for_symbol(symbol)
    if tier == "majors":
        spread_strict_ok = symbol_meta.get("spread_pct", 999.0) <= 0.22
    elif tier == "safe":
        spread_strict_ok = symbol_meta.get("spread_pct", 999.0) <= 0.18
    else:
        spread_strict_ok = symbol_meta.get("spread_pct", 999.0) <= 0.15

    ok = all([
        near_ema20, bearish_reject, had_pullback, rsi_ok, ema_stack,
        adx_ok, vol_ok, candle_not_too_big, wick_ok, atr_ok,
        not_too_dumped, not_shorting_hyper_pump, spread_strict_ok
    ])

    return {
        "ok": bool(ok),
        "close": float(x["close"]),
        "ema20": float(x["ema20"]),
        "rsi": float(x["rsi14"]),
        "adx": float(x["adx14"]),
        "atr": float(x["atr14"]),
        "atr_pct": float(x["atr_pct"]),
        "volume": float(x["volume"]),
        "vol_ma20": float(x["vol_ma20"]) if pd.notna(x["vol_ma20"]) else 0.0,
    }


def build_signal(symbol: str, side: str, df15: pd.DataFrame) -> dict:
    x = df15.iloc[-1]
    entry = float(x["close"])
    atr_val = float(x["atr14"])

    if side == "LONG":
        stop = entry - atr_val * SL_ATR_MULT_LONG
        risk = entry - stop
        if risk <= 0:
            return {"ok": False}
        tp1 = entry + risk * TP1_R_MULT_LONG
        tp2 = entry + risk * TP2_R_MULT_LONG
        tp3 = entry + risk * TP3_R_MULT_LONG
        risk_pct = (risk / entry) * 100.0
        if not (MIN_RISK_PCT_LONG <= risk_pct <= MAX_RISK_PCT_LONG):
            return {"ok": False}
        rr_tp2 = TP2_R_MULT_LONG
        rr_tp3 = TP3_R_MULT_LONG
    else:
        stop = entry + atr_val * SL_ATR_MULT_SHORT
        risk = stop - entry
        if risk <= 0:
            return {"ok": False}
        tp1 = entry - risk * TP1_R_MULT_SHORT
        tp2 = entry - risk * TP2_R_MULT_SHORT
        tp3 = entry - risk * TP3_R_MULT_SHORT
        risk_pct = (risk / entry) * 100.0
        if not (MIN_RISK_PCT_SHORT <= risk_pct <= MAX_RISK_PCT_SHORT):
            return {"ok": False}
        rr_tp2 = TP2_R_MULT_SHORT
        rr_tp3 = TP3_R_MULT_SHORT

    return {
        "ok": True,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "initial_stop": stop,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk_pct": risk_pct,
        "rr_tp2": rr_tp2,
        "rr_tp3": rr_tp3,
    }


def score_long_signal(trend_info, entry_info, signal, symbol_meta, btc_filter_info, eth_filter_info):
    score = 0.0

    score += min(max((trend_info["rsi"] - 50) * 0.8, 0), 10)
    score += min(max((trend_info["adx"] - ADX_MIN_LONG) * 0.45, 0), 8)
    score += min(max((entry_info["adx"] - ADX_MIN_LONG) * 0.30, 0), 5)

    trend_gap = abs(((trend_info["close"] - trend_info["ema20"]) / trend_info["close"]) * 100)
    score += max(0, 4 - abs(trend_gap - 0.9) * 2.5)

    vol_ratio = (entry_info["volume"] / entry_info["vol_ma20"]) if entry_info["vol_ma20"] > 0 else 1.0
    score += min(max((vol_ratio - 1.0) * 8, 0), 8)

    if 0.35 <= signal["risk_pct"] <= 1.40:
        score += 6
    elif 0.28 <= signal["risk_pct"] <= 1.90:
        score += 3

    if symbol_meta["quote_volume"] >= 40_000_000:
        score += 4
    elif symbol_meta["quote_volume"] >= 15_000_000:
        score += 2

    if symbol_meta["spread_pct"] <= 0.10:
        score += 3
    elif symbol_meta["spread_pct"] <= 0.20:
        score += 1

    if btc_filter_info.get("ok_long", True):
        score += 3
    else:
        score -= 3

    if not is_alt_symbol(signal["symbol"]):
        score += 2
    elif eth_filter_info.get("ok_long", True):
        score += 1

    if trend_info["close"] > trend_info["ema50"] > trend_info["ema200"]:
        score += 2

    if entry_info["atr_pct"] < 0.0028:
        score -= 2

    return round(score, 2)


def score_short_signal(trend_info, entry_info, signal, symbol_meta, btc_filter_info, eth_filter_info, symbol):
    score = 0.0

    score += min(max((50 - trend_info["rsi"]) * 0.8, 0), 10)
    score += min(max((trend_info["adx"] - ADX_MIN_SHORT) * 0.45, 0), 8)
    score += min(max((entry_info["adx"] - ADX_MIN_SHORT) * 0.30, 0), 5)

    trend_gap = abs(((trend_info["close"] - trend_info["ema20"]) / trend_info["close"]) * 100)
    score += max(0, 4 - abs(trend_gap - 0.8) * 2.5)

    vol_ratio = (entry_info["volume"] / entry_info["vol_ma20"]) if entry_info["vol_ma20"] > 0 else 1.0
    score += min(max((vol_ratio - 1.0) * 8, 0), 8)

    if 0.30 <= signal["risk_pct"] <= 1.30:
        score += 6
    elif 0.28 <= signal["risk_pct"] <= 1.70:
        score += 3

    if symbol_meta["quote_volume"] >= 40_000_000:
        score += 4
    elif symbol_meta["quote_volume"] >= 15_000_000:
        score += 2

    if symbol_meta["spread_pct"] <= 0.08:
        score += 3
    elif symbol_meta["spread_pct"] <= 0.15:
        score += 1

    if btc_filter_info.get("ok_short", True):
        score += 3
    else:
        score -= 3

    if symbol in ("BTCUSDT", "ETHUSDT", "PAXGUSDT"):
        score += 1
    elif eth_filter_info.get("ok_short", True):
        score += 2
    else:
        score -= 2

    if trend_info["close"] < trend_info["ema50"] < trend_info["ema200"]:
        score += 2

    if entry_info["atr_pct"] < 0.0030:
        score -= 2

    return round(score, 2)


def scan_symbol_side(symbol: str, side: str, tickers_24h: dict, books: dict, btc_filter_info: dict, eth_filter_info: dict):
    symbol_meta = symbol_market_ok(symbol, tickers_24h, books)
    if not symbol_meta["ok"]:
        return None

    if REQUIRE_BTC_CONFIRMATION:
        if side == "LONG" and not btc_filter_info.get("ok_long", True):
            return None
        if side == "SHORT" and not btc_filter_info.get("ok_short", True):
            return None

    if side == "SHORT":
        if symbol not in ("BTCUSDT", "ETHUSDT", "PAXGUSDT", "ARBUSDT", "FETUSDT", "RNDRUSDT", "APTUSDT"):
            return None
        if is_alt_symbol(symbol) and not eth_filter_info.get("ok_short", True):
            return None

    df1h = enrich(get_klines(symbol, TF_TREND, LIVE_LIMIT_1H))
    df15 = enrich(get_klines(symbol, TF_ENTRY, LIVE_LIMIT_15M))

    if len(df1h) < 220 or len(df15) < 220:
        return None

    if side == "LONG":
        t = trend_long_ok(df1h)
        if not t["ok"]:
            return None
        e = entry_long_signal(df15, symbol_meta)
        if not e["ok"]:
            return None
    else:
        t = trend_short_ok(df1h)
        if not t["ok"]:
            return None
        e = entry_short_signal(df15, symbol_meta, symbol)
        if not e["ok"]:
            return None

    s = build_signal(symbol, side, df15)
    if not s["ok"]:
        return None

    if side == "LONG":
        score = score_long_signal(t, e, s, symbol_meta, btc_filter_info, eth_filter_info)
        if score < MIN_SCORE_TO_SIGNAL_LONG:
            return None
    else:
        score = score_short_signal(t, e, s, symbol_meta, btc_filter_info, eth_filter_info, symbol)
        if score < MIN_SCORE_TO_SIGNAL_SHORT:
            return None

    s["score"] = score
    s["trend_info"] = t
    s["entry_info"] = e
    s["market_info"] = symbol_meta
    return s


# =========================================================
# ACTIVE SIGNAL MANAGEMENT
# =========================================================

def make_active_record(sig: dict) -> dict:
    return {
        "symbol": sig["symbol"],
        "side": sig["side"],
        "time": now_utc_iso(),
        "status": "OPEN",
        "entry": sig["entry"],
        "initial_stop": sig["initial_stop"],
        "stop": sig["stop"],
        "tp1": sig["tp1"],
        "tp2": sig["tp2"],
        "tp3": sig["tp3"],
        "score": sig["score"],
        "breakeven_moved": False,
        "tp2_locked": False,
        "session": active_session_name(),
    }


def venue_text(symbol: str, side: str) -> str:
    if side == "LONG":
        return f"Binance Data LONG | {symbol}"
    return f"Binance Data SHORT/Alarm | {symbol}"


def signal_to_telegram(sig: dict) -> str:
    symbol = sig["symbol"]
    side = sig["side"]
    market = sig["market_info"]
    trend = sig["trend_info"]
    entry_info = sig["entry_info"]

    emoji = "🟢" if side == "LONG" else "🔴"

    lines = [
        f"{emoji} <b>BINANCE DATA PRO SIGNAL V4</b>",
        f"<b>Parite:</b> {symbol}",
        f"<b>Yön:</b> {side}",
        f"<b>Setup:</b> {venue_text(symbol, side)}",
        "",
        f"<b>Entry:</b> {fmt_price(sig['entry'])}",
        f"<b>SL:</b> {fmt_price(sig['initial_stop'])}",
        f"<b>TP1:</b> {fmt_price(sig['tp1'])}",
        f"<b>TP2:</b> {fmt_price(sig['tp2'])}",
        f"<b>TP3:</b> {fmt_price(sig['tp3'])}",
        "",
        f"<b>Risk %:</b> {sig['risk_pct']:.2f}",
        f"<b>Skor:</b> {sig['score']:.2f}",
        f"<b>24h Değişim:</b> %{market['pct_change_24h']:.2f}",
        f"<b>Spread:</b> %{market['spread_pct']:.3f}",
        f"<b>Quote Vol 24h:</b> {market['quote_volume']:,.0f} USDT",
        "",
        f"<b>Trend RSI:</b> {trend['rsi']:.2f}",
        f"<b>Trend ADX:</b> {trend['adx']:.2f}",
        f"<b>Entry RSI:</b> {entry_info['rsi']:.2f}",
        f"<b>ATR %:</b> %{entry_info['atr_pct'] * 100:.2f}",
        f"<b>Session:</b> {active_session_name()}",
    ]
    return "\n".join(lines)


def close_active_signal(state, key: str, reason: str, exit_price: float):
    rec = state["active_signals"].get(key)
    if not rec:
        return

    side = rec["side"]
    entry = float(rec["entry"])

    if side == "LONG":
        pnl_pct = ((exit_price - entry) / entry) * 100.0
    else:
        pnl_pct = ((entry - exit_price) / entry) * 100.0

    rec["status"] = "CLOSED"
    rec["close_reason"] = reason
    rec["close_time"] = now_utc_iso()
    rec["exit_price"] = exit_price
    rec["pnl_pct"] = round(pnl_pct, 3)

    state["closed_signals"].append(rec)
    if len(state["closed_signals"]) > CLOSED_HISTORY_LIMIT:
        state["closed_signals"] = state["closed_signals"][-CLOSED_HISTORY_LIMIT:]

    state["last_outcomes"][last_outcome_key(rec["symbol"], rec["side"])] = reason
    state["active_signals"].pop(key, None)

    icon = "✅" if reason in ("TP2", "TP3", "WIN") else "⚠️"
    send_telegram(
        f"{icon} <b>SİNYAL KAPANDI</b>\n"
        f"<b>Parite:</b> {rec['symbol']}\n"
        f"<b>Yön:</b> {rec['side']}\n"
        f"<b>Neden:</b> {reason}\n"
        f"<b>Exit:</b> {fmt_price(exit_price)}\n"
        f"<b>PnL:</b> %{pnl_pct:.2f}"
    )


def update_active_signals(state, valid_symbols):
    if not state.get("active_signals"):
        return

    for key, rec in list(state["active_signals"].items()):
        try:
            symbol = rec["symbol"]
            side = rec["side"]

            if symbol not in valid_symbols:
                continue

            age_h = hours_since(rec["time"])
            df15 = enrich(get_klines(symbol, TF_ENTRY, 80))
            if len(df15) < 5:
                continue

            x = df15.iloc[-1]
            high = float(x["high"])
            low = float(x["low"])
            close = float(x["close"])

            entry = float(rec["entry"])
            tp1 = float(rec["tp1"])
            tp2 = float(rec["tp2"])
            tp3 = float(rec["tp3"])

            if side == "LONG":
                if (not rec["breakeven_moved"]) and high >= tp1:
                    rec["stop"] = entry
                    rec["breakeven_moved"] = True
                    send_telegram(
                        f"🔒 <b>TP1 GÖRÜLDÜ - BE</b>\n"
                        f"<b>Parite:</b> {symbol}\n"
                        f"<b>Yön:</b> {side}\n"
                        f"<b>Yeni Stop:</b> {fmt_price(entry)}"
                    )

                if (not rec["tp2_locked"]) and high >= tp2:
                    new_stop = entry + ((tp2 - entry) * 0.35)
                    if new_stop > rec["stop"]:
                        rec["stop"] = new_stop
                    rec["tp2_locked"] = True
                    send_telegram(
                        f"💰 <b>TP2 GÖRÜLDÜ - KÂR KİLİTLENDİ</b>\n"
                        f"<b>Parite:</b> {symbol}\n"
                        f"<b>Yön:</b> {side}\n"
                        f"<b>Yeni Stop:</b> {fmt_price(rec['stop'])}"
                    )

                if low <= rec["stop"]:
                    reason = "STOP" if rec["stop"] <= entry else "WIN"
                    close_active_signal(state, key, reason, float(rec["stop"]))
                    continue

                if high >= tp3:
                    close_active_signal(state, key, "TP3", tp3)
                    continue

            else:
                if (not rec["breakeven_moved"]) and low <= tp1:
                    rec["stop"] = entry
                    rec["breakeven_moved"] = True
                    send_telegram(
                        f"🔒 <b>TP1 GÖRÜLDÜ - BE</b>\n"
                        f"<b>Parite:</b> {symbol}\n"
                        f"<b>Yön:</b> {side}\n"
                        f"<b>Yeni Stop:</b> {fmt_price(entry)}"
                    )

                if (not rec["tp2_locked"]) and low <= tp2:
                    new_stop = entry - ((entry - tp2) * 0.35)
                    if new_stop < rec["stop"]:
                        rec["stop"] = new_stop
                    rec["tp2_locked"] = True
                    send_telegram(
                        f"💰 <b>TP2 GÖRÜLDÜ - KÂR KİLİTLENDİ</b>\n"
                        f"<b>Parite:</b> {symbol}\n"
                        f"<b>Yön:</b> {side}\n"
                        f"<b>Yeni Stop:</b> {fmt_price(rec['stop'])}"
                    )

                if high >= rec["stop"]:
                    reason = "STOP" if rec["stop"] >= entry else "WIN"
                    close_active_signal(state, key, reason, float(rec["stop"]))
                    continue

                if low <= tp3:
                    close_active_signal(state, key, "TP3", tp3)
                    continue

            if age_h >= ACTIVE_TRADE_MAX_AGE_HOURS:
                close_active_signal(state, key, "TIMEOUT", close)

        except Exception as e:
            log(f"update_active_signals error {key}: {e}")


# =========================================================
# MAIN
# =========================================================

def choose_best_signals(candidates, top_n=MAX_SIGNALS_PER_ROUND):
    if not candidates:
        return []
    candidates = sorted(candidates, key=lambda x: (x["score"], -x["risk_pct"]), reverse=True)

    final = []
    used_symbols = set()
    for c in candidates:
        if c["symbol"] in used_symbols:
            continue
        final.append(c)
        used_symbols.add(c["symbol"])
        if len(final) >= top_n:
            break
    return final


def main():
    log("BINANCE DATA PRO SIGNAL BOT V4 starting.")
    state = load_state()

    try:
        exchange_symbols = get_exchange_symbols()
        valid_symbols = {s for s in SYMBOL_RULES.keys() if s in exchange_symbols}
        skipped = sorted(set(SYMBOL_RULES.keys()) - valid_symbols)

        if skipped:
            log(f"Skipped unsupported symbols: {', '.join(skipped)}")

        if not valid_symbols:
            log("No valid symbols.")
            return

        send_telegram(
            f"✅ <b>BINANCE DATA PRO BOT V4 BAŞLADI</b>\n\n"
            f"<b>Aktif Pariteler:</b> {', '.join(sorted(valid_symbols))}\n"
            f"<b>Session Filter:</b> {'ON' if REQUIRE_SESSION_FILTER else 'OFF'}\n"
            f"<b>BTC Filter:</b> {'ON' if REQUIRE_BTC_CONFIRMATION else 'OFF'}\n"
            f"<b>Max Sinyal/Tur:</b> {MAX_SIGNALS_PER_ROUND}"
        )

        while True:
            try:
                now_utc = datetime.now(timezone.utc)

                update_active_signals(state, valid_symbols)
                save_state(state)

                if REQUIRE_SESSION_FILTER and not is_session_active(now_utc):
                    log("Session inactive, skipping scan.")
                    time.sleep(SCAN_INTERVAL_SECONDS)
                    continue

                tickers_24h = get_24h_tickers()
                books = get_orderbook_ticker()
                btc_filter_info = market_regime_filter(BTC_CONFIRMATION_SYMBOL)
                eth_filter_info = market_regime_filter(ETH_CONFIRMATION_SYMBOL)

                candidates = []

                for symbol, rules in SYMBOL_RULES.items():
                    if symbol not in valid_symbols:
                        continue

                    if rules.get("allow_long", False):
                        if (not is_in_cooldown(state, symbol, "LONG")) and (not has_open_signal(state, symbol, "LONG")):
                            try:
                                sig = scan_symbol_side(symbol, "LONG", tickers_24h, books, btc_filter_info, eth_filter_info)
                                if sig:
                                    candidates.append(sig)
                            except Exception as e:
                                log(f"scan error {symbol} LONG: {e}")

                    if rules.get("allow_short", False):
                        if (not is_in_cooldown(state, symbol, "SHORT")) and (not has_open_signal(state, symbol, "SHORT")):
                            try:
                                sig = scan_symbol_side(symbol, "SHORT", tickers_24h, books, btc_filter_info, eth_filter_info)
                                if sig:
                                    candidates.append(sig)
                            except Exception as e:
                                log(f"scan error {symbol} SHORT: {e}")

                best = choose_best_signals(candidates)

                if not best:
                    log("No signal this round.")
                else:
                    for sig in best:
                        key = active_key(sig["symbol"], sig["side"])
                        state["active_signals"][key] = make_active_record(sig)
                        state["last_signal_times"][cooldown_key(sig["symbol"], sig["side"])] = now_utc_iso()
                        send_telegram(signal_to_telegram(sig))
                        log(f"SENT SIGNAL {sig['symbol']} {sig['side']} score={sig['score']}")

                    save_state(state)

                time.sleep(SCAN_INTERVAL_SECONDS)

            except KeyboardInterrupt:
                log("Interrupted by user.")
                break
            except Exception as e:
                log(f"loop error: {e}")
                log(traceback.format_exc())
                time.sleep(15)

    except Exception as e:
        log(f"fatal error: {e}")
        log(traceback.format_exc())


if __name__ == "__main__":
    main()
