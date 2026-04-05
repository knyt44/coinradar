# -*- coding: utf-8 -*-
"""
PRO HYBRID SIGNAL BOT V3
- Binance spot market data
- Long / Short optimized separately
- BTC / ETH / PAXG + selected altcoins
- BTC / ETH: LONG + SHORT
- Selected altcoins: LONG + SHORT only if whitelisted
- 1H trend + 15M entry
- BTC + ETH regime confirmation for riskier shorts
- Session filter
- Unified live + backtest exit engine
- TP1 / TP2 / TP3
- TP1 -> BE
- TP2 -> lock profit
- Telegram optional

KURULUM:
pip install requests pandas numpy

ENV:
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID

ÇALIŞTIR:
python pro_hybrid_signal_bot_v3.py
"""

import os
import time
import json
import traceback
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
import numpy as np
import pandas as pd

# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://api.binance.com"

# Daha kontrollü short whitelist
SYMBOL_RULES = {
    "BTCUSDT":   {"allow_long": True, "allow_short": True,  "short_tier": "majors"},
    "ETHUSDT":   {"allow_long": True, "allow_short": True,  "short_tier": "majors"},
    "PAXGUSDT":  {"allow_long": True, "allow_short": True,  "short_tier": "safe"},
    "SEIUSDT":   {"allow_long": True, "allow_short": False, "short_tier": "none"},
    "DOGEUSDT":  {"allow_long": True, "allow_short": False, "short_tier": "none"},
    "ARBUSDT":   {"allow_long": True, "allow_short": True,  "short_tier": "alts"},
    "TRUMPUSDT": {"allow_long": True, "allow_short": False, "short_tier": "none"},
    "TAOUSDT":   {"allow_long": True, "allow_short": False, "short_tier": "none"},
    "FETUSDT":   {"allow_long": True, "allow_short": True,  "short_tier": "alts"},
    "RNDRUSDT":  {"allow_long": True, "allow_short": True,  "short_tier": "alts"},
    "APTUSDT":   {"allow_long": True, "allow_short": True,  "short_tier": "alts"},
}

STATE_FILE = "pro_hybrid_signal_bot_v3_state.json"
LOG_FILE = "pro_hybrid_signal_bot_v3.log"

SCAN_INTERVAL_SECONDS = 60

TF_TREND = "1h"
TF_ENTRY = "15m"
LIVE_LIMIT_1H = 500
LIVE_LIMIT_15M = 500

REQUIRE_SESSION_FILTER = True

# Global market confirmation
REQUIRE_BTC_CONFIRMATION = True
BTC_CONFIRMATION_SYMBOL = "BTCUSDT"
ETH_CONFIRMATION_SYMBOL = "ETHUSDT"

BTC_TREND_RSI_MIN_LONG = 54
BTC_TREND_RSI_MAX_SHORT = 46

ETH_TREND_RSI_MIN_LONG = 53
ETH_TREND_RSI_MAX_SHORT = 47

# Market quality filters
MIN_QUOTE_VOLUME_USDT_24H = 15_000_000
MIN_TRADES_24H = 20_000
MAX_SPREAD_PCT = 0.35
MAX_24H_PUMP_PCT_FOR_LONG = 10.5
MIN_24H_CHANGE_PCT = -9.0

# Short specific market filters
MAX_24H_DUMP_PCT_FOR_SHORT = -11.0
MAX_24H_PUMP_PCT_FOR_SHORT = 7.5

MIN_ATR_PCT_15M_LONG = 0.0030
MAX_ATR_PCT_15M_LONG = 0.0320

MIN_ATR_PCT_15M_SHORT = 0.0035
MAX_ATR_PCT_15M_SHORT = 0.0280

# Trend filters
RSI_TREND_LONG_MIN = 55
RSI_TREND_SHORT_MAX = 45

# Entry filters
RSI_ENTRY_LONG_MIN = 50
RSI_ENTRY_LONG_MAX = 62

RSI_ENTRY_SHORT_MIN = 35
RSI_ENTRY_SHORT_MAX = 49

ADX_MIN_LONG = 22
ADX_MIN_SHORT = 24

MIN_VOLUME_FACTOR_LONG = 1.20
MIN_VOLUME_FACTOR_SHORT = 1.35

