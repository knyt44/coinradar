#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ETH SUPERBOT PRO+ AI WHALE NEWS SWING
=========================================================
Özellikler:
- ETHUSDT için LONG / SHORT sinyal üretir
- 15m trend + 5m entry mantığı
- 1h / 2h / 4h swing uygunluk analizi
- BTCUSDT çoklu timeframe rejim filtresi
- Balina baskı alarmı (open interest + large trade pressure)
- Haber / risk alarmı (RSS headline tarama + yaklaşan event uyarısı)
- AI yorumlama metni
- Telegram bildirimleri
- State yönetimi / cooldown / aktif sinyal takibi

Not:
- "Haberi önceden bilme" mümkün değildir.
- Bu sürüm yeni yayınlanan başlıkları erken algılar ve yaklaşan event uyarısı verir.
- Balina modülü, public veri üzerinden baskı tahmini yapar; %100 kesin değildir.
"""

import os
import re
import time
import json
import math
import traceback
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

import requests

# =========================================================
# CONFIG
# =========================================================
SYMBOL = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"
MARKET_TYPE = "futures"   # "futures" veya "spot"

SPOT_BASE_URL = "https://api.binance.com"
FUTURES_BASE_URL = "https://fapi.binance.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CHECK_EVERY_SECONDS = 20
STATE_FILE = "eth_superbot_pro_plus_state.json"

HTTP_TIMEOUT = 15
MAX_HTTP_RETRY = 3

# ---------------------------------------------------------
# SIGNAL / LIFECYCLE
# ---------------------------------------------------------
MIN_SIGNAL_GAP_MINUTES = 25
MAX_ACTIVE_SIGNAL_AGE_MINUTES = 180
SIGNAL_TIMEOUT_MINUTES = 180
MIN_PRICE_DISTANCE_PCT = 0.45
REVERSE_SIGNAL_STRENGTH_BONUS = 1.75
SAME_DIRECTION_SCORE_BONUS = 2.0

# ---------------------------------------------------------
# THRESHOLDS
# ---------------------------------------------------------
LONG_SCORE_THRESHOLD = 9.25
SHORT_SCORE_THRESHOLD = 9.25
MIN_RR = 1.25

# ---------------------------------------------------------
# RISK
# ---------------------------------------------------------
ATR_SL_MULTIPLIER = 1.20
ATR_TP1_MULTIPLIER = 1.50
ATR_TP2_MULTIPLIER = 2.60
ATR_TP3_MULTIPLIER = 3.60

# ---------------------------------------------------------
# BTC FILTER
# ---------------------------------------------------------
USE_BTC_FILTER = True
BTC_15M_TREND_THRESHOLD = 0.12
BTC_1H_TREND_THRESHOLD = 0.28
BTC_4H_TREND_THRESHOLD = 0.75
BTC_DUMP_BLOCK_PCT = -0.85
BTC_PUMP_BLOCK_PCT = 0.85

# ---------------------------------------------------------
# WHALE / FLOW
# ---------------------------------------------------------
ENABLE_WHALE_ALERTS = True
WHALE_ALERT_COOLDOWN_MINUTES = 20
LARGE_TRADE_NOTIONAL_USDT = 500000  # tek trade yaklaşık eşik
AGGTRADE_LIMIT = 250
OI_ALERT_THRESHOLD_PCT = 1.20
TRADE_PRESSURE_DOMINANCE_PCT = 62.0

# ---------------------------------------------------------
# NEWS / RISK
# ---------------------------------------------------------
ENABLE_NEWS_ALERTS = True
NEWS_ALERT_COOLDOWN_MINUTES = 30
NEWS_LOOKBACK_MINUTES = 120

# Public RSS kaynakları
NEWS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]

# Manüel yaklaşan event listesi
# İstersen bunları güncellersin
# UTC format: "YYYY-MM-DD HH:MM"
UPCOMING_EVENTS = [
    {"name": "US CPI", "time_utc": "2026-04-10 12:30", "impact": "HIGH"},
    {"name": "FOMC", "time_utc": "2026-05-06 18:00", "impact": "HIGH"},
    {"name": "PCE", "time_utc": "2026-04-30 12:30", "impact": "MEDIUM"},
]

EVENT_PREALERT_HOURS = [24, 6, 1]

NEWS_KEYWORDS_HIGH = [
    "sec", "etf", "hack", "exploit", "liquidation", "lawsuit", "ban", "fed",
    "fomc", "cpi", "pce", "rate", "interest rate", "war", "tariff",
    "delist", "bankruptcy", "outage", "security breach", "approval", "rejection"
]

NEWS_KEYWORDS_CRYPTO = [
    "bitcoin", "btc", "ethereum", "eth", "binance", "coinbase", "crypto",
    "exchange", "stablecoin", "usdt", "usdc", "staking", "on-chain", "whale",
    "futures", "open interest", "liquidation"
]

# =========================================================
# UTILS
# =========================================================
def now_utc():
    return datetime.now(timezone.utc)

def now_local_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def utc_ts():
    return int(now_utc().timestamp())

def log(msg: str):
    print(f"[{now_local_str()}] {msg}")

def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def safe_int(x, default=None):
    try:
        return int(x)
    except Exception:
        return default

def pct_change(a, b):
    a = safe_float(a)
    b = safe_float(b)
    if a in (None, 0) or b is None:
        return 0.0
    return ((b - a) / a) * 100.0

def pct_diff(a, b):
    a = safe_float(a)
    b = safe_float(b)
    if a in (None, 0) or b is None:
        return 999.0
    return abs(a - b) / a * 100.0

def minutes_since(ts):
    if not ts:
        return 999999
    return (utc_ts() - int(ts)) / 60.0

def fmt_price(v):
    try:
        fv = float(v)
        if fv >= 1000:
            return f"{fv:,.2f}"
        if fv >= 1:
            return f"{fv:.2f}"
        return f"{fv:.6f}"
    except Exception:
        return str(v)

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

# =========================================================
# TELEGRAM
# =========================================================
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("ENV eksik: TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            return True
        log(f"Telegram hata: {r.status_code} | {r.text}")
        return False
    except Exception as e:
        log(f"Telegram exception: {e}")
        return False

# =========================================================
# HTTP
# =========================================================
def http_get_json(url: str, params=None):
    last_err = None
    for _ in range(MAX_HTTP_RETRY):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(1.0)
    raise last_err

def http_get_text(url: str):
    last_err = None
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ETHSuperBot/1.0)"
    }
    for _ in range(MAX_HTTP_RETRY):
        try:
            r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            time.sleep(1.0)
    raise last_err

# =========================================================
# MARKET DATA
# =========================================================
def get_base_url():
    return FUTURES_BASE_URL if MARKET_TYPE == "futures" else SPOT_BASE_URL

def get_klines(symbol: str, interval: str, limit: int = 200):
    if MARKET_TYPE == "futures":
        url = f"{FUTURES_BASE_URL}/fapi/v1/klines"
    else:
        url = f"{SPOT_BASE_URL}/api/v3/klines"

    data = http_get_json(url, params={
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    })

    candles = []
    for k in data:
        candles.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": int(k[6]),
        })
    return candles

def get_mark_price(symbol: str):
    if MARKET_TYPE == "futures":
        url = f"{FUTURES_BASE_URL}/fapi/v1/premiumIndex"
        data = http_get_json(url, params={"symbol": symbol})
        return float(data["markPrice"])
    else:
        url = f"{SPOT_BASE_URL}/api/v3/ticker/price"
        data = http_get_json(url, params={"symbol": symbol})
        return float(data["price"])

def get_agg_trades(symbol: str, limit: int = 250):
    if MARKET_TYPE == "futures":
        url = f"{FUTURES_BASE_URL}/fapi/v1/aggTrades"
    else:
        url = f"{SPOT_BASE_URL}/api/v3/aggTrades"

    return http_get_json(url, params={
        "symbol": symbol,
        "limit": limit
    })

def get_open_interest(symbol: str):
    if MARKET_TYPE != "futures":
        return None
    url = f"{FUTURES_BASE_URL}/fapi/v1/openInterest"
    data = http_get_json(url, params={"symbol": symbol})
    return safe_float(data.get("openInterest"))

def get_open_interest_hist(symbol: str, period: str = "5m", limit: int = 30):
    if MARKET_TYPE != "futures":
        return []
    url = f"{FUTURES_BASE_URL}/futures/data/openInterestHist"
    try:
        data = http_get_json(url, params={
            "symbol": symbol,
            "period": period,
            "limit": limit
        })
        return data if isinstance(data, list) else []
    except Exception:
        return []

# =========================================================
# INDICATORS
# =========================================================
def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def ema_series(values, period):
    if len(values) < period:
        return [None] * len(values)

    alpha = 2 / (period + 1)
    result = [None] * len(values)

    seed = sum(values[:period]) / period
    result[period - 1] = seed
    prev = seed

    for i in range(period, len(values)):
        prev = (values[i] - prev) * alpha + prev
        result[i] = prev

    return result

def rsi_series(values, period=14):
    if len(values) < period + 1:
        return [None] * len(values)

    gains = [0.0]
    losses = [0.0]

    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    result = [None] * len(values)

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))

    for i in range(period + 1, len(values)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))

    return result

def macd_series(values, fast=12, slow=26, signal=9):
    ema_fast = ema_series(values, fast)
    ema_slow = ema_series(values, slow)

    macd_line = [None] * len(values)
    for i in range(len(values)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]

    valid = [x for x in macd_line if x is not None]
    if len(valid) < signal:
        return macd_line, [None] * len(values), [None] * len(values)

    signal_valid = ema_series(valid, signal)
    signal_line = [None] * len(values)

    vi = 0
    for i in range(len(values)):
        if macd_line[i] is not None:
            signal_line[i] = signal_valid[vi]
            vi += 1

    hist = [None] * len(values)
    for i in range(len(values)):
        if macd_line[i] is not None and signal_line[i] is not None:
            hist[i] = macd_line[i] - signal_line[i]

    return macd_line, signal_line, hist

def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period

def highest_high(candles, period):
    if len(candles) < period:
        return None
    return max(x["high"] for x in candles[-period:])

def lowest_low(candles, period):
    if len(candles) < period:
        return None
    return min(x["low"] for x in candles[-period:])

# =========================================================
# STATE IO
# =========================================================
def default_state():
    return {
        "last_signal": None,
        "active_signal": None,
        "signal_history": [],
        "last_whale_alert_ts": 0,
        "last_news_alert_ts": 0,
        "sent_news_keys": [],
        "sent_event_keys": []
    }

def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return default_state()
            base = default_state()
            base.update(data)
            if not isinstance(base.get("sent_news_keys"), list):
                base["sent_news_keys"] = []
            if not isinstance(base.get("sent_event_keys"), list):
                base["sent_event_keys"] = []
            return base
    except Exception as e:
        log(f"State load exception: {e}")
        return default_state()

def save_state(state):
    tmp_file = STATE_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, STATE_FILE)

# =========================================================
# ANALYSIS HELPERS
# =========================================================
def analyze_timeframe(candles):
    closed = candles[:-1] if len(candles) > 2 else candles[:]
    if len(closed) < 60:
        raise ValueError("Yeterli kapalı mum verisi yok")

    closes = [c["close"] for c in closed]
    highs = [c["high"] for c in closed]
    lows = [c["low"] for c in closed]
    volumes = [c["volume"] for c in closed]

    ema9 = ema_series(closes, 9)
    ema21 = ema_series(closes, 21)
    ema50 = ema_series(closes, 50)
    rsi14 = rsi_series(closes, 14)
    macd_line, macd_signal, macd_hist = macd_series(closes, 12, 26, 9)

    return {
        "close": closes[-1],
        "prev_close": closes[-2] if len(closes) >= 2 else closes[-1],
        "ema9": ema9[-1],
        "ema21": ema21[-1],
        "ema50": ema50[-1],
        "rsi14": rsi14[-1],
        "macd": macd_line[-1],
        "macd_signal": macd_signal[-1],
        "macd_hist": macd_hist[-1],
        "atr14": atr(closed, 14),
        "swing_high_20": highest_high(closed, 20),
        "swing_low_20": lowest_low(closed, 20),
        "vol_last": volumes[-1],
        "vol_sma20": sma(volumes, 20),
        "high_last": highs[-1],
        "low_last": lows[-1],
    }

def calc_tf_bias(tf):
    score = 0
    reasons = []

    if tf["ema9"] and tf["ema21"] and tf["ema50"]:
        if tf["ema9"] > tf["ema21"] > tf["ema50"]:
            score += 2
            reasons.append("ema bull stack")
        elif tf["ema9"] < tf["ema21"] < tf["ema50"]:
            score -= 2
            reasons.append("ema bear stack")

    if tf["close"] > (tf["ema21"] or 0):
        score += 1
        reasons.append("close > ema21")
    else:
        score -= 1
        reasons.append("close < ema21")

    if tf["rsi14"] is not None:
        if tf["rsi14"] >= 56:
            score += 1
            reasons.append(f"rsi bullish {round(tf['rsi14'],2)}")
        elif tf["rsi14"] <= 44:
            score -= 1
            reasons.append(f"rsi bearish {round(tf['rsi14'],2)}")

    if tf["macd_hist"] is not None:
        if tf["macd_hist"] > 0:
            score += 1
            reasons.append("macd hist positive")
        elif tf["macd_hist"] < 0:
            score -= 1
            reasons.append("macd hist negative")

    if score >= 2:
        return "LONG", score, reasons
    if score <= -2:
        return "SHORT", score, reasons
    return "NEUTRAL", score, reasons

def get_btc_regime():
    if not USE_BTC_FILTER:
        return {
            "bias": "NEUTRAL",
            "score": 0.0,
            "reason": "BTC filter disabled",
            "block_new_signals": False
        }

    try:
        btc_15m = analyze_timeframe(get_klines(BTC_SYMBOL, "15m", 200))
        btc_1h = analyze_timeframe(get_klines(BTC_SYMBOL, "1h", 200))
        btc_4h = analyze_timeframe(get_klines(BTC_SYMBOL, "4h", 200))

        bias_15m, score_15m, _ = calc_tf_bias(btc_15m)
        bias_1h, score_1h, _ = calc_tf_bias(btc_1h)
        bias_4h, score_4h, _ = calc_tf_bias(btc_4h)

        move_15m = pct_change(btc_15m["prev_close"], btc_15m["close"])
        move_1h = pct_change(btc_1h["prev_close"], btc_1h["close"])
        move_4h = pct_change(btc_4h["prev_close"], btc_4h["close"])

        long_score = 0.0
        short_score = 0.0
        reasons = []

        if bias_15m == "LONG":
            long_score += 1.3
            reasons.append("btc 15m long")
        elif bias_15m == "SHORT":
            short_score += 1.3
            reasons.append("btc 15m short")

        if bias_1h == "LONG":
            long_score += 1.8
            reasons.append("btc 1h long")
        elif bias_1h == "SHORT":
            short_score += 1.8
            reasons.append("btc 1h short")

        if bias_4h == "LONG":
            long_score += 2.4
            reasons.append("btc 4h long")
        elif bias_4h == "SHORT":
            short_score += 2.4
            reasons.append("btc 4h short")

        if move_15m >= BTC_15M_TREND_THRESHOLD:
            long_score += 0.8
            reasons.append(f"btc 15m move {round(move_15m,3)}%")
        elif move_15m <= -BTC_15M_TREND_THRESHOLD:
            short_score += 0.8
            reasons.append(f"btc 15m move {round(move_15m,3)}%")

        if move_1h >= BTC_1H_TREND_THRESHOLD:
            long_score += 1.0
            reasons.append(f"btc 1h move {round(move_1h,3)}%")
        elif move_1h <= -BTC_1H_TREND_THRESHOLD:
            short_score += 1.0
            reasons.append(f"btc 1h move {round(move_1h,3)}%")

        if move_4h >= BTC_4H_TREND_THRESHOLD:
            long_score += 1.2
            reasons.append(f"btc 4h move {round(move_4h,3)}%")
        elif move_4h <= -BTC_4H_TREND_THRESHOLD:
            short_score += 1.2
            reasons.append(f"btc 4h move {round(move_4h,3)}%")

        block_new_signals = False
        if move_15m <= BTC_DUMP_BLOCK_PCT:
            block_new_signals = True
            reasons.append("btc dump block active")
        elif move_15m >= BTC_PUMP_BLOCK_PCT:
            block_new_signals = True
            reasons.append("btc pump block active")

        if long_score > short_score + 1.0:
            bias = "LONG"
            final_score = long_score
        elif short_score > long_score + 1.0:
            bias = "SHORT"
            final_score = short_score
        else:
            bias = "NEUTRAL"
            final_score = max(long_score, short_score)

        return {
            "bias": bias,
            "score": round(final_score, 2),
            "reason": " | ".join(reasons[:10]),
            "block_new_signals": block_new_signals,
            "move_15m": round(move_15m, 3),
            "move_1h": round(move_1h, 3),
            "move_4h": round(move_4h, 3),
            "tf": {
                "15m": btc_15m,
                "1h": btc_1h,
                "4h": btc_4h
            }
        }
    except Exception as e:
        return {
            "bias": "NEUTRAL",
            "score": 0.0,
            "reason": f"BTC fail-open: {e}",
            "block_new_signals": False
        }

def classify_swing(tf_name, tf):
    score = 0
    notes = []

    if tf["ema21"] and tf["ema50"]:
        if tf["close"] > tf["ema21"] > tf["ema50"]:
            score += 2
            notes.append("trend up")
        elif tf["close"] < tf["ema21"] < tf["ema50"]:
            score -= 2
            notes.append("trend down")

    if tf["rsi14"] is not None:
        if 53 <= tf["rsi14"] <= 72:
            score += 1
            notes.append("rsi strong")
        elif 28 <= tf["rsi14"] <= 47:
            score -= 1
            notes.append("rsi weak")

    if tf["macd_hist"] is not None:
        if tf["macd_hist"] > 0:
            score += 1
            notes.append("macd+")
        elif tf["macd_hist"] < 0:
            score -= 1
            notes.append("macd-")

    if tf["vol_last"] and tf["vol_sma20"]:
        if tf["vol_last"] > tf["vol_sma20"] * 1.10:
            if score > 0:
                score += 1
                notes.append("volume confirm up")
            elif score < 0:
                score -= 1
                notes.append("volume confirm down")

    if score >= 3:
        verdict = "UYGUN"
    elif score <= -3:
        verdict = "UYGUN DEGIL"
    else:
        verdict = "TEMKINLI"

    return {
        "tf": tf_name,
        "score": score,
        "verdict": verdict,
        "notes": notes[:5]
    }

# =========================================================
# WHALE ANALYSIS
# =========================================================
def get_trade_pressure(symbol: str):
    try:
        trades = get_agg_trades(symbol, AGGTRADE_LIMIT)
        if not isinstance(trades, list) or not trades:
            return {
                "dominant": "NEUTRAL",
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "large_buy_count": 0,
                "large_sell_count": 0,
                "dominance_pct": 50.0
            }

        buy_notional = 0.0
        sell_notional = 0.0
        large_buy_count = 0
        large_sell_count = 0

        for t in trades:
            price = safe_float(t.get("p"))
            qty = safe_float(t.get("q"))
            is_buyer_maker = bool(t.get("m"))  # True ise satıcı agresif
            if price is None or qty is None:
                continue
            notional = price * qty

            if is_buyer_maker:
                sell_notional += notional
                if notional >= LARGE_TRADE_NOTIONAL_USDT:
                    large_sell_count += 1
            else:
                buy_notional += notional
                if notional >= LARGE_TRADE_NOTIONAL_USDT:
                    large_buy_count += 1

        total = buy_notional + sell_notional
        dominance_pct = (max(buy_notional, sell_notional) / total * 100.0) if total > 0 else 50.0

        if total <= 0:
            dominant = "NEUTRAL"
        elif buy_notional > sell_notional and dominance_pct >= TRADE_PRESSURE_DOMINANCE_PCT:
            dominant = "LONG"
        elif sell_notional > buy_notional and dominance_pct >= TRADE_PRESSURE_DOMINANCE_PCT:
            dominant = "SHORT"
        else:
            dominant = "NEUTRAL"

        return {
            "dominant": dominant,
            "buy_notional": round(buy_notional, 2),
            "sell_notional": round(sell_notional, 2),
            "large_buy_count": large_buy_count,
            "large_sell_count": large_sell_count,
            "dominance_pct": round(dominance_pct, 2)
        }
    except Exception as e:
        return {
            "dominant": "NEUTRAL",
            "buy_notional": 0.0,
            "sell_notional": 0.0,
            "large_buy_count": 0,
            "large_sell_count": 0,
            "dominance_pct": 50.0,
            "error": str(e)
        }

def get_whale_bias(symbol: str):
    result = {
        "bias": "NEUTRAL",
        "score": 0.0,
        "reason": "no whale data"
    }

    try:
        flow = get_trade_pressure(symbol)
        score = 0.0
        reasons = []

        if flow["dominant"] == "LONG":
            score += 1.8
            reasons.append(f"trade pressure long {flow['dominance_pct']}%")
            if flow["large_buy_count"] > flow["large_sell_count"]:
                score += 0.8
                reasons.append(f"large buys {flow['large_buy_count']}")
        elif flow["dominant"] == "SHORT":
            score -= 1.8
            reasons.append(f"trade pressure short {flow['dominance_pct']}%")
            if flow["large_sell_count"] > flow["large_buy_count"]:
                score -= 0.8
                reasons.append(f"large sells {flow['large_sell_count']}")

        if MARKET_TYPE == "futures":
            oi_hist = get_open_interest_hist(symbol, "5m", 12)
            if len(oi_hist) >= 2:
                prev_oi = safe_float(oi_hist[-2].get("sumOpenInterest"))
                last_oi = safe_float(oi_hist[-1].get("sumOpenInterest"))
                oi_chg = pct_change(prev_oi, last_oi)

                mark_price = get_mark_price(symbol)
                kl_5m = analyze_timeframe(get_klines(symbol, "5m", 120))
                price_chg_5m = pct_change(kl_5m["prev_close"], kl_5m["close"])

                if oi_chg >= OI_ALERT_THRESHOLD_PCT and price_chg_5m > 0:
                    score += 1.6
                    reasons.append(f"oi up {round(oi_chg,2)}% + price up")
                elif oi_chg >= OI_ALERT_THRESHOLD_PCT and price_chg_5m < 0:
                    score -= 1.6
                    reasons.append(f"oi up {round(oi_chg,2)}% + price down")

                if abs(oi_chg) >= OI_ALERT_THRESHOLD_PCT:
                    reasons.append(f"oi delta {round(oi_chg,2)}% @ {fmt_price(mark_price)}")

        if score >= 2.0:
            result["bias"] = "LONG"
        elif score <= -2.0:
            result["bias"] = "SHORT"
        else:
            result["bias"] = "NEUTRAL"

        result["score"] = round(abs(score), 2)
        result["reason"] = " | ".join(reasons[:8]) if reasons else "flow mixed"

        return result
    except Exception as e:
        result["reason"] = f"whale fail-open: {e}"
        return result

def maybe_send_whale_alert(state):
    if not ENABLE_WHALE_ALERTS:
        return

    if minutes_since(state.get("last_whale_alert_ts")) < WHALE_ALERT_COOLDOWN_MINUTES:
        return

    whale = get_whale_bias(SYMBOL)
    if whale["bias"] not in ("LONG", "SHORT"):
        return

    emoji = "🐋🟢" if whale["bias"] == "LONG" else "🐋🔴"
    msg = (
        f"{emoji} BALINA BASKI UYARISI\n"
        f"Sembol: {SYMBOL}\n"
        f"Yön: {whale['bias']}\n"
        f"Güç: {whale['score']}\n"
        f"Detay: {whale['reason']}\n"
        f"Zaman: {now_local_str()}"
    )
    if tg_send(msg):
        state["last_whale_alert_ts"] = utc_ts()
        log(f"Whale alert gönderildi: {whale['bias']}")

# =========================================================
# NEWS / EVENT
# =========================================================
def parse_rss_items(feed_url: str):
    items = []
    try:
        xml_text = http_get_text(feed_url)
        root = ET.fromstring(xml_text)

        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()

            items.append({
                "title": title,
                "link": link,
                "pub_date": pub_date
            })

        # Atom fallback
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns):
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.attrib.get("href", "").strip() if link_el is not None else ""
            pub_date = (
                entry.findtext("atom:updated", default="", namespaces=ns)
                or entry.findtext("atom:published", default="", namespaces=ns)
                or ""
            ).strip()

            if title:
                items.append({
                    "title": title,
                    "link": link,
                    "pub_date": pub_date
                })

    except Exception as e:
        log(f"RSS parse hata: {feed_url} | {e}")

    return items

def news_item_key(item):
    return normalize_text(f"{item.get('title','')}|{item.get('link','')}")

def score_news_title(title: str):
    txt = normalize_text(title)
    score = 0
    hits = []

    for kw in NEWS_KEYWORDS_HIGH:
        if kw in txt:
            score += 3
            hits.append(kw)

    for kw in NEWS_KEYWORDS_CRYPTO:
        if kw in txt:
            score += 1
            hits.append(kw)

    if SYMBOL.replace("USDT", "").lower()[:3] in txt or "ethereum" in txt or "eth" in txt:
        score += 2
        hits.append("eth")

    if "bitcoin" in txt or "btc" in txt:
        score += 2
        hits.append("btc")

    if score >= 8:
        level = "HIGH"
    elif score >= 4:
        level = "MEDIUM"
    else:
        level = "LOW"

    return level, score, list(dict.fromkeys(hits))

def detect_news_risk(state):
    if not ENABLE_NEWS_ALERTS:
        return None

    fresh_candidates = []
    known_keys = set(state.get("sent_news_keys", []))

    for feed in NEWS_FEEDS:
        items = parse_rss_items(feed)
        for item in items[:15]:
            title = item.get("title", "")
            if not title:
                continue

            level, score, hits = score_news_title(title)
            if level == "LOW":
                continue

            key = news_item_key(item)
            if key in known_keys:
                continue

            fresh_candidates.append({
                "title": title,
                "link": item.get("link", ""),
                "level": level,
                "score": score,
                "hits": hits,
                "key": key
            })

    if not fresh_candidates:
        return None

    fresh_candidates.sort(key=lambda x: x["score"], reverse=True)
    return fresh_candidates[0]

def maybe_send_news_alert(state):
    if not ENABLE_NEWS_ALERTS:
        return

    if minutes_since(state.get("last_news_alert_ts")) < NEWS_ALERT_COOLDOWN_MINUTES:
        return

    item = detect_news_risk(state)
    if not item:
        return

    emoji = "📰🔴" if item["level"] == "HIGH" else "📰🟠"
    msg = (
        f"{emoji} KRIPTO HABER RISK UYARISI\n"
        f"Seviye: {item['level']}\n"
        f"Başlık: {item['title']}\n"
        f"Anahtarlar: {', '.join(item['hits'][:8])}\n"
        f"Link: {item['link'] or '-'}\n"
        f"Yorum: Haber bazlı volatilite artabilir, işleme girmeden tekrar kontrol et.\n"
        f"Zaman: {now_local_str()}"
    )

    if tg_send(msg):
        state["last_news_alert_ts"] = utc_ts()
        keys = state.get("sent_news_keys", [])
        keys.append(item["key"])
        state["sent_news_keys"] = keys[-100:]
        log(f"News alert gönderildi: {item['title']}")

def maybe_send_event_prealerts(state):
    now = now_utc()
    sent_keys = set(state.get("sent_event_keys", []))
    new_keys = list(sent_keys)

    for event in UPCOMING_EVENTS:
        try:
            event_dt = datetime.strptime(event["time_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            delta_hours = (event_dt - now).total_seconds() / 3600.0

            if delta_hours < 0:
                continue

            for h in EVENT_PREALERT_HOURS:
                key = f"{event['name']}|{event['time_utc']}|{h}"
                if key in sent_keys:
                    continue

                if 0 <= delta_hours <= h + 0.12 and delta_hours >= h - 0.35:
                    msg = (
                        f"⏰ MAKRO / EVENT RISK UYARISI\n"
                        f"Etkinlik: {event['name']}\n"
                        f"Etki: {event['impact']}\n"
                        f"Kalan süre: ~{h} saat\n"
                        f"Event zamanı (UTC): {event['time_utc']}\n"
                        f"Yorum: Event öncesi spread ve volatilite artabilir.\n"
                        f"Zaman: {now_local_str()}"
                    )
                    if tg_send(msg):
                        new_keys.append(key)
        except Exception as e:
            log(f"Event prealert hata: {e}")

    state["sent_event_keys"] = new_keys[-200:]

# =========================================================
# SIGNAL / MESSAGE HELPERS
# =========================================================
def build_signal_payload(direction, entry, sl, tp1, tp2, tp3, score, strategy_tag, reason, extra=None):
    return {
        "direction": direction.upper(),
        "entry": round(float(entry), 4),
        "sl": round(float(sl), 4),
        "tp1": round(float(tp1), 4),
        "tp2": round(float(tp2), 4),
        "tp3": round(float(tp3), 4),
        "score": round(float(score), 2),
        "strategy_tag": strategy_tag,
        "reason": reason,
        "extra": extra or {},
        "created_ts": utc_ts(),
        "updated_ts": utc_ts(),
        "status": "OPEN"
    }

def is_same_direction(a, b):
    return bool(a and b and a.get("direction") == b.get("direction"))

def is_opposite_direction(a, b):
    return bool(a and b and a.get("direction") != b.get("direction"))

def entry_distance_pct(sig_a, sig_b):
    if not sig_a or not sig_b:
        return 999.0
    return pct_diff(sig_a.get("entry"), sig_b.get("entry"))

def build_ai_commentary(signal):
    extra = signal.get("extra", {})
    btc = extra.get("btc", {})
    whale = extra.get("whale", {})
    swing_1h = extra.get("swing_1h", {})
    swing_2h = extra.get("swing_2h", {})
    swing_4h = extra.get("swing_4h", {})
    news_risk = extra.get("news_risk", "UNKNOWN")

    direction = signal.get("direction", "UNKNOWN")
    score = signal.get("score", 0)

    parts = []

    if direction == "LONG":
        parts.append("Teknik yapı ağırlıklı olarak yukarı yönlü.")
    else:
        parts.append("Teknik yapı ağırlıklı olarak aşağı yönlü.")

    if btc.get("bias") == direction:
        parts.append("BTC ana rejimi işlemi destekliyor.")
    elif btc.get("bias") in ("LONG", "SHORT") and btc.get("bias") != direction:
        parts.append("BTC rejimi bu işleme tam destek vermiyor, temkin gerekli.")
    else:
        parts.append("BTC tarafı nötre yakın, ana destek zayıf.")

    if whale.get("bias") == direction:
        parts.append("Balina akışı aynı yöne baskı kuruyor.")
    elif whale.get("bias") in ("LONG", "SHORT") and whale.get("bias") != direction:
        parts.append("Balina akışı ters yönde, fake hareket riski var.")

    swing_bits = []
    for s in [swing_1h, swing_2h, swing_4h]:
        if s:
            swing_bits.append(f"{s['tf']}:{s['verdict']}")

    if swing_bits:
        parts.append("Swing görünümü " + ", ".join(swing_bits) + ".")

    if news_risk == "HIGH":
        parts.append("Haber riski yüksek; stop disiplini şart.")
    elif news_risk == "MEDIUM":
        parts.append("Haber tarafı orta riskli; girişte teyit önemli.")

    if score >= 12:
        parts.append("Setup güçlü.")
    elif score >= 10:
        parts.append("Setup alınabilir ama seçici olunmalı.")
    else:
        parts.append("Setup mevcut fakat agresif değil.")

    return " ".join(parts)

def format_signal_message(sig, current_price):
    emoji = "🟢" if sig["direction"] == "LONG" else "🔴"
    extra = sig.get("extra", {})
    btc = extra.get("btc", {})
    whale = extra.get("whale", {})
    swing_1h = extra.get("swing_1h", {})
    swing_2h = extra.get("swing_2h", {})
    swing_4h = extra.get("swing_4h", {})
    ai = extra.get("ai_commentary", "-")
    news_risk = extra.get("news_risk", "UNKNOWN")

    return (
        f"{emoji} {SYMBOL} {sig['direction']} SINYAL\n"
        f"Fiyat: {fmt_price(current_price)}\n"
        f"Entry: {fmt_price(sig['entry'])}\n"
        f"SL: {fmt_price(sig['sl'])}\n"
        f"TP1: {fmt_price(sig['tp1'])}\n"
        f"TP2: {fmt_price(sig['tp2'])}\n"
        f"TP3: {fmt_price(sig['tp3'])}\n"
        f"Score: {sig['score']}\n"
        f"Setup: {sig['strategy_tag']}\n"
        f"BTC Bias: {btc.get('bias','NEUTRAL')} | {btc.get('reason','-')}\n"
        f"Whale: {whale.get('bias','NEUTRAL')} | {whale.get('reason','-')}\n"
        f"News Risk: {news_risk}\n"
        f"1H Swing: {swing_1h.get('verdict','-')}\n"
        f"2H Swing: {swing_2h.get('verdict','-')}\n"
        f"4H Swing: {swing_4h.get('verdict','-')}\n"
        f"AI Yorum: {ai}\n"
        f"Neden: {sig['reason']}\n"
        f"Zaman: {now_local_str()}"
    )

def format_close_message(sig, close_reason, close_price=None):
    direction_emoji = "🟢" if sig["direction"] == "LONG" else "🔴"
    reason_map = {
        "SL_HIT": "Stop oldu",
        "TP1_HIT": "TP1 görüldü",
        "TP2_HIT": "TP2 görüldü",
        "TP3_HIT": "TP3 görüldü",
        "TIMEOUT": "Süre doldu",
        "MAX_ACTIVE_AGE_EXCEEDED": "Maks aktif süre doldu",
        "REVERSED_BY_STRONGER_SIGNAL": "Daha güçlü ters sinyal geldi",
    }
    reason_text = reason_map.get(close_reason, close_reason)

    body = (
        f"✅ {SYMBOL} SINYAL KAPANDI\n"
        f"Yön: {direction_emoji} {sig['direction']}\n"
        f"Entry: {fmt_price(sig.get('entry'))}\n"
        f"SL: {fmt_price(sig.get('sl'))}\n"
        f"TP1: {fmt_price(sig.get('tp1'))}\n"
        f"TP2: {fmt_price(sig.get('tp2'))}\n"
        f"TP3: {fmt_price(sig.get('tp3'))}\n"
        f"Kapanış nedeni: {reason_text}\n"
    )

    if close_price is not None:
        body += f"Kapanış fiyatı: {fmt_price(close_price)}\n"

    body += f"İlk score: {sig.get('score')}\nZaman: {now_local_str()}"
    return body

# =========================================================
# ACTIVE SIGNAL MGMT
# =========================================================
def close_active_signal(state, close_reason, current_price=None, notify_telegram=True):
    active = state.get("active_signal")
    if not active:
        return

    active["status"] = "CLOSED"
    active["close_reason"] = close_reason
    active["closed_ts"] = utc_ts()

    if current_price is not None:
        active["close_price"] = round(float(current_price), 4)

    if notify_telegram:
        tg_send(format_close_message(active, close_reason, current_price))

    state["signal_history"].append(active)
    state["signal_history"] = state["signal_history"][-300:]
    state["active_signal"] = None
    log(f"Aktif sinyal kapatildi: {close_reason}")

def is_tp1_hit(signal, current_price):
    if not signal:
        return False
    direction = signal["direction"]
    tp1 = safe_float(signal.get("tp1"))
    cp = safe_float(current_price)
    if tp1 is None or cp is None:
        return False
    return cp >= tp1 if direction == "LONG" else cp <= tp1

def is_tp2_hit(signal, current_price):
    if not signal:
        return False
    direction = signal["direction"]
    tp2 = safe_float(signal.get("tp2"))
    cp = safe_float(current_price)
    if tp2 is None or cp is None:
        return False
    return cp >= tp2 if direction == "LONG" else cp <= tp2

def is_tp3_hit(signal, current_price):
    if not signal:
        return False
    direction = signal["direction"]
    tp3 = safe_float(signal.get("tp3"))
    cp = safe_float(current_price)
    if tp3 is None or cp is None:
        return False
    return cp >= tp3 if direction == "LONG" else cp <= tp3

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

def refresh_active_signal_if_needed(state, current_price):
    active = state.get("active_signal")
    if not active:
        return

    if is_sl_hit(active, current_price):
        close_active_signal(state, "SL_HIT", current_price, notify_telegram=True)
        return

    if is_tp3_hit(active, current_price):
        close_active_signal(state, "TP3_HIT", current_price, notify_telegram=True)
        return

    if is_tp2_hit(active, current_price):
        close_active_signal(state, "TP2_HIT", current_price, notify_telegram=True)
        return

    if is_tp1_hit(active, current_price):
        close_active_signal(state, "TP1_HIT", current_price, notify_telegram=True)
        return

    if is_expired(active):
        close_active_signal(state, "TIMEOUT", current_price, notify_telegram=True)
        return

def recent_gap_blocked(state, new_signal):
    last_signal = state.get("last_signal")
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

def should_send_signal(state, new_signal, current_price):
    refresh_active_signal_if_needed(state, current_price)
    active = state.get("active_signal")

    if not active:
        if recent_gap_blocked(state, new_signal):
            return False, "RECENT_DUPLICATE_GAP"
        return True, "NO_ACTIVE_SIGNAL"

    active_age = minutes_since(active.get("created_ts"))
    if active_age >= MAX_ACTIVE_SIGNAL_AGE_MINUTES:
        close_active_signal(state, "MAX_ACTIVE_AGE_EXCEEDED", current_price, notify_telegram=True)
        if recent_gap_blocked(state, new_signal):
            return False, "RECENT_DUPLICATE_GAP"
        return True, "ACTIVE_TOO_OLD"

    if is_opposite_direction(active, new_signal):
        active_score = safe_float(active.get("score"), 0.0)
        new_score = safe_float(new_signal.get("score"), 0.0)

        if new_score >= active_score + REVERSE_SIGNAL_STRENGTH_BONUS:
            close_active_signal(state, "REVERSED_BY_STRONGER_SIGNAL", current_price, notify_telegram=True)
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

def register_sent_signal(state, sent_signal):
    sent_signal["created_ts"] = utc_ts()
    sent_signal["updated_ts"] = utc_ts()
    sent_signal["status"] = "OPEN"
    state["active_signal"] = sent_signal
    state["last_signal"] = sent_signal

# =========================================================
# CORE SIGNAL ENGINE
# =========================================================
def build_trade_signal():
    candles_15m = get_klines(SYMBOL, "15m", 200)
    candles_5m = get_klines(SYMBOL, "5m", 200)
    candles_1h = get_klines(SYMBOL, "1h", 200)
    candles_2h = get_klines(SYMBOL, "2h", 200)
    candles_4h = get_klines(SYMBOL, "4h", 200)

    tf15 = analyze_timeframe(candles_15m)
    tf5 = analyze_timeframe(candles_5m)
    tf1h = analyze_timeframe(candles_1h)
    tf2h = analyze_timeframe(candles_2h)
    tf4h = analyze_timeframe(candles_4h)

    current_price = tf5["close"]
    btc = get_btc_regime()
    whale = get_whale_bias(SYMBOL)

    swing_1h = classify_swing("1H", tf1h)
    swing_2h = classify_swing("2H", tf2h)
    swing_4h = classify_swing("4H", tf4h)

    long_score = 0.0
    short_score = 0.0
    reasons_long = []
    reasons_short = []

    # -----------------------------------------------------
    # 15m trend
    # -----------------------------------------------------
    if tf15["ema9"] and tf15["ema21"] and tf15["ema50"]:
        if tf15["ema9"] > tf15["ema21"] > tf15["ema50"]:
            long_score += 3.0
            reasons_long.append("15m ema bull stack")
        if tf15["ema9"] < tf15["ema21"] < tf15["ema50"]:
            short_score += 3.0
            reasons_short.append("15m ema bear stack")

    if tf15["close"] > (tf15["ema21"] or 0):
        long_score += 1.5
        reasons_long.append("15m close > ema21")
    else:
        short_score += 1.5
        reasons_short.append("15m close < ema21")

    if tf15["rsi14"] is not None:
        if 52 <= tf15["rsi14"] <= 72:
            long_score += 1.5
            reasons_long.append(f"15m rsi strong {round(tf15['rsi14'], 2)}")
        if 28 <= tf15["rsi14"] <= 48:
            short_score += 1.5
            reasons_short.append(f"15m rsi weak {round(tf15['rsi14'], 2)}")

    if tf15["macd_hist"] is not None:
        if tf15["macd_hist"] > 0:
            long_score += 1.25
            reasons_long.append("15m macd hist > 0")
        if tf15["macd_hist"] < 0:
            short_score += 1.25
            reasons_short.append("15m macd hist < 0")

    # -----------------------------------------------------
    # 1h stronger filter
    # -----------------------------------------------------
    if tf1h["ema21"] and tf1h["ema50"]:
        if tf1h["close"] > tf1h["ema21"] and tf1h["ema21"] > tf1h["ema50"]:
            long_score += 1.5
            reasons_long.append("1h strong long trend")
        if tf1h["close"] < tf1h["ema21"] and tf1h["ema21"] < tf1h["ema50"]:
            short_score += 1.5
            reasons_short.append("1h strong short trend")

    if tf1h["rsi14"] is not None:
        if tf1h["rsi14"] >= 52:
            long_score += 0.5
            reasons_long.append(f"1h rsi bullish {round(tf1h['rsi14'], 2)}")
        elif tf1h["rsi14"] <= 48:
            short_score += 0.5
            reasons_short.append(f"1h rsi bearish {round(tf1h['rsi14'], 2)}")

    # -----------------------------------------------------
    # 2h / 4h swing alignment
    # -----------------------------------------------------
    if swing_2h["verdict"] == "UYGUN":
        long_score += 0.75
        reasons_long.append("2h swing aligned")
    elif swing_2h["verdict"] == "UYGUN DEGIL":
        short_score += 0.75
        reasons_short.append("2h swing aligned")

    if swing_4h["verdict"] == "UYGUN":
        long_score += 1.1
        reasons_long.append("4h swing aligned")
    elif swing_4h["verdict"] == "UYGUN DEGIL":
        short_score += 1.1
        reasons_short.append("4h swing aligned")

    # -----------------------------------------------------
    # BTC directional filter
    # -----------------------------------------------------
    if btc["block_new_signals"]:
        return None, current_price, "BTC_VOLATILITY_BLOCK"

    if btc["bias"] == "LONG":
        long_score += 1.2
        short_score -= 0.8
        reasons_long.append("btc supports long")
        reasons_short.append("btc against short")
    elif btc["bias"] == "SHORT":
        short_score += 1.2
        long_score -= 0.8
        reasons_short.append("btc supports short")
        reasons_long.append("btc against long")
    else:
        long_score -= 0.2
        short_score -= 0.2

    # -----------------------------------------------------
    # Whale directional effect
    # -----------------------------------------------------
    if whale["bias"] == "LONG":
        long_score += 1.0
        reasons_long.append("whale flow long")
    elif whale["bias"] == "SHORT":
        short_score += 1.0
        reasons_short.append("whale flow short")

    # -----------------------------------------------------
    # 5m entry
    # -----------------------------------------------------
    long_entry_ok = False
    short_entry_ok = False

    if tf5["ema9"] and tf5["ema21"] and tf5["ema50"]:
        if (
            current_price > tf5["ema21"] and
            tf5["ema9"] >= tf5["ema21"] and
            tf5["ema21"] >= tf5["ema50"] and
            tf5["rsi14"] is not None and 48 <= tf5["rsi14"] <= 68 and
            tf5["macd"] is not None and tf5["macd_signal"] is not None and
            tf5["macd"] >= tf5["macd_signal"]
        ):
            long_score += 3.0
            reasons_long.append("5m entry aligned")
            long_entry_ok = True

        if (
            current_price < tf5["ema21"] and
            tf5["ema9"] <= tf5["ema21"] and
            tf5["ema21"] <= tf5["ema50"] and
            tf5["rsi14"] is not None and 32 <= tf5["rsi14"] <= 52 and
            tf5["macd"] is not None and tf5["macd_signal"] is not None and
            tf5["macd"] <= tf5["macd_signal"]
        ):
            short_score += 3.0
            reasons_short.append("5m entry aligned")
            short_entry_ok = True

    # -----------------------------------------------------
    # Volume confirm
    # -----------------------------------------------------
    if tf5["vol_last"] and tf5["vol_sma20"]:
        if tf5["vol_last"] > tf5["vol_sma20"] * 1.08:
            if long_entry_ok:
                long_score += 1.0
                reasons_long.append("5m vol strong confirm")
            if short_entry_ok:
                short_score += 1.0
                reasons_short.append("5m vol strong confirm")

    atr5 = tf5["atr14"]
    if atr5 is None or atr5 <= 0:
        return None, current_price, "ATR_UNAVAILABLE"

    # Haber riski notu
    news_item = detect_news_risk(load_state())
    if news_item:
        if news_item["level"] == "HIGH":
            long_score -= 0.75
            short_score -= 0.75
            news_risk = "HIGH"
        else:
            long_score -= 0.35
            short_score -= 0.35
            news_risk = "MEDIUM"
    else:
        news_risk = "LOW"

    candidates = []

    # -----------------------------------------------------
    # LONG candidate
    # -----------------------------------------------------
    if long_entry_ok and long_score >= LONG_SCORE_THRESHOLD:
        entry = current_price
        sl = entry - (atr5 * ATR_SL_MULTIPLIER)
        tp1 = entry + (atr5 * ATR_TP1_MULTIPLIER)
        tp2 = entry + (atr5 * ATR_TP2_MULTIPLIER)
        tp3 = entry + (atr5 * ATR_TP3_MULTIPLIER)

        risk = entry - sl
        reward = tp1 - entry
        rr = reward / risk if risk > 0 else 0

        if rr >= MIN_RR:
            extra = {
                "btc": btc,
                "whale": whale,
                "swing_1h": swing_1h,
                "swing_2h": swing_2h,
                "swing_4h": swing_4h,
                "news_risk": news_risk
            }
            payload = build_signal_payload(
                direction="LONG",
                entry=entry,
                sl=sl,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                score=long_score,
                strategy_tag="ETH_15M_TREND_5M_ENTRY_PRO_PLUS_AI",
                reason=" | ".join(reasons_long[:10]) + f" | BTC:{btc['bias']} | Whale:{whale['bias']} | RR:{round(rr, 2)}",
                extra=extra
            )
            payload["extra"]["ai_commentary"] = build_ai_commentary(payload)
            candidates.append(payload)

    # -----------------------------------------------------
    # SHORT candidate
    # -----------------------------------------------------
    if short_entry_ok and short_score >= SHORT_SCORE_THRESHOLD:
        entry = current_price
        sl = entry + (atr5 * ATR_SL_MULTIPLIER)
        tp1 = entry - (atr5 * ATR_TP1_MULTIPLIER)
        tp2 = entry - (atr5 * ATR_TP2_MULTIPLIER)
        tp3 = entry - (atr5 * ATR_TP3_MULTIPLIER)

        risk = sl - entry
        reward = entry - tp1
        rr = reward / risk if risk > 0 else 0

        if rr >= MIN_RR:
            extra = {
                "btc": btc,
                "whale": whale,
                "swing_1h": swing_1h,
                "swing_2h": swing_2h,
                "swing_4h": swing_4h,
                "news_risk": news_risk
            }
            payload = build_signal_payload(
                direction="SHORT",
                entry=entry,
                sl=sl,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                score=short_score,
                strategy_tag="ETH_15M_TREND_5M_ENTRY_PRO_PLUS_AI",
                reason=" | ".join(reasons_short[:10]) + f" | BTC:{btc['bias']} | Whale:{whale['bias']} | RR:{round(rr, 2)}",
                extra=extra
            )
            payload["extra"]["ai_commentary"] = build_ai_commentary(payload)
            candidates.append(payload)

    if not candidates:
        return None, current_price, f"NO_VALID_SIGNAL | long={round(long_score,2)} short={round(short_score,2)}"

    candidates.sort(key=lambda x: x["score"], reverse=True)
    best = candidates[0]
    return best, current_price, f"SIGNAL_READY | long={round(long_score,2)} short={round(short_score,2)}"

# =========================================================
# MAIN LOOP
# =========================================================
def run_once():
    state = load_state()

    try:
        maybe_send_event_prealerts(state)
        maybe_send_news_alert(state)
        maybe_send_whale_alert(state)

        signal, current_price, info = build_trade_signal()
        refresh_active_signal_if_needed(state, current_price)

        if signal:
            can_send, reason_code = should_send_signal(
                state=state,
                new_signal=signal,
                current_price=current_price
            )

            if can_send:
                message = format_signal_message(signal, current_price)
                sent = tg_send(message)
                if sent:
                    register_sent_signal(state, signal)
                    log(
                        f"YENI SINYAL GONDERILDI | {signal['direction']} | "
                        f"entry={signal['entry']} sl={signal['sl']} tp1={signal['tp1']} tp2={signal['tp2']} tp3={signal['tp3']} "
                        f"score={signal['score']} | {reason_code}"
                    )
                else:
                    log("Sinyal bulundu ama Telegram gönderimi başarısız.")
            else:
                log(
                    f"Sinyal var ama gönderilmedi: {reason_code} | "
                    f"Yön={signal['direction']} Entry={signal['entry']} Score={signal['score']}"
                )
        else:
            log(f"Yeni valid sinyal yok. {info}")

    except Exception as e:
        log(f"Analiz exception: {e}")
        traceback.print_exc()

    try:
        save_state(state)
    except Exception as e:
        log(f"State save exception: {e}")

def send_startup_message():
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        tg_send(
            f"🤖 ETH SuperBot PRO+ AI başladı.\n"
            f"Zaman: {now_local_str()}\n"
            f"Sembol: {SYMBOL}\n"
            f"Market: {MARKET_TYPE}\n"
            f"Özellikler: BTC regime | Whale alerts | AI yorum | 1H-2H-4H swing | News risk"
        )
        log("Başlangıç mesajı gönderildi.")
    else:
        log("Başlangıç mesajı gönderilemedi: Telegram ENV eksik.")

def main():
    log("Bot başlıyor...")
    send_startup_message()

    while True:
        log("Analiz yapılıyor...")
        run_once()
        time.sleep(CHECK_EVERY_SECONDS)

if __name__ == "__main__":
    main()
