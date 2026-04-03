# -*- coding: utf-8 -*-
"""
COINRADAR ELIT++ - MEXC FUTURES SIGNAL BOT
Tek dosya / tek parça / profesyonel Telegram formatlı
Amaç:
- 200-250 coin taramak
- çöp coinleri mümkün olduğunca elemek
- kaliteli 3 sinyal seçmek
- LONG / SHORT sinyal üretmek
- mevcut yapıdaki iyi parçaları koruyup geliştirmek

Kurulum:
    pip install requests pandas

Çalıştırma:
    python coinradar_elitpp.py
"""

import os
import time
import html
import json
import math
import traceback
from typing import List, Optional, Tuple

import requests
import pandas as pd


# =========================================================
# TELEGRAM
# Öncelik:
# 1) Environment variable
# 2) Aşağıdaki sabitler
# =========================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "BURAYA_TOKEN").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID").strip()

# =========================================================
# GENEL AYARLAR
# =========================================================
MEXC_BASE = "https://contract.mexc.com"
HTTP_TIMEOUT = 15
CHECK_EVERY_SECONDS = 60

STATE_FILE = "coinradar_elitpp_state.json"
SEND_STARTUP_MESSAGE = True
SEND_HEARTBEAT_IF_NO_SIGNAL = True
HEARTBEAT_EVERY_MINUTES = 30

# Daha geniş ama kontrollü tarama
MAX_SYMBOLS_TO_SCAN = 250
TOP_N_SIGNALS = 3
SIGNAL_COOLDOWN_MINUTES = 30

# Kline
TF_ENTRY = "Min5"
TF_CONFIRM = "Min15"
TF_REGIME = "Min60"
KLINE_LIMIT = 260

# =========================================================
# MARKET / EVREN FİLTRESİ
# =========================================================
REQUIRE_USDT_PERP = True

# 200-250 coin tarayabilsin diye gevşetildi ama yine kaliteli kalsın
MIN_AMOUNT24_USDT = 2_500_000
MIN_HOLDVOL = 40_000
MAX_SPREAD_PCT = 0.35
MAX_ABS_FUNDING = 0.0020
MAX_24H_PUMP_PCT = 18.0
MIN_PRICE = 0.00001

# Çok riskli / anlamsız / aşırı spekülatif sözleşmeleri ele
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

# Sinyal eşikleri
MIN_RSI_LONG = 51
MAX_RSI_SHORT = 49
MIN_ADX = 15
MIN_RR_TO_TP2 = 1.25

MAX_DISTANCE_FROM_EMA20_ATR = 2.4
MAX_LAST_CANDLE_RANGE_ATR = 2.8
MIN_BREAKOUT_BODY_ATR = 0.12

SL_ATR_MULT = 1.10
TP1_ATR_MULT = 1.10
TP2_ATR_MULT = 1.90
TP3_ATR_MULT = 2.90

# Volume & yapı
VOLUME_SURGE_MIN = 1.15
BREAKOUT_LOOKBACK = 20

# BTC bias
USE_BTC_REGIME_FILTER = True
BTC_SYMBOL = "BTC_USDT"

# =========================================================
# REQUEST SESSION
# =========================================================
session = requests.Session()
session.headers.update({
    "User-Agent": "CoinRadar-ElitPP/2.0"
})


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
        return {"last_sent": {}, "last_heartbeat": 0}


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
        print("Telegram response:", r.text[:500])
        return False
    except Exception as e:
        print("Telegram exception:", e)
        return False


def telegram_test() -> bool:
    return telegram_send_html("✅ <b>COINRADAR ELIT++ TEST</b>\n\nTelegram bağlantısı başarılı.")


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


def get_funding_rate(symbol: str) -> Optional[float]:
    try:
        data = mexc_get(f"/api/v1/contract/funding_rate/{symbol}")
        item = data.get("data", {}) or {}
        return safe_float(item.get("fundingRate"), None)
    except Exception:
        return None


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
# MARKET FİLTRESİ
# =========================================================
def is_blacklisted(symbol: str) -> bool:
    s = symbol.upper()
    for k in BLACKLIST_KEYWORDS:
        if k in s:
            return True
    return False


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
            math.log10(max(amount24, 1.0)) * 0.70 +
            math.log10(max(hold_vol, 1.0)) * 0.30
        )

        rows.append({
            "symbol": symbol,
            "last_price": last_price,
            "amount24": amount24,
            "hold_vol": hold_vol,
            "spread_pct": spread_pct,
            "rise_fall_pct": rise_fall_rate,
            "liquidity_score": liquidity_score
        })

    rows.sort(
        key=lambda x: (x["liquidity_score"], x["amount24"], x["hold_vol"]),
        reverse=True
    )
    return rows[:MAX_SYMBOLS_TO_SCAN]