MAX_DISTANCE_FROM_EMA20_ATR_LONG = 0.90
MAX_DISTANCE_FROM_EMA20_ATR_SHORT = 0.75

MAX_LAST_CANDLE_BODY_ATR_LONG = 0.95
MAX_LAST_CANDLE_BODY_ATR_SHORT = 0.85

MIN_RECLAIM_BODY_RATIO_LONG = 0.52
MIN_RECLAIM_BODY_RATIO_SHORT = 0.58

MAX_WICK_TO_BODY_RATIO_LONG = 2.8
MAX_WICK_TO_BODY_RATIO_SHORT = 2.2

# Risk
SL_ATR_MULT_LONG = 1.20
SL_ATR_MULT_SHORT = 1.10

TP1_R_MULT_LONG = 1.20
TP2_R_MULT_LONG = 2.40
TP3_R_MULT_LONG = 3.60

TP1_R_MULT_SHORT = 1.00
TP2_R_MULT_SHORT = 2.20
TP3_R_MULT_SHORT = 3.20

MIN_RISK_PCT_LONG = 0.35
MAX_RISK_PCT_LONG = 2.20

MIN_RISK_PCT_SHORT = 0.30
MAX_RISK_PCT_SHORT = 1.80

MIN_SCORE_TO_SIGNAL_LONG = 24.0
MIN_SCORE_TO_SIGNAL_SHORT = 27.0

BASE_COOLDOWN_HOURS = 8
LOSS_COOLDOWN_HOURS = 12
WIN_COOLDOWN_HOURS = 5
ACTIVE_TRADE_MAX_AGE_HOURS = 36

RUN_BACKTEST_ON_START = True
BACKTEST_DAYS = 120
BACKTEST_MAX_HOLD_BARS = 64
BACKTEST_BAR_SPACING = 18
BACKTEST_EQUITY_START = 10000.0
COMMISSION_PER_SIDE_PCT = 0.10
SLIPPAGE_PER_SIDE_PCT = 0.03

MAX_SIGNALS_PER_ROUND = 2
CLOSED_HISTORY_LIMIT = 300

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HTTP_TIMEOUT = 20
session = requests.Session()
session.headers.update({"User-Agent": "pro-hybrid-signal-bot-v3/4.0"})


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
        "last_backtest_report": {},
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
# API
# =========================================================

def get_exchange_info():
    url = f"{BASE_URL}/api/v3/exchangeInfo"
    r = session.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_exchange_symbols():
    data = get_exchange_info()
    symbols = set()
    for s in data.get("symbols", []):
        if s.get("status") == "TRADING":
            symbols.add(s.get("symbol"))
    return symbols


def get_24h_tickers():
    url = f"{BASE_URL}/api/v3/ticker/24hr"
    r = session.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    out = {}
    for row in data:
        symbol = row.get("symbol")
        if symbol:
            out[symbol] = row
    return out


def get_orderbook_ticker():
    url = f"{BASE_URL}/api/v3/ticker/bookTicker"
    r = session.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    out = {}
    for row in data:
        symbol = row.get("symbol")
        if symbol:
            out[symbol] = row
    return out


def _klines_request(symbol: str, interval: str, limit: int = 1000, end_time_ms=None):
    url = f"{BASE_URL}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    if end_time_ms is not None:
        params["endTime"] = int(end_time_ms)
    r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def klines_to_df(raw):
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trade_count",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "quote_volume", "trade_count", "taker_buy_quote"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def get_klines(symbol: str, interval: str, limit: int = 500, closed_only: bool = True):
    raw = _klines_request(symbol, interval, limit=limit)
    df = klines_to_df(raw)
    if closed_only and len(df) > 1:
        df = df.iloc[:-1].copy()
    return df.reset_index(drop=True)


def get_historical_klines(symbol: str, interval: str, days: int, closed_only: bool = True):
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_cutoff = datetime.now(timezone.utc) - timedelta(days=days + 5)

    parts = []
    for _ in range(30):
        raw = _klines_request(symbol, interval, limit=1000, end_time_ms=end_time)
        if not raw:
            break
        df = klines_to_df(raw)
        parts.append(df)

        oldest_open = df.iloc[0]["open_time"]
        if oldest_open <= start_cutoff:
            break

        end_time = int(df.iloc[0]["open_time"].timestamp() * 1000) - 1
        time.sleep(0.10)

    if not parts:
        return pd.DataFrame()

    out = pd.concat(parts, ignore_index=True)
    out = out.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    out = out[out["open_time"] >= start_cutoff].copy()

    if closed_only and len(out) > 1:
        out = out.iloc[:-1].copy()

    return out.reset_index(drop=True)


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

    return out


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


