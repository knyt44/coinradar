#!/usr/bin/env python3
"""
BINANCE SUPER SPOT SCANNER - FINAL

Amaç:
- Binance spot USDT marketini tarar
- Çöp coinleri mümkün olduğunca filtreler
- Sadece LONG spot fırsatı arar
- Telegram'a giriş, stop, TP1, TP2, TP3 yollar
- Aktif sinyalleri takip eder
- TP1 sonrası BE, TP2 sonrası stop yükseltme yapar

Bu sürüm sinyal botudur, otomatik emir açmaz.

Kurulum:
    pip install requests pandas

Env:
    Railway Variables kullan:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Çalıştır:
    python bot.py
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

# ============================================================
# CONFIG
# ============================================================
BINANCE_BASE_URL = "https://fapi.binance.me"
STATE_FILE = "super_binance_spot_scanner_final_state.json"
LOG_FILE = "super_binance_spot_scanner_final.log"
HTTP_TIMEOUT = 15
CHECK_EVERY_SECONDS = 60
TOP_N_SIGNALS = 5
MAX_SYMBOLS_TO_SCAN = 140
QUOTE_ASSET = "USDT"
USER_AGENT = "super-binance-spot-scanner-final/1.0"

# Anti-trash filters
MIN_QUOTE_VOLUME_USDT = 10_000_000
MAX_SPREAD_PCT = 0.30
MIN_TRADES_24H = 25_000
MAX_24H_PUMP_PCT = 12.0
MIN_PRICE = 0.00001
MIN_LISTING_AGE_BARS_4H = 140
MAX_RANGE_COMPRESSION_PCT = 0.0125
MIN_ATR_PCT_15M = 0.0035
MAX_ATR_PCT_15M = 0.0280
MIN_24H_CHANGE_PCT = -8.0
MAX_SINGLE_CANDLE_BODY_PCT = 0.06
EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "BUSD", "DAI", "EUR", "TRY", "BRL",
    "AUD", "GBP", "BIDR", "UAH", "NGN", "RUB", "ZAR"
}
LEVERAGED_TOKEN_MARKERS = ["UP", "DOWN", "BULL", "BEAR"]

# Regime / timeframe settings
MIN_BARS_4H = 300
MIN_BARS_1H = 300
MIN_BARS_15M = 300
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
EMA_PULLBACK = 21
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
BREAKOUT_LOOKBACK = 20
VOLUME_SURGE_MULT = 1.35
RETEST_BUFFER_PCT = 0.0018
ENTRY_ZONE_BUFFER_PCT = 0.0022
MAX_DISTANCE_FROM_EMA20_ATR = 2.0
MAX_LAST_CANDLE_RANGE_ATR = 2.2
MAX_WICK_TO_BODY_RATIO = 3.0
MIN_BREAKOUT_BODY_ATR = 0.18
MIN_ADX_4H = 18
MIN_ADX_1H = 17
REQUIRE_BTC_CONFIRMATION = True
BTC_CONFIRMATION_SYMBOL = "BTCUSDT"

# Thresholds
BASE_MIN_SIGNAL_SCORE = 12
MIN_QUALITY_SCORE = 8
MIN_RR_TO_TP2 = 1.35
MIN_SCORE_ADVANTAGE = 0

# Risk plan
SL_ATR = 1.15
TP1_ATR = 1.40
TP2_ATR = 2.35
TP3_ATR = 3.25

# Lifecycle / adaptive logic
SIGNAL_COOLDOWN_HOURS = 8
ACTIVE_TRADE_MAX_AGE_HOURS = 36
SUMMARY_EVERY_MINUTES = 120
RECENT_RESULTS_WINDOW = 20
LOSS_COOLDOWN_MINUTES = 90
CONSECUTIVE_LOSS_LIMIT = 3

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ============================================================
# LOGGING
# ============================================================
logger = logging.getLogger("super_binance_spot_scanner_final")
logger.setLevel(logging.INFO)
logger.handlers.clear()
_formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(_formatter)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)
logger.addHandler(_stream_handler)

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": USER_AGENT})


# ============================================================
# DATA MODELS
# ============================================================
@dataclass
class Signal:
    symbol: str
    score: int
    quality: int
    side: str
    entry_low: float
    entry_high: float
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    rr_tp2: float
    price: float
    change_pct_24h: float
    quote_volume: float
    spread_pct: float
    breakout_level: float
    setup: str
    reasons: List[str]
    created_at: int


# ============================================================
# COMMON HELPERS
# ============================================================
def now_ts() -> int:
    return int(time.time())


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def round_smart(x: float) -> float:
    if x >= 1000:
        return round(x, 2)
    if x >= 100:
        return round(x, 3)
    if x >= 1:
        return round(x, 4)
    if x >= 0.1:
        return round(x, 5)
    if x >= 0.01:
        return round(x, 6)
    return round(x, 8)


def default_state() -> Dict:
    return {
        "sent": {},
        "active_trades": {},
        "results": [],
        "last_scan_ts": 0,
        "last_summary_ts": 0,
        "last_btc_note": "",
        "last_loss_ts": 0,
    }


def load_state() -> Dict:
    path = Path(STATE_FILE)
    if not path.exists():
        return default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_state()
        merged = default_state()
        merged.update(data)
        return merged
    except Exception as e:
        logger.warning("State okunamadı, sıfırlandı: %s", e)
        return default_state()


def save_state(state: Dict) -> None:
    tmp = Path(STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def get_recent_results(state: Dict, n: int = RECENT_RESULTS_WINDOW) -> List[str]:
    return state.get("results", [])[-n:]


def get_recent_stats(state: Dict) -> Dict:
    results = get_recent_results(state)
    if not results:
        return {"count": 0, "win_rate": 0.0, "losses": 0, "consecutive_losses": 0}

    wins = sum(1 for x in results if x in {"TP1", "TP2", "TP3"})
    losses = sum(1 for x in results if x in {"STOP", "BE_STOP"})

    consecutive_losses = 0
    for item in reversed(results):
        if item in {"STOP", "BE_STOP"}:
            consecutive_losses += 1
        else:
            break

    return {
        "count": len(results),
        "win_rate": (wins / len(results)) * 100.0,
        "losses": losses,
        "consecutive_losses": consecutive_losses,
    }


def effective_thresholds(state: Dict) -> Tuple[int, int]:
    score = BASE_MIN_SIGNAL_SCORE
    quality = MIN_QUALITY_SCORE
    stats = get_recent_stats(state)

    if stats["count"] >= 8:
        wr = stats["win_rate"]
        if wr < 45:
            score += 2
            quality += 1
        elif wr < 55:
            score += 1
            quality += 1
        elif wr > 72:
            score = max(score - 1, 10)
            quality = max(quality - 1, 7)

    return score, quality


def in_loss_cooldown(state: Dict) -> bool:
    stats = get_recent_stats(state)
    if stats["consecutive_losses"] < CONSECUTIVE_LOSS_LIMIT:
        return False
    last_loss_ts = int(state.get("last_loss_ts", 0))
    if last_loss_ts <= 0:
        return False
    return (now_ts() - last_loss_ts) < LOSS_COOLDOWN_MINUTES * 60


# ============================================================
# TELEGRAM
# ============================================================
def tg_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("Telegram env eksik; mesaj loga yazıldı:\n%s", text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [text]

    for chunk in chunks:
        try:
            r = HTTP.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if r.status_code != 200:
                logger.error("Telegram hata: %s", r.text)
        except Exception as e:
            logger.error("Telegram exception: %s", e)


# ============================================================
# BINANCE API
# ============================================================
def api_get(path: str, params: Optional[Dict] = None):
    url = f"{BINANCE_BASE_URL}{path}"
    r = HTTP.get(url, params=params or {}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_exchange_info() -> List[Dict]:
    data = api_get("/fapi/v1/exchangeInfo")
    return data.get("symbols", []) if isinstance(data, dict) else []


def get_24hr_tickers() -> List[Dict]:
    data = api_get("/fapi/v1/ticker/24hr")
    return data if isinstance(data, list) else []


def get_book_tickers() -> Dict[str, Dict]:
    data = api_get("/fapi/v1/ticker/bookTicker")
    if isinstance(data, dict):
        data = [data]
    return {item.get("symbol"): item for item in data if item.get("symbol")}


def get_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    data = api_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(
        data,
        columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "n_trades", "taker_base", "taker_quote", "ignore"
        ],
    )
    for col in ["open", "high", "low", "close", "volume", "quote_volume", "n_trades"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df.dropna().reset_index(drop=True)
    return df


# ============================================================
# INDICATORS
# ============================================================
def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def rsi(s: pd.Series, period: int = 14) -> pd.Series:
    delta = s.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.ewm(alpha=1 / period, adjust=False).mean()
    ma_down = down.ewm(alpha=1 / period, adjust=False).mean()
    rs = ma_up / ma_down.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move.clip(lower=0)
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move.clip(lower=0)
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_series.replace(0, pd.NA)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_series.replace(0, pd.NA)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)) * 100
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["ema20"] = ema(out["close"], EMA_FAST)
    out["ema50"] = ema(out["close"], EMA_MID)
    out["ema200"] = ema(out["close"], EMA_SLOW)
    out["ema21"] = ema(out["close"], EMA_PULLBACK)
    out["rsi"] = rsi(out["close"], RSI_PERIOD)
    out["atr"] = atr(out, ATR_PERIOD)
    out["adx"] = adx(out, ADX_PERIOD)
    out["vol_ma20"] = out["volume"].rolling(20).mean()
    out["atr_pct"] = out["atr"] / out["close"].replace(0, pd.NA)
    out["range_pct"] = (out["high"] - out["low"]) / out["close"].replace(0, pd.NA)
    out["body"] = (out["close"] - out["open"]).abs()
    out["body_pct"] = out["body"] / out["close"].replace(0, pd.NA)
    out["upper_wick"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_wick"] = out[["open", "close"]].min(axis=1) - out["low"]
    out["wick_to_body"] = (out[["upper_wick", "lower_wick"]].max(axis=1) / out["body"].replace(0, pd.NA)).fillna(999)
    out["high_breakout"] = out["high"].rolling(BREAKOUT_LOOKBACK).max().shift(2)
    out["compression"] = ((out["high"].rolling(12).max() - out["low"].rolling(12).min()) / out["close"]).fillna(999)
    return out


# ============================================================
# UNIVERSE FILTER
# ============================================================
def is_leveraged_or_unwanted(base_asset: str) -> bool:
    if base_asset in EXCLUDED_BASES:
        return True
    for marker in LEVERAGED_TOKEN_MARKERS:
        if base_asset.endswith(marker):
            return True
    return False


def build_universe() -> List[Dict]:
    exchange_info = get_exchange_info()
    tickers = {x.get("symbol"): x for x in get_24hr_tickers() if x.get("symbol")}
    books = get_book_tickers()

    universe: List[Dict] = []

    for item in exchange_info:
        symbol = item.get("symbol", "")
        status = item.get("status", "")
        quote = item.get("quoteAsset", "")
        base = item.get("baseAsset", "")

        if status != "TRADING":
            continue
        if quote != QUOTE_ASSET:
            continue
        if not symbol.endswith(QUOTE_ASSET):
            continue
        if is_leveraged_or_unwanted(base):
            continue

        ticker = tickers.get(symbol)
        book = books.get(symbol)
        if not ticker or not book:
            continue

        ask = safe_float(book.get("askPrice"))
        bid = safe_float(book.get("bidPrice"))
        last = safe_float(ticker.get("lastPrice"))
        quote_volume = safe_float(ticker.get("quoteVolume"))
        change_pct_24h = safe_float(ticker.get("priceChangePercent"))
        n_trades = int(safe_float(ticker.get("count"), 0))

        if last <= 0 or ask <= 0 or bid <= 0:
            continue
        if last < MIN_PRICE:
            continue
        if quote_volume < MIN_QUOTE_VOLUME_USDT:
            continue
        if n_trades < MIN_TRADES_24H:
            continue
        if change_pct_24h < MIN_24H_CHANGE_PCT:
            continue
        if change_pct_24h > MAX_24H_PUMP_PCT:
            continue

        spread_pct = ((ask - bid) / ask) * 100
        if spread_pct > MAX_SPREAD_PCT:
            continue

        universe.append({
            "symbol": symbol,
            "price": last,
            "quote_volume": quote_volume,
            "change_pct_24h": change_pct_24h,
            "spread_pct": spread_pct,
            "n_trades": n_trades,
        })

    universe.sort(key=lambda x: (x["quote_volume"], x["n_trades"]), reverse=True)
    return universe[:MAX_SYMBOLS_TO_SCAN]


# ============================================================
# BTC REGIME FILTER
# ============================================================
def btc_regime_ok() -> Tuple[bool, str]:
    try:
        df1h = add_indicators(get_klines(BTC_CONFIRMATION_SYMBOL, "1h", 260))
        df15 = add_indicators(get_klines(BTC_CONFIRMATION_SYMBOL, "15m", 260))
        if len(df1h) < 220 or len(df15) < 220:
            return False, "BTC veri yetersiz"

        l1 = df1h.iloc[-2]
        l15 = df15.iloc[-2]

        cond_trend = l1["ema20"] > l1["ema50"] > l1["ema200"]
        cond_rsi = l1["rsi"] >= 51
        cond_price = l15["close"] > l15["ema20"]

        if cond_trend and cond_rsi and cond_price:
            return True, "BTC rejimi uyumlu"
        return False, "BTC rejimi zayıf"
    except Exception as e:
        return False, f"BTC filtre hatası: {e}"


# ============================================================
# SIGNAL ENGINE
# ============================================================
def calc_rr(entry: float, stop: float, target: float) -> float:
    risk = max(entry - stop, 1e-12)
    reward = target - entry
    return reward / risk


def evaluate_symbol(meta: Dict, min_score: int, min_quality: int) -> Optional[Signal]:
    symbol = meta["symbol"]

    try:
        df4 = add_indicators(get_klines(symbol, "4h", MIN_BARS_4H))
        df1 = add_indicators(get_klines(symbol, "1h", MIN_BARS_1H))
        df15 = add_indicators(get_klines(symbol, "15m", MIN_BARS_15M))
    except Exception as e:
        logger.warning("%s veri çekim hatası: %s", symbol, e)
        return None

    if len(df4) < max(EMA_SLOW + 10, MIN_LISTING_AGE_BARS_4H):
        return None
    if len(df1) < EMA_SLOW + 10 or len(df15) < EMA_SLOW + 10:
        return None

    l4 = df4.iloc[-2]
    l1 = df1.iloc[-2]
    prev15 = df15.iloc[-3]
    l15 = df15.iloc[-2]

    # Hard filters
    if pd.isna(l15["atr"]) or l15["atr"] <= 0:
        return None
    if not (MIN_ATR_PCT_15M <= l15["atr_pct"] <= MAX_ATR_PCT_15M):
        return None
    if l15["compression"] > MAX_RANGE_COMPRESSION_PCT:
        return None
    if l15["wick_to_body"] > MAX_WICK_TO_BODY_RATIO:
        return None
    if l15["range_pct"] > (l15["atr_pct"] * MAX_LAST_CANDLE_RANGE_ATR):
        return None
    if l15["body_pct"] > MAX_SINGLE_CANDLE_BODY_PCT:
        return None

    score = 0
    quality = 0
    reasons: List[str] = []

    # 4H trend
    if l4["ema20"] > l4["ema50"] > l4["ema200"]:
        score += 3
        quality += 2
        reasons.append("4h trend güçlü")
    elif l4["ema20"] > l4["ema50"]:
        score += 1
        reasons.append("4h trend pozitif")
    else:
        return None

    if l4["rsi"] >= 54:
        score += 1
        quality += 1
        reasons.append("4h RSI destekli")
    if l4["adx"] >= MIN_ADX_4H:
        score += 1
        quality += 1
        reasons.append("4h ADX iyi")

    # 1H trend / momentum
    if l1["ema20"] > l1["ema50"] > l1["ema200"]:
        score += 3
        quality += 2
        reasons.append("1h momentum güçlü")
    elif l1["ema20"] > l1["ema50"]:
        score += 1
        reasons.append("1h momentum pozitif")
    else:
        return None

    if 52 <= l1["rsi"] <= 72:
        score += 1
        quality += 1
        reasons.append("1h RSI sağlıklı")
    if l1["adx"] >= MIN_ADX_1H:
        score += 1
        quality += 1
        reasons.append("1h ADX destekli")

    # 15M alignment
    if l15["ema20"] > l15["ema50"] and l15["close"] > l15["ema20"]:
        score += 1
        reasons.append("15m trend yönünde")
    else:
        return None

    breakout_level = l15["high_breakout"]
    if pd.isna(breakout_level) or breakout_level <= 0:
        return None

    breakout = (
        prev15["close"] > breakout_level and
        prev15["body"] >= (prev15["atr"] * MIN_BREAKOUT_BODY_ATR)
    )
    retest_hold = (
        l15["low"] <= breakout_level * (1 + RETEST_BUFFER_PCT) and
        l15["close"] > breakout_level
    )
    pullback_ok = (
        l15["low"] <= l15["ema21"] * 1.003 and
        l15["close"] > l15["ema21"]
    )
    volume_ok = bool(pd.notna(l15["vol_ma20"]) and l15["volume"] >= l15["vol_ma20"] * VOLUME_SURGE_MULT)
    not_extended = ((l15["close"] - l15["ema20"]) / l15["atr"]) <= MAX_DISTANCE_FROM_EMA20_ATR

    if not breakout:
        return None
    score += 2
    quality += 1
    reasons.append("15m breakout")

    if not retest_hold:
        return None
    score += 2
    quality += 2
    reasons.append("retest tuttu")

    if pullback_ok:
        score += 1
        quality += 1
        reasons.append("EMA21 pullback")

    if volume_ok:
        score += 1
        quality += 1
        reasons.append("hacim güçlü")

    if not not_extended:
        return None
    score += 1
    reasons.append("giriş çok geç değil")

    entry_low = breakout_level * (1 + 0.0004)
    entry_high = breakout_level * (1 + ENTRY_ZONE_BUFFER_PCT)
    entry = min(max(l15["close"], entry_low), entry_high)
    stop = entry - (l15["atr"] * SL_ATR)
    tp1 = entry + (l15["atr"] * TP1_ATR)
    tp2 = entry + (l15["atr"] * TP2_ATR)
    tp3 = entry + (l15["atr"] * TP3_ATR)

    if stop <= 0 or entry <= stop:
        return None

    rr_tp2 = calc_rr(entry, stop, tp2)
    if rr_tp2 < MIN_RR_TO_TP2:
        return None

    if score < min_score:
        return None
    if quality < min_quality:
        return None
    if (score - min_score) < MIN_SCORE_ADVANTAGE:
        return None

    return Signal(
        symbol=symbol,
        score=score,
        quality=quality,
        side="LONG",
        entry_low=round_smart(entry_low),
        entry_high=round_smart(entry_high),
        entry=round_smart(entry),
        stop=round_smart(stop),
        tp1=round_smart(tp1),
        tp2=round_smart(tp2),
        tp3=round_smart(tp3),
        rr_tp2=round(rr_tp2, 2),
        price=round_smart(meta["price"]),
        change_pct_24h=round(meta["change_pct_24h"], 2),
        quote_volume=round(meta["quote_volume"], 0),
        spread_pct=round(meta["spread_pct"], 3),
        breakout_level=round_smart(breakout_level),
        setup="Breakout + Retest + Pullback",
        reasons=reasons,
        created_at=now_ts(),
    )


# ============================================================
# ACTIVE TRADE MANAGEMENT
# ============================================================
def add_active_trade(state: Dict, sig: Signal) -> None:
    state.setdefault("active_trades", {})[sig.symbol] = {
        **asdict(sig),
        "tp1_hit": False,
        "tp2_hit": False,
    }


def close_trade(state: Dict, symbol: str, result: str) -> None:
    state.setdefault("results", []).append(result)
    state["results"] = state["results"][-200:]
    if result in {"STOP", "BE_STOP"}:
        state["last_loss_ts"] = now_ts()
    state.get("active_trades", {}).pop(symbol, None)


def check_active_trades(state: Dict) -> None:
    active = dict(state.get("active_trades", {}))

    for symbol, tr in active.items():
        try:
            df = get_klines(symbol, "15m", 5)
            if df.empty:
                continue

            last = df.iloc[-2]
            high = safe_float(last["high"])
            low = safe_float(last["low"])
            created_at = int(tr.get("created_at", 0))

            if now_ts() - created_at > ACTIVE_TRADE_MAX_AGE_HOURS * 3600:
                close_trade(state, symbol, "TIMEOUT")
                tg_send(f"⏳ {symbol} trade timeout; takip kapatıldı.")
                continue

            stop = safe_float(tr["stop"])
            entry = safe_float(tr["entry"])
            tp1 = safe_float(tr["tp1"])
            tp2 = safe_float(tr["tp2"])
            tp3 = safe_float(tr["tp3"])

            if not tr.get("tp1_hit") and high >= tp1:
                tr["tp1_hit"] = True
                tr["stop"] = round_smart(entry)
                tg_send(f"✅ {symbol} TP1 görüldü. Stop BE seviyesine çekildi: {tr['stop']}")

            if not tr.get("tp2_hit") and high >= tp2:
                tr["tp2_hit"] = True
                tr["stop"] = round_smart((entry + tp1) / 2)
                tg_send(f"✅ {symbol} TP2 görüldü. Stop yukarı alındı: {tr['stop']}")

            if high >= tp3:
                close_trade(state, symbol, "TP3")
                tg_send(f"🏁 {symbol} TP3 tamamlandı.")
                continue

            if low <= safe_float(tr["stop"]):
                result = "BE_STOP" if tr.get("tp1_hit") else "STOP"
                close_trade(state, symbol, result)
                tg_send(f"🛑 {symbol} stop oldu. Sonuç: {result}")
                continue

            state["active_trades"][symbol] = tr
        except Exception as e:
            logger.warning("Aktif trade kontrol hatası %s: %s", symbol, e)


# ============================================================
# MESSAGE FORMATTING
# ============================================================
def format_signal(sig: Signal, min_score: int, min_quality: int) -> str:
    return (
        f"🚀 BINANCE SUPER SPOT SCANNER\n"
        f"Saat: {utc_now_str()}\n"
        f"Coin: {sig.symbol}\n"
        f"Yön: {sig.side}\n"
        f"Setup: {sig.setup}\n"
        f"Skor: {sig.score}/{min_score}+\n"
        f"Kalite: {sig.quality}/{min_quality}+\n"
        f"Son Fiyat: {sig.price}\n"
        f"Breakout Seviye: {sig.breakout_level}\n"
        f"Giriş Bölgesi: {sig.entry_low} - {sig.entry_high}\n"
        f"İdeal Giriş: {sig.entry}\n"
        f"Stop: {sig.stop}\n"
        f"TP1: {sig.tp1}\n"
        f"TP2: {sig.tp2}\n"
        f"TP3: {sig.tp3}\n"
        f"RR(TP2): {sig.rr_tp2}\n"
        f"24s Değişim: {sig.change_pct_24h}%\n"
        f"24s Hacim: {sig.quote_volume:,.0f} USDT\n"
        f"Spread: {sig.spread_pct}%\n"
        f"Neden: {', '.join(sig.reasons[:8])}\n"
        f"Not: Otomatik emir açmaz; manuel onaylı sinyal için tasarlanmıştır."
    )


def format_summary(signals: List[Signal], btc_note: str, min_score: int, min_quality: int, state: Dict) -> str:
    stats = get_recent_stats(state)
    lines = [
        f"📊 BINANCE spot tarama özeti | {utc_now_str()}",
        f"BTC filtre: {btc_note}",
        f"Aktif eşik: skor>={min_score} | kalite>={min_quality}",
        f"Son {stats['count']} sonuç win-rate: %{stats['win_rate']:.1f}",
        f"Bulunan sinyal: {len(signals)}",
        "",
    ]
    for i, s in enumerate(signals[:TOP_N_SIGNALS], 1):
        lines.append(f"{i}) {s.symbol} | skor {s.score} | kalite {s.quality} | giriş {s.entry} | stop {s.stop} | tp2 {s.tp2}")
    return "\n".join(lines)


# ============================================================
# MAIN ENGINE
# ============================================================
def run_scan(state: Dict) -> List[Signal]:
    check_active_trades(state)

    if in_loss_cooldown(state):
        msg = f"⏸️ Loss cooldown aktif. Yeni sinyal aranmayacak. Saat: {utc_now_str()}"
        logger.info(msg)
        save_state(state)
        return []

    min_score, min_quality = effective_thresholds(state)

    btc_ok, btc_note = btc_regime_ok()
    state["last_btc_note"] = btc_note

    if REQUIRE_BTC_CONFIRMATION and not btc_ok:
        logger.info("Tarama pas: %s", btc_note)
        tg_send(f"⚠️ Binance spot tarama pas: {btc_note}\nSaat: {utc_now_str()}")
        save_state(state)
        return []

    universe = build_universe()
    logger.info("Tarama evreni: %s coin", len(universe))

    signals: List[Signal] = []
    for meta in universe:
        sig = evaluate_symbol(meta, min_score, min_quality)
        if sig:
            signals.append(sig)

    signals.sort(key=lambda x: (x.score, x.quality, x.quote_volume, x.rr_tp2), reverse=True)
    signals = signals[:TOP_N_SIGNALS]

    if signals and now_ts() - int(state.get("last_summary_ts", 0)) >= SUMMARY_EVERY_MINUTES * 60:
        tg_send(format_summary(signals, btc_note, min_score, min_quality, state))
        state["last_summary_ts"] = now_ts()

    sent = state.setdefault("sent", {})
    for sig in signals:
        old = sent.get(sig.symbol)
        cooldown_ok = True
        if old:
            old_ts = int(old.get("created_at", 0))
            if now_ts() - old_ts < SIGNAL_COOLDOWN_HOURS * 3600:
                cooldown_ok = False

        if cooldown_ok:
            tg_send(format_signal(sig, min_score, min_quality))
            sent[sig.symbol] = asdict(sig)
            add_active_trade(state, sig)
            logger.info("Sinyal gönderildi: %s skor=%s kalite=%s", sig.symbol, sig.score, sig.quality)

    state["last_scan_ts"] = now_ts()
    save_state(state)
    return signals


def main() -> None:
    logger.info("BINANCE SUPER SPOT SCANNER FINAL başlıyor")
    logger.info("Endpoint: %s", BINANCE_BASE_URL)
    state = load_state()

    while True:
        try:
            run_scan(state)
        except KeyboardInterrupt:
            logger.info("Bot durduruldu.")
            break
        except Exception as e:
            logger.exception("Ana döngü hatası: %s", e)
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