# =========================================================
# BTC REGIME
# =========================================================
def get_btc_regime() -> str:
    try:
        df = get_kline(BTC_SYMBOL, TF_CONFIRM, 240)
        if len(df) < 210:
            return "NEUTRAL"

        df = enrich_indicators(df)
        last = df.iloc[-1]

        bullish = (
            last["close"] > last["ema20"] > last["ema50"] > last["ema200"]
            and last["rsi"] >= 52
            and last["adx"] >= 16
        )
        bearish = (
            last["close"] < last["ema20"] < last["ema50"] < last["ema200"]
            and last["rsi"] <= 48
            and last["adx"] >= 16
        )

        if bullish:
            return "LONG_ONLY"
        if bearish:
            return "SHORT_ONLY"
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"


# =========================================================
# SİNYAL MOTORU
# =========================================================
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


def breakout_flags(df: pd.DataFrame) -> Tuple[bool, bool]:
    if len(df) < BREAKOUT_LOOKBACK + 2:
        return False, False

    prev_high = df["high"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].max()
    prev_low = df["low"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].min()
    last = df.iloc[-1]

    breakout_long = (
        last["close"] > prev_high
        and last["body"] >= last["atr"] * MIN_BREAKOUT_BODY_ATR
    )
    breakdown_short = (
        last["close"] < prev_low
        and last["body"] >= last["atr"] * MIN_BREAKOUT_BODY_ATR
    )
    return breakout_long, breakdown_short


def ema_reclaim_long(last5, prev5) -> bool:
    return (
        last5["close"] > last5["ema20"]
        and prev5["close"] <= prev5["ema20"]
        and last5["close"] > last5["open"]
    )


def ema_reject_short(last5, prev5) -> bool:
    return (
        last5["close"] < last5["ema20"]
        and prev5["close"] >= prev5["ema20"]
        and last5["close"] < last5["open"]
    )


def wick_ok_for_long(last5) -> bool:
    body = max(last5["body"], 1e-12)
    return (last5["upper_wick"] / body) <= 3.5


def wick_ok_for_short(last5) -> bool:
    body = max(last5["body"], 1e-12)
    return (last5["lower_wick"] / body) <= 3.5


def trend_score_long(last15) -> float:
    score = 0.0
    if last15["close"] > last15["ema20"]:
        score += 1.0
    if last15["ema20"] > last15["ema50"]:
        score += 1.0
    if last15["ema50"] > last15["ema200"]:
        score += 1.0
    return score


def trend_score_short(last15) -> float:
    score = 0.0
    if last15["close"] < last15["ema20"]:
        score += 1.0
    if last15["ema20"] < last15["ema50"]:
        score += 1.0
    if last15["ema50"] < last15["ema200"]:
        score += 1.0
    return score