def roundtrip_cost_pct():
    return (COMMISSION_PER_SIDE_PCT + SLIPPAGE_PER_SIDE_PCT) * 2.0


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
        return float(x)
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

def symbol_market_ok(symbol: str, tickers_24h: dict, books: dict) -> dict:
    row = tickers_24h.get(symbol)
    book = books.get(symbol)

    if not row or not book:
        return {"ok": False, "reason": "ticker_missing"}

    quote_volume = safe_float(row.get("quoteVolume"))
    trade_count = safe_float(row.get("count"))
    pct_change = safe_float(row.get("priceChangePercent"))
    ask = safe_float(book.get("askPrice"))
    bid = safe_float(book.get("bidPrice"))
    last_price = safe_float(row.get("lastPrice"))

    spread_pct = 999.0
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        if mid > 0:
            spread_pct = ((ask - bid) / mid) * 100.0

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
        x["ema200"] >= prev["ema200"] and
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
        x["ema200"] <= prev["ema200"] and
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
        (p1["low"] <= p1["ema20"] * 1.002) or
        (p2["low"] <= p2["ema20"] * 1.002) or
        (p1["close"] < p1["ema20"]) or
        (p2["close"] < p2["ema20"])
    )
    rsi_ok = RSI_ENTRY_LONG_MIN <= x["rsi14"] <= RSI_ENTRY_LONG_MAX and x["rsi14"] > p1["rsi14"]
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
        (p1["high"] >= p1["ema20"] * 0.998) or
        (p2["high"] >= p2["ema20"] * 0.998) or
        (p1["close"] > p1["ema20"]) or
        (p2["close"] > p2["ema20"])
    )

    rsi_ok = RSI_ENTRY_SHORT_MIN <= x["rsi14"] <= RSI_ENTRY_SHORT_MAX and x["rsi14"] < p1["rsi14"]
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
        spread_strict_ok = symbol_meta.get("spread_pct", 999.0) <= 0.18
    elif tier == "safe":
        spread_strict_ok = symbol_meta.get("spread_pct", 999.0) <= 0.15
    else:
        spread_strict_ok = symbol_meta.get("spread_pct", 999.0) <= 0.12

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


def score_long_signal(trend_info: dict, entry_info: dict, signal: dict, symbol_meta: dict, btc_filter_info: dict, eth_filter_info: dict) -> float:
    score = 0.0

    score += min(max((trend_info["rsi"] - 50) * 0.9, 0), 12)
    score += min(max((trend_info["adx"] - ADX_MIN_LONG) * 0.45, 0), 8)
    score += min(max((entry_info["adx"] - ADX_MIN_LONG) * 0.35, 0), 6)

    trend_gap = abs(((trend_info["close"] - trend_info["ema20"]) / trend_info["close"]) * 100)
    score += max(0, 5 - abs(trend_gap - 0.8) * 3)

    score += min(max((entry_info["rsi"] - 50) * 0.30, 0), 4)

    vol_ratio = (entry_info["volume"] / entry_info["vol_ma20"]) if entry_info["vol_ma20"] > 0 else 1.0
    score += min(max((vol_ratio - 1.0) * 10, 0), 10)

    if 0.45 <= signal["risk_pct"] <= 1.80:
        score += 7
    elif 0.35 <= signal["risk_pct"] <= 2.20:
        score += 4

    if symbol_meta["quote_volume"] >= 50_000_000:
        score += 4
    elif symbol_meta["quote_volume"] >= 25_000_000:
        score += 2

    if symbol_meta["spread_pct"] <= 0.08:
        score += 4
    elif symbol_meta["spread_pct"] <= 0.15:
        score += 2

    if btc_filter_info.get("ok_long", True):
        score += 4
    if eth_filter_info.get("ok_long", True):
        score += 2

    score += 4
    return round(score, 2)


def score_short_signal(trend_info: dict, entry_info: dict, signal: dict, symbol_meta: dict, btc_filter_info: dict, eth_filter_info: dict, symbol: str) -> float:
    score = 0.0

    score += min(max((50 - trend_info["rsi"]) * 1.0, 0), 13)
    score += min(max((trend_info["adx"] - ADX_MIN_SHORT) * 0.55, 0), 9)
    score += min(max((entry_info["adx"] - ADX_MIN_SHORT) * 0.40, 0), 7)

    trend_gap = abs(((trend_info["ema20"] - trend_info["close"]) / trend_info["close"]) * 100)
    score += max(0, 6 - abs(trend_gap - 0.9) * 3)

    score += min(max((50 - entry_info["rsi"]) * 0.35, 0), 5)

    vol_ratio = (entry_info["volume"] / entry_info["vol_ma20"]) if entry_info["vol_ma20"] > 0 else 1.0
    score += min(max((vol_ratio - 1.0) * 12, 0), 12)

    if 0.35 <= signal["risk_pct"] <= 1.40:
        score += 8
    elif 0.30 <= signal["risk_pct"] <= 1.80:
        score += 5

    if symbol_meta["quote_volume"] >= 60_000_000:
        score += 4
    elif symbol_meta["quote_volume"] >= 30_000_000:
        score += 2

    if symbol_meta["spread_pct"] <= 0.06:
        score += 5
    elif symbol_meta["spread_pct"] <= 0.10:
        score += 3

    if btc_filter_info.get("ok_short", True):
        score += 5

    if not is_alt_symbol(symbol):
        if eth_filter_info.get("ok_short", True):
            score += 2
    else:
        if eth_filter_info.get("ok_short", True):
            score += 4
        else:
            score -= 3

    pct_24h = symbol_meta.get("pct_change_24h", 0.0)
    if -6.5 <= pct_24h <= 2.5:
        score += 3
    elif pct_24h < -9.5:
        score -= 4

    score += 3
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

    # Altcoin shortlar için ETH teyidi daha da önemli
    if side == "SHORT" and is_alt_symbol(symbol):
        if not eth_filter_info.get("ok_short", True):
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
# ACTIVE SIGNAL MANAGEMENT / UNIFIED EXIT ENGINE
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
    if side == "SHORT":
        return "SHORT için FUTURES/MARGIN gerekir"
    return "LONG signal"


def entry_message(sig: dict) -> str:
    return (
        f"🔥 <b>PRO V3 SİNYAL</b>\n\n"
        f"<b>Parite:</b> {sig['symbol']}\n"
        f"<b>Yön:</b> {sig['side']}\n"
        f"<b>Session:</b> {active_session_name()}\n"
        f"<b>Entry:</b> {fmt_price(sig['entry'])}\n"
        f"<b>Stop:</b> {fmt_price(sig['stop'])}\n"
        f"<b>TP1:</b> {fmt_price(sig['tp1'])}\n"
        f"<b>TP2:</b> {fmt_price(sig['tp2'])}\n"
        f"<b>TP3:</b> {fmt_price(sig['tp3'])}\n\n"
        f"<b>Risk:</b> %{sig['risk_pct']:.2f}\n"
        f"<b>RR TP2:</b> {sig['rr_tp2']:.2f}\n"
        f"<b>RR TP3:</b> {sig['rr_tp3']:.2f}\n"
        f"<b>Skor:</b> {sig['score']:.2f}\n"
        f"<b>Not:</b> {venue_text(sig['symbol'], sig['side'])}\n\n"
        f"<i>TP1 sonrası BE. TP2 sonrası kâr kilitleme.</i>"
    )