def build_signal(symbol: str, market_info: dict, btc_regime: str) -> Optional[dict]:
    try:
        funding = get_funding_rate(symbol)
        if funding is not None and abs(funding) > MAX_ABS_FUNDING:
            return None

        df5 = get_kline(symbol, TF_ENTRY, KLINE_LIMIT)
        df15 = get_kline(symbol, TF_CONFIRM, KLINE_LIMIT)

        if len(df5) < 230 or len(df15) < 230:
            return None

        df5 = enrich_indicators(df5)
        df15 = enrich_indicators(df15)

        last5 = df5.iloc[-1]
        prev5 = df5.iloc[-2]
        last15 = df15.iloc[-1]

        if last5["atr"] <= 0:
            return None

        breakout_long, breakdown_short = breakout_flags(df5)

        trend_long_score = trend_score_long(last15)
        trend_short_score = trend_score_short(last15)

        trend_long = trend_long_score >= 2.5
        trend_short = trend_short_score >= 2.5

        vol_ratio = 0.0
        if pd.notna(last5["vol_ma"]) and last5["vol_ma"] > 0:
            vol_ratio = float(last5["vol"] / last5["vol_ma"])

        vol_ok = vol_ratio >= VOLUME_SURGE_MIN
        adx_ok = last5["adx"] >= MIN_ADX

        candle_range_atr = (last5["range"] / last5["atr"]) if last5["atr"] > 0 else 999
        if candle_range_atr > MAX_LAST_CANDLE_RANGE_ATR:
            return None

        distance_from_ema20_atr = abs(last5["close"] - last5["ema20"]) / last5["atr"]
        if distance_from_ema20_atr > MAX_DISTANCE_FROM_EMA20_ATR:
            return None

        long_trigger = breakout_long or ema_reclaim_long(last5, prev5)
        short_trigger = breakdown_short or ema_reject_short(last5, prev5)

        long_ok = all([
            trend_long,
            long_trigger,
            last5["rsi"] >= MIN_RSI_LONG,
            adx_ok,
            wick_ok_for_long(last5),
        ])

        short_ok = all([
            trend_short,
            short_trigger,
            last5["rsi"] <= MAX_RSI_SHORT,
            adx_ok,
            wick_ok_for_short(last5),
        ])

        # Volume yoksa tamamen öldürme, ama puanda ceza ver
        if USE_BTC_REGIME_FILTER:
            if btc_regime == "LONG_ONLY":
                short_ok = False
            elif btc_regime == "SHORT_ONLY":
                long_ok = False
            else:
                # BTC nötrse daha seçici ol
                if long_ok and not breakout_long and vol_ratio < 1.05:
                    long_ok = False
                if short_ok and not breakdown_short and vol_ratio < 1.05:
                    short_ok = False

        if not long_ok and not short_ok:
            return None

        if long_ok and short_ok:
            long_seed = trend_long_score + max(0.0, last5["rsi"] - 50) + last5["adx"] * 0.15
            short_seed = trend_short_score + max(0.0, 50 - last5["rsi"]) + last5["adx"] * 0.15
            if long_seed >= short_seed:
                short_ok = False
            else:
                long_ok = False

        entry = float(last5["close"])
        atrv = float(last5["atr"])

        if long_ok:
            side = "LONG"
            sl = entry - atrv * SL_ATR_MULT
            tp1 = entry + atrv * TP1_ATR_MULT
            tp2 = entry + atrv * TP2_ATR_MULT
            tp3 = entry + atrv * TP3_ATR_MULT
            setup = "Breakout" if breakout_long else "EMA Reclaim"
            setup_bonus = 0.9 if breakout_long else 0.5
            trend_bonus = trend_long_score
        else:
            side = "SHORT"
            sl = entry + atrv * SL_ATR_MULT
            tp1 = entry - atrv * TP1_ATR_MULT
            tp2 = entry - atrv * TP2_ATR_MULT
            tp3 = entry - atrv * TP3_ATR_MULT
            setup = "Breakdown" if breakdown_short else "EMA Reject"
            setup_bonus = 0.9 if breakdown_short else 0.5
            trend_bonus = trend_short_score

        rr = compute_rr(side, entry, sl, tp2)
        if rr < MIN_RR_TO_TP2:
            return None

        # Kalite puanı
        score = 0.0

        score += clamp(trend_bonus * 0.90, 0.0, 3.0)
        score += clamp(abs(float(last5["rsi"]) - 50) * 0.08, 0.0, 1.8)
        score += clamp((float(last5["adx"]) - 14) * 0.12, 0.0, 2.0)
        score += clamp((vol_ratio - 1.0) * 1.4, 0.0, 1.6)
        score += clamp(rr * 1.25, 0.0, 2.6)
        score += setup_bonus

        # Likidite bonusu
        if market_info["amount24"] >= 10_000_000:
            score += 0.7
        elif market_info["amount24"] >= 5_000_000:
            score += 0.4

        # Spread cezası
        score -= clamp(market_info["spread_pct"] * 2.8, 0.0, 1.1)

        # Funding cezası
        if funding is not None:
            if side == "LONG" and funding > 0:
                score -= clamp(funding * 800, 0.0, 0.8)
            elif side == "SHORT" and funding < 0:
                score -= clamp(abs(funding) * 800, 0.0, 0.8)

        # Volume zayıfsa hafif ceza
        if not vol_ok:
            score -= 0.35

        score = round(clamp(score, 1.0, 10.0), 1)

        # Düşük kaliteli sinyalleri ele
        if score < 6.2:
            return None

        return {
            "symbol": symbol,
            "side": side,
            "entry": round(entry, 10),
            "sl": round(sl, 10),
            "tp1": round(tp1, 10),
            "tp2": round(tp2, 10),
            "tp3": round(tp3, 10),
            "rr": round(rr, 2),
            "score": score,
            "setup": setup,
            "tf": "5m / 15m",
            "spread_pct": round(market_info["spread_pct"], 3),
            "amount24": market_info["amount24"],
            "hold_vol": market_info["hold_vol"],
            "funding": None if funding is None else round(funding * 100, 4),
            "rsi": round(float(last5["rsi"]), 1),
            "adx": round(float(last5["adx"]), 1),
            "vol_ratio": round(vol_ratio, 2),
            "btc_regime": btc_regime
        }

    except Exception:
        return None