def evaluate_position_on_bar(rec: dict, high: float, low: float):
    out = dict(rec)
    events = []

    side = out["side"]
    entry = float(out["entry"])
    stop = float(out["stop"])
    tp1 = float(out["tp1"])
    tp2 = float(out["tp2"])
    tp3 = float(out["tp3"])
    be_moved = bool(out.get("breakeven_moved", False))
    tp2_locked = bool(out.get("tp2_locked", False))

    if side == "LONG":
        if not be_moved:
            if low <= stop:
                pnl_pct = ((stop - entry) / entry) * 100.0
                return {
                    "rec": out,
                    "closed": True,
                    "close_status": "CLOSED_STOP",
                    "close_reason": "STOP",
                    "close_price": stop,
                    "pnl_pct": pnl_pct,
                    "events": events,
                }

            if high >= tp1:
                out["stop"] = entry
                out["breakeven_moved"] = True
                be_moved = True
                events.append({"type": "TP1", "new_stop": entry})

        current_stop = float(out["stop"])

        if be_moved and (not tp2_locked) and high >= tp2:
            locked_stop = entry + ((tp1 - entry) * 0.75)
            out["stop"] = max(current_stop, locked_stop)
            out["tp2_locked"] = True
            tp2_locked = True
            events.append({"type": "TP2", "new_stop": out["stop"]})

        current_stop = float(out["stop"])

        if high >= tp3:
            pnl_pct = ((tp3 - entry) / entry) * 100.0
            return {
                "rec": out,
                "closed": True,
                "close_status": "CLOSED_TP3",
                "close_reason": "TP3",
                "close_price": tp3,
                "pnl_pct": pnl_pct,
                "events": events,
            }

        if low <= current_stop:
            pnl_pct = ((current_stop - entry) / entry) * 100.0
            if tp2_locked:
                status = "CLOSED_TP2"
                reason = "TP2_LOCK_STOP"
            elif be_moved:
                status = "CLOSED_BE"
                reason = "BE"
            else:
                status = "CLOSED_STOP"
                reason = "STOP"

            return {
                "rec": out,
                "closed": True,
                "close_status": status,
                "close_reason": reason,
                "close_price": current_stop,
                "pnl_pct": pnl_pct,
                "events": events,
            }

    else:
        if not be_moved:
            if high >= stop:
                pnl_pct = ((entry - stop) / entry) * 100.0
                return {
                    "rec": out,
                    "closed": True,
                    "close_status": "CLOSED_STOP",
                    "close_reason": "STOP",
                    "close_price": stop,
                    "pnl_pct": pnl_pct,
                    "events": events,
                }

            if low <= tp1:
                out["stop"] = entry
                out["breakeven_moved"] = True
                be_moved = True
                events.append({"type": "TP1", "new_stop": entry})

        current_stop = float(out["stop"])

        if be_moved and (not tp2_locked) and low <= tp2:
            locked_stop = entry - ((entry - tp1) * 0.75)
            out["stop"] = min(current_stop, locked_stop)
            out["tp2_locked"] = True
            tp2_locked = True
            events.append({"type": "TP2", "new_stop": out["stop"]})

        current_stop = float(out["stop"])

        if low <= tp3:
            pnl_pct = ((entry - tp3) / entry) * 100.0
            return {
                "rec": out,
                "closed": True,
                "close_status": "CLOSED_TP3",
                "close_reason": "TP3",
                "close_price": tp3,
                "pnl_pct": pnl_pct,
                "events": events,
            }

        if high >= current_stop:
            pnl_pct = ((entry - current_stop) / entry) * 100.0
            if tp2_locked:
                status = "CLOSED_TP2"
                reason = "TP2_LOCK_STOP"
            elif be_moved:
                status = "CLOSED_BE"
                reason = "BE"
            else:
                status = "CLOSED_STOP"
                reason = "STOP"

            return {
                "rec": out,
                "closed": True,
                "close_status": status,
                "close_reason": reason,
                "close_price": current_stop,
                "pnl_pct": pnl_pct,
                "events": events,
            }

    return {
        "rec": out,
        "closed": False,
        "close_status": None,
        "close_reason": None,
        "close_price": None,
        "pnl_pct": None,
        "events": events,
    }


def archive_closed_signal(state, key, rec):
    state["closed_signals"].append(rec)
    if len(state["closed_signals"]) > CLOSED_HISTORY_LIMIT:
        state["closed_signals"] = state["closed_signals"][-CLOSED_HISTORY_LIMIT:]
    if key in state["active_signals"]:
        del state["active_signals"][key]


def close_active_record(state, key, rec, close_status, close_reason, close_price, pnl_pct):
    rec["status"] = close_status
    rec["close_reason"] = close_reason
    rec["closed_time"] = now_utc_iso()
    rec["closed_price_est"] = close_price
    rec["pnl_pct"] = pnl_pct

    if close_status in ("CLOSED_TP2", "CLOSED_TP3"):
        state["last_outcomes"][last_outcome_key(rec["symbol"], rec["side"])] = "WIN"
    elif close_status == "CLOSED_BE":
        state["last_outcomes"][last_outcome_key(rec["symbol"], rec["side"])] = "BE"
    else:
        state["last_outcomes"][last_outcome_key(rec["symbol"], rec["side"])] = "LOSS"

    archive_closed_signal(state, key, rec)


def update_active_signals(state, valid_symbols):
    changed = False

    for key in list(state["active_signals"].keys()):
        rec = state["active_signals"].get(key)
        if not rec or rec.get("status") != "OPEN":
            continue
        if rec["symbol"] not in valid_symbols:
            continue

        try:
            if hours_since(rec["time"]) > ACTIVE_TRADE_MAX_AGE_HOURS:
                timeout_rec = dict(rec)
                timeout_rec["status"] = "CLOSED_TIMEOUT"
                timeout_rec["close_reason"] = "TIMEOUT"
                timeout_rec["closed_time"] = now_utc_iso()
                state["last_outcomes"][last_outcome_key(rec["symbol"], rec["side"])] = "TIMEOUT"
                archive_closed_signal(state, key, timeout_rec)
                changed = True
                send_telegram(
                    f"⏰ <b>SİNYAL TIMEOUT</b>\n\n"
                    f"<b>Parite:</b> {rec['symbol']}\n"
                    f"<b>Yön:</b> {rec['side']}"
                )
                continue

            df15 = get_klines(rec["symbol"], TF_ENTRY, 5)
            if len(df15) < 1:
                continue

            last = df15.iloc[-1]
            high = float(last["high"])
            low = float(last["low"])

            result = evaluate_position_on_bar(rec, high, low)
            new_rec = result["rec"]

            for ev in result["events"]:
                if ev["type"] == "TP1":
                    new_rec["tp1_hit_time"] = now_utc_iso()
                    send_telegram(
                        f"🟡 <b>TP1 GÖRÜLDÜ</b>\n\n"
                        f"<b>Parite:</b> {new_rec['symbol']}\n"
                        f"<b>Yön:</b> {new_rec['side']}\n"
                        f"<b>Yeni Stop:</b> {fmt_price(ev['new_stop'])}"
                    )
                    changed = True
                elif ev["type"] == "TP2":
                    new_rec["tp2_hit_time"] = now_utc_iso()
                    send_telegram(
                        f"🟢 <b>TP2 GÖRÜLDÜ</b>\n\n"
                        f"<b>Parite:</b> {new_rec['symbol']}\n"
                        f"<b>Yön:</b> {new_rec['side']}\n"
                        f"<b>Kilitli Stop:</b> {fmt_price(ev['new_stop'])}"
                    )
                    changed = True

            if result["closed"]:
                close_active_record(
                    state=state,
                    key=key,
                    rec=new_rec,
                    close_status=result["close_status"],
                    close_reason=result["close_reason"],
                    close_price=result["close_price"],
                    pnl_pct=result["pnl_pct"],
                )
                changed = True

                emoji = "🚀" if result["close_status"] == "CLOSED_TP3" else "🟢" if result["close_status"] == "CLOSED_TP2" else "⚪" if result["close_status"] == "CLOSED_BE" else "🔴"
                send_telegram(
                    f"{emoji} <b>SİNYAL KAPANDI</b>\n\n"
                    f"<b>Parite:</b> {new_rec['symbol']}\n"
                    f"<b>Yön:</b> {new_rec['side']}\n"
                    f"<b>Durum:</b> {result['close_status']}\n"
                    f"<b>Neden:</b> {result['close_reason']}\n"
                    f"<b>Çıkış:</b> {fmt_price(result['close_price'])}\n"
                    f"<b>Sonuç:</b> %{result['pnl_pct']:.2f}"
                )
                continue

            state["active_signals"][key] = new_rec

        except Exception as e:
            log(f"active signal update error {key}: {e}")

    if changed:
        save_state(state)


# =========================================================
# BACKTEST
# =========================================================