# =========================================================
# MESAJ FORMAT
# =========================================================
def format_signal_message(sig: dict) -> str:
    symbol = html.escape(sig["symbol"].replace("_", "/"))
    side = html.escape(sig["side"])
    setup = html.escape(sig["setup"])
    tf = html.escape(sig["tf"])

    funding_text = "N/A" if sig["funding"] is None else f"{sig['funding']}%"
    emoji = "🟢" if sig["side"] == "LONG" else "🔴"

    return (
        f"🚨 <b>COINRADAR ELIT++ SIGNAL</b>\n\n"
        f"{emoji} <b>{side} | {symbol}</b>\n"
        f"<b>Entry:</b> {fmt_price(sig['entry'])}\n"
        f"<b>Stop:</b> {fmt_price(sig['sl'])}\n\n"
        f"<b>TP1:</b> {fmt_price(sig['tp1'])}\n"
        f"<b>TP2:</b> {fmt_price(sig['tp2'])}\n"
        f"<b>TP3:</b> {fmt_price(sig['tp3'])}\n\n"
        f"<b>R/R:</b> {sig['rr']}\n"
        f"<b>Score:</b> {sig['score']}/10\n"
        f"<b>Setup:</b> {setup}\n"
        f"<b>TF:</b> {tf}\n"
        f"<b>RSI:</b> {sig['rsi']} | <b>ADX:</b> {sig['adx']}\n"
        f"<b>Vol x:</b> {sig['vol_ratio']}\n"
        f"<b>Spread:</b> {sig['spread_pct']}%\n"
        f"<b>Funding:</b> {funding_text}\n"
        f"<b>BTC Bias:</b> {html.escape(sig['btc_regime'])}"
    )


def format_startup_message() -> str:
    return (
        "✅ <b>COINRADAR ELIT++ başladı</b>\n\n"
        f"Taranan maksimum sözleşme: <b>{MAX_SYMBOLS_TO_SCAN}</b>\n"
        f"Seçilen sinyal sayısı: <b>{TOP_N_SIGNALS}</b>\n"
        "MEXC futures market izleniyor.\n"
        "Kaliteli 3 sinyal modu aktif."
    )


def format_heartbeat_message(symbol_count: int, btc_regime: str) -> str:
    return (
        "ℹ️ <b>COINRADAR ELIT++ aktif</b>\n\n"
        f"Taranan sözleşme: <b>{symbol_count}</b>\n"
        f"BTC Bias: <b>{html.escape(btc_regime)}</b>\n"
        "Bu turda filtreleri geçen sinyal bulunmadı."
    )


def format_error_message(err: str) -> str:
    txt = html.escape(err[:800])
    return f"⚠️ <b>COINRADAR ELIT++ HATA</b>\n\n<code>{txt}</code>"


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
    seen = set()
    out = []
    for sig in signals:
        key = sig["symbol"]
        if key in seen:
            continue
        seen.add(key)
        out.append(sig)
    return out


def pick_top_signals(signals: List[dict], n: int) -> List[dict]:
    # Önce en yüksek skor, sonra RR, sonra likidite
    signals = dedupe_signals(signals)
    signals = sorted(
        signals,
        key=lambda x: (x["score"], x["rr"], x["amount24"]),
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
            universe = build_market_universe()
            btc_regime = get_btc_regime()

            print(f"[INFO] Universe: {len(universe)} | BTC bias: {btc_regime}")

            signals = []
            for item in universe:
                sym = item["symbol"]
                sig = build_signal(sym, item, btc_regime)
                if not sig:
                    continue
                if is_on_cooldown(state, sig["symbol"], sig["side"]):
                    continue
                signals.append(sig)

            top_signals = pick_top_signals(signals, TOP_N_SIGNALS)

            if top_signals:
                for sig in top_signals:
                    ok = telegram_send_html(format_signal_message(sig))
                    if ok:
                        mark_sent(state, sig["symbol"], sig["side"])
                        print(f"[SENT] {sig['side']} {sig['symbol']} score={sig['score']} rr={sig['rr']}")
                    else:
                        print(f"[FAIL] Telegram gönderilemedi: {sig['symbol']}")
            else:
                print("[INFO] Uygun sinyal yok.")
                if SEND_HEARTBEAT_IF_NO_SIGNAL:
                    last_hb = state.get("last_heartbeat", 0)
                    if (now_ts() - last_hb) >= HEARTBEAT_EVERY_MINUTES * 60:
                        if telegram_send_html(format_heartbeat_message(len(universe), btc_regime)):
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