def generate_signal_for_index(df1h: pd.DataFrame, df15: pd.DataFrame, idx15: int, side: str, symbol: str):
    if idx15 < 220:
        return None

    ts = df15.iloc[idx15]["close_time"]
    df1h_cut = df1h[df1h["close_time"] <= ts].copy()
    if len(df1h_cut) < 220:
        return None

    df15_cut = df15.iloc[:idx15 + 1].copy()
    if len(df15_cut) < 220:
        return None

    fake_meta = {
        "quote_volume": 80_000_000,
        "spread_pct": 0.06,
        "pct_change_24h": -1.5 if side == "SHORT" else 2.0,
        "trade_count": 70000,
        "ok": True,
    }
    fake_btc = {"ok_long": True, "ok_short": True}
    fake_eth = {"ok_long": True, "ok_short": True}

    if side == "LONG":
        t = trend_long_ok(df1h_cut)
        if not t["ok"]:
            return None
        e = entry_long_signal(df15_cut, fake_meta)
        if not e["ok"]:
            return None
        s = build_signal(symbol, side, df15_cut)
        if not s["ok"]:
            return None
        score = score_long_signal(t, e, s, fake_meta, fake_btc, fake_eth)
        if score < MIN_SCORE_TO_SIGNAL_LONG:
            return None
    else:
        t = trend_short_ok(df1h_cut)
        if not t["ok"]:
            return None
        e = entry_short_signal(df15_cut, fake_meta, symbol)
        if not e["ok"]:
            return None
        s = build_signal(symbol, side, df15_cut)
        if not s["ok"]:
            return None
        score = score_short_signal(t, e, s, fake_meta, fake_btc, fake_eth, symbol)
        if score < MIN_SCORE_TO_SIGNAL_SHORT:
            return None

    s["score"] = score
    return s


def simulate_trade(df15: pd.DataFrame, idx15: int, sig: dict):
    rec = {
        "symbol": sig["symbol"],
        "side": sig["side"],
        "entry": sig["entry"],
        "stop": sig["stop"],
        "tp1": sig["tp1"],
        "tp2": sig["tp2"],
        "tp3": sig["tp3"],
        "breakeven_moved": False,
        "tp2_locked": False,
    }

    exit_reason = "TIMEOUT"
    exit_price = float(df15.iloc[min(idx15 + BACKTEST_MAX_HOLD_BARS, len(df15) - 1)]["close"])
    exit_idx = min(idx15 + BACKTEST_MAX_HOLD_BARS, len(df15) - 1)

    for j in range(idx15 + 1, min(idx15 + BACKTEST_MAX_HOLD_BARS + 1, len(df15))):
        row = df15.iloc[j]
        high = float(row["high"])
        low = float(row["low"])

        result = evaluate_position_on_bar(rec, high, low)
        rec = result["rec"]

        if result["closed"]:
            exit_reason = result["close_reason"]
            exit_price = result["close_price"]
            exit_idx = j
            break

    if sig["side"] == "LONG":
        gross_pnl_pct = ((exit_price - sig["entry"]) / sig["entry"]) * 100.0
    else:
        gross_pnl_pct = ((sig["entry"] - exit_price) / sig["entry"]) * 100.0

    net_pnl_pct = gross_pnl_pct - roundtrip_cost_pct()
    return {
        "side": sig["side"],
        "score": sig["score"],
        "exit_reason": exit_reason,
        "exit_price": exit_price,
        "exit_idx": exit_idx,
        "gross_pnl_pct": gross_pnl_pct,
        "net_pnl_pct": net_pnl_pct,
    }


def backtest_symbol(symbol: str, side: str):
    try:
        df1h = enrich(get_historical_klines(symbol, TF_TREND, BACKTEST_DAYS))
        df15 = enrich(get_historical_klines(symbol, TF_ENTRY, BACKTEST_DAYS))

        if len(df1h) < 260 or len(df15) < 300:
            return None

        trades = []
        idx = 220
        while idx < len(df15) - BACKTEST_MAX_HOLD_BARS - 1:
            sig = generate_signal_for_index(df1h, df15, idx, side, symbol)
            if sig:
                sim = simulate_trade(df15, idx, sig)
                trades.append(sim)
                idx += BACKTEST_BAR_SPACING
            else:
                idx += 1

        if not trades:
            return {
                "symbol": symbol,
                "side": side,
                "trades": 0,
                "win_rate": 0.0,
                "avg_net": 0.0,
                "total_net": 0.0,
            }

        wins = [t for t in trades if t["net_pnl_pct"] > 0]
        avg_net = sum(t["net_pnl_pct"] for t in trades) / len(trades)
        total_net = sum(t["net_pnl_pct"] for t in trades)
        win_rate = (len(wins) / len(trades)) * 100.0

        return {
            "symbol": symbol,
            "side": side,
            "trades": len(trades),
            "win_rate": round(win_rate, 2),
            "avg_net": round(avg_net, 2),
            "total_net": round(total_net, 2),
        }
    except Exception as e:
        log(f"backtest_symbol error {symbol} {side}: {e}")
        return None


def run_startup_backtest(valid_symbols):
    rows = []
    for symbol, rule in SYMBOL_RULES.items():
        if symbol not in valid_symbols:
            continue

        if rule.get("allow_long", False):
            r = backtest_symbol(symbol, "LONG")
            if r:
                rows.append(r)

        if rule.get("allow_short", False):
            r = backtest_symbol(symbol, "SHORT")
            if r:
                rows.append(r)

    if not rows:
        return {}

    report = {"rows": rows, "time": now_utc_iso()}
    lines = ["📊 <b>STARTUP BACKTEST RAPORU V3</b>\n"]
    for r in rows:
        lines.append(
            f"{r['symbol']} {r['side']} | işlem: {r['trades']} | "
            f"win: %{r['win_rate']:.2f} | avg: %{r['avg_net']:.2f} | total: %{r['total_net']:.2f}"
        )
    send_telegram("\n".join(lines))
    return report


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
    log("PRO HYBRID SIGNAL BOT V3 starting...")
    state = load_state()

    try:
        exchange_symbols = get_exchange_symbols()
        valid_symbols = {s for s in SYMBOL_RULES.keys() if s in exchange_symbols}
        skipped = sorted(set(SYMBOL_RULES.keys()) - valid_symbols)

        if skipped:
            log(f"Skipped unsupported symbols: {', '.join(skipped)}")

        if not valid_symbols:
            log("No valid symbols found on exchange.")
            return

        if RUN_BACKTEST_ON_START:
            try:
                report = run_startup_backtest(valid_symbols)
                state["last_backtest_report"] = report
                save_state(state)
            except Exception as e:
                log(f"startup backtest error: {e}")

        send_telegram(
            f"✅ <b>PRO BOT V3 BAŞLADI</b>\n\n"
            f"<b>Aktif Pariteler:</b> {', '.join(sorted(valid_symbols))}\n"
            f"<b>Session Filter:</b> {'ON' if REQUIRE_SESSION_FILTER else 'OFF'}\n"
            f"<b>BTC Filter:</b> {'ON' if REQUIRE_BTC_CONFIRMATION else 'OFF'}\n"
            f"<b>Max Sinyal/Tur:</b> {MAX_SIGNALS_PER_ROUND}"
        )

        while True:
            try:
                now_utc = datetime.now(timezone.utc)

                update_active_signals(state, valid_symbols)

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

                best = choose_best_signals(candidates, top_n=MAX_SIGNALS_PER_ROUND)

                if not best:
                    log("No signal this round.")
                else:
                    for sig in best:
                        key = active_key(sig["symbol"], sig["side"])
                        state["active_signals"][key] = make_active_record(sig)
                        state["last_signal_times"][cooldown_key(sig["symbol"], sig["side"])] = now_utc_iso()
                        send_telegram(entry_message(sig))
                        log(
                            f"SIGNAL {sig['symbol']} {sig['side']} "
                            f"score={sig['score']} risk={sig['risk_pct']:.2f}%"
                        )
                    save_state(state)

                time.sleep(SCAN_INTERVAL_SECONDS)

            except KeyboardInterrupt:
                log("Stopped by user.")
                break
            except Exception as e:
                log(f"main loop error: {e}")
                log(traceback.format_exc())
                time.sleep(15)

    except Exception as e:
        log(f"fatal error: {e}")
        log(traceback.format_exc())


if __name__ == "__main__":
    main()
