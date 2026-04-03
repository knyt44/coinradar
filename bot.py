# -*- coding: utf-8 -*-
"""
COINRADAR PRO++ - MEXC FUTURES SIGNAL BOT
Tek dosya / tek parça / Telegram profesyonel formatlı sinyal botu

Kurulum:
    pip install requests pandas

Çalıştırma:
    python coinradar_propp.py

Not:
- Bu bot sinyal üretir, otomatik emir açmaz.
- MEXC futures market verisini tarar.
- Telegram'a profesyonel formatta mesaj gönderir.
"""

import time
import math
import html
import json
import traceback
from typing import Dict, List, Optional, Tuple, Set

import requests
import pandas as pd


# =========================================================
# TELEGRAM
# =========================================================
TELEGRAM_BOT_TOKEN = "BURAYA_TOKEN"
TELEGRAM_CHAT_ID = "BURAYA_CHAT_ID"

# =========================================================
# GENEL AYARLAR
# =========================================================
MEXC_BASE = "https://contract.mexc.com"
CHECK_EVERY_SECONDS = 60
HTTP_TIMEOUT = 15

# Kaç coin taransın
MAX_SYMBOLS_TO_SCAN = 120

# Sadece en iyi kaç sinyal gönderilsin
TOP_N_SIGNALS = 2

# Aynı coin için tekrar sinyal cooldown
SIGNAL_COOLDOWN_MINUTES = 45

# Durum mesajı
SEND_STARTUP_MESSAGE = True
SEND_HEARTBEAT_IF_NO_SIGNAL = True
HEARTBEAT_EVERY_MINUTES = 30

# State
STATE_FILE = "coinradar_propp_state.json"

# =========================================================
# MARKET / FİLTRE
# =========================================================
QUOTE_CURRENCY = "USDT"

# Çöp coin / likidite / spread / funding filtreleri
MIN_AMOUNT24_USDT = 8_000_000       # 24h turnover
MIN_HOLDVOL = 100_000               # open interest contracts
MAX_SPREAD_PCT = 0.20               # yüzde
MAX_ABS_FUNDING = 0.0012            # 0.12%
MAX_24H_PUMP_PCT = 10.0             # aşırı pump kovalanmasın
MIN_PRICE = 0.00001

# Kara liste
BLACKLIST_KEYWORDS = {
    "1000", "10000", "100000", "MOG", "PEPE", "FLOKI",
    "LUNA", "USTC", "BULL", "BEAR"
}

# Sadece perpetual ve USDT
REQUIRE_USDT_PERP = True

# =========================================================
# İNDİKATÖRLER
# =========================================================
TF_SIGNAL = "Min5"
TF_TREND = "Min15"
KLINE_LIMIT = 220

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
VOL_MA_PERIOD = 20

MIN_RSI_LONG = 53
MAX_RSI_SHORT = 47
MIN_ADX = 18
MIN_RR_TO_TP2 = 1.60

# BTC rejim filtresi
USE_BTC_REGIME_FILTER = True
BTC_SYMBOL = "BTC_USDT"

# Mesafe / kalite filtreleri
MAX_DISTANCE_FROM_EMA20_ATR = 1.8
MAX_LAST_CANDLE_RANGE_ATR = 2.2
MIN_BREAKOUT_BODY_ATR = 0.15

# SL / TP ATR katları
SL_ATR_MULT = 1.15
TP1_ATR_MULT = 1.20
TP2_ATR_MULT = 2.00
TP3_ATR_MULT = 3.00

# =========================================================
# REQUEST SESSION
# =========================================================
session = requests.Session()
session.headers.update({
    "User-Agent": "CoinRadar-ProPP/1.0"
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


def pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return ((a - b) / b) * 100.0


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


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "last_sent": {},      # symbol_side -> ts
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
        "disable_web_page_preview": True
    }
    try:
        r = session.post(url, data=payload, timeout=HTTP_TIMEOUT)
        if r.ok:
            return True
        print("Telegram hata:", r.status_code, r.text[:300])
        return False
    except Exception as e:
        print("Telegram exception:", e)
        return False


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


def get_kline(symbol: str, interval: str, limit: int = 220) -> pd.DataFrame:
    end_ = now_ts()
    # limit kadar mum için kaba start
    seconds_per_bar = {
        "Min1": 60, "Min5": 300, "Min15": 900, "Min30": 1800,
        "Min60": 3600, "Hour4": 14400, "Day1": 86400
    }.get(interval, 300)
    start_ = end_ - (limit + 20) * seconds_per_bar

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
    ma_up = up.ewm(alpha=1/period, adjust=False).mean()
    ma_down = down.ewm(alpha=1/period, adjust=False).mean()
    rs = ma_up / ma_down.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = pd.Series(
        [u if (u > d and u > 0) else 0 for u, d in zip(up_move.fillna(0), down_move.fillna(0))],
        index=df.index
    )
    minus_dm = pd.Series(
        [d if (d > u and d > 0) else 0 for u, d in zip(up_move.fillna(0), down_move.fillna(0))],
        index=df.index
    )

    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs()
    ], axis=1).max(axis=1)

    atr_ = tr.ewm(alpha=1/period, adjust=False).mean().replace(0, pd.NA)
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)) * 100
    adx_ = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx_.fillna(0.0)


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
# MARKET FİLTRE
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

        if REQUIRE_USDT_PERP:
            if not symbol.endswith("_USDT"):
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

        spread_pct = 0.0
        if bid1 > 0 and ask1 > 0:
            mid = (bid1 + ask1) / 2.0
            if mid > 0:
                spread_pct = ((ask1 - bid1) / mid) * 100.0

        if spread_pct > MAX_SPREAD_PCT:
            continue

        rows.append({
            "symbol": symbol,
            "last_price": last_price,
            "amount24": amount24,
            "hold_vol": hold_vol,
            "spread_pct": spread_pct,
            "rise_fall_pct": rise_fall_rate,
        })

    rows.sort(key=lambda x: (x["amount24"], x["hold_vol"]), reverse=True)
    return rows[:MAX_SYMBOLS_TO_SCAN]


# =========================================================
# BTC REJİM
# =========================================================
def get_btc_regime() -> str:
    try:
        df = get_kline(BTC_SYMBOL, TF_TREND, 220)
        if len(df) < 210:
            return "NEUTRAL"
        df = enrich_indicators(df)
        last = df.iloc[-1]

        bullish = (
            last["close"] > last["ema20"] > last["ema50"] > last["ema200"]
            and last["rsi"] >= 52
            and last["adx"] >= 18
        )
        bearish = (
            last["close"] < last["ema20"] < last["ema50"] < last["ema200"]
            and last["rsi"] <= 48
            and last["adx"] >= 18
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
def breakdown_or_breakout(df: pd.DataFrame) -> Tuple[bool, bool]:
    """
    breakout_long, breakdown_short
    """
    if len(df) < 25:
        return False, False

    prev20_high = df["high"].iloc[-21:-1].max()
    prev20_low = df["low"].iloc[-21:-1].min()
    last = df.iloc[-1]

    breakout_long = last["close"] > prev20_high and last["body"] >= last["atr"] * MIN_BREAKOUT_BODY_ATR
    breakdown_short = last["close"] < prev20_low and last["body"] >= last["atr"] * MIN_BREAKOUT_BODY_ATR
    return breakout_long, breakdown_short


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


def build_signal(symbol: str, market_info: dict, btc_regime: str) -> Optional[dict]:
    try:
        funding = get_funding_rate(symbol)
        if funding is not None and abs(funding) > MAX_ABS_FUNDING:
            return None

        df5 = get_kline(symbol, TF_SIGNAL, KLINE_LIMIT)
        df15 = get_kline(symbol, TF_TREND, KLINE_LIMIT)

        if len(df5) < 210 or len(df15) < 210:
            return None

        df5 = enrich_indicators(df5)
        df15 = enrich_indicators(df15)

        last5 = df5.iloc[-1]
        prev5 = df5.iloc[-2]
        last15 = df15.iloc[-1]

        breakout_long, breakdown_short = breakdown_or_breakout(df5)

        trend_long = last15["close"] > last15["ema20"] > last15["ema50"] > last15["ema200"]
        trend_short = last15["close"] < last15["ema20"] < last15["ema50"] < last15["ema200"]

        vol_ok = last5["vol"] > (last5["vol_ma"] * 1.20 if pd.notna(last5["vol_ma"]) else 0)
        adx_ok = last5["adx"] >= MIN_ADX

        candle_range_atr = (last5["range"] / last5["atr"]) if last5["atr"] > 0 else 999
        if candle_range_atr > MAX_LAST_CANDLE_RANGE_ATR:
            return None

        distance_from_ema20_atr = abs(last5["close"] - last5["ema20"]) / last5["atr"] if last5["atr"] > 0 else 999
        if distance_from_ema20_atr > MAX_DISTANCE_FROM_EMA20_ATR:
            return None

        # LONG setup
        long_ok = all([
            trend_long,
            breakout_long or (last5["close"] > last5["ema20"] and prev5["close"] <= prev5["ema20"]),
            last5["rsi"] >= MIN_RSI_LONG,
            adx_ok,
            vol_ok,
        ])

        # SHORT setup
        short_ok = all([
            trend_short,
            breakdown_short or (last5["close"] < last5["ema20"] and prev5["close"] >= prev5["ema20"]),
            last5["rsi"] <= MAX_RSI_SHORT,
            adx_ok,
            vol_ok,
        ])

        if USE_BTC_REGIME_FILTER:
            if btc_regime == "LONG_ONLY":
                short_ok = False
            elif btc_regime == "SHORT_ONLY":
                long_ok = False
            elif btc_regime == "NEUTRAL":
                # nötr piyasada daha seçici ol
                if not breakout_long:
                    long_ok = False
                if not breakdown_short:
                    short_ok = False

        if not long_ok and not short_ok:
            return None

        if long_ok and short_ok:
            # Çakışmada trend gücüne bak
            long_score_seed = (last5["rsi"] - 50) + (last5["adx"] - 15)
            short_score_seed = (50 - last5["rsi"]) + (last5["adx"] - 15)
            if long_score_seed >= short_score_seed:
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
        else:
            side = "SHORT"
            sl = entry + atrv * SL_ATR_MULT
            tp1 = entry - atrv * TP1_ATR_MULT
            tp2 = entry - atrv * TP2_ATR_MULT
            tp3 = entry - atrv * TP3_ATR_MULT
            setup = "Breakdown" if breakdown_short else "EMA Reject"

        rr = compute_rr(side, entry, sl, tp2)
        if rr < MIN_RR_TO_TP2:
            return None

        strength = 0.0
        strength += clamp(abs(last5["rsi"] - 50) * 0.20, 0, 3.0)
        strength += clamp((last5["adx"] - 15) * 0.15, 0, 2.5)
        strength += clamp(rr * 1.4, 0, 3.0)
        strength += 0.8 if vol_ok else 0.0
        strength += 0.8 if ((side == "LONG" and breakout_long) or (side == "SHORT" and breakdown_short)) else 0.0

        # Funding aleyhe ise puan kır
        if funding is not None:
            if side == "LONG" and funding > 0:
                strength -= clamp(funding * 1000, 0, 0.7)
            elif side == "SHORT" and funding < 0:
                strength -= clamp(abs(funding) * 1000, 0, 0.7)

        spread_penalty = clamp(market_info["spread_pct"] * 3.0, 0, 1.0)
        strength -= spread_penalty

        score = round(clamp(strength, 1.0, 10.0), 1)

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
            "funding": None if funding is None else round(funding * 100, 4),  # %
            "rsi": round(float(last5["rsi"]), 1),
            "adx": round(float(last5["adx"]), 1),
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
        f"🚨 <b>COINRADAR SIGNAL</b>\n\n"
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
        f"<b>Spread:</b> {sig['spread_pct']}%\n"
        f"<b>Funding:</b> {funding_text}\n"
        f"<b>BTC Bias:</b> {html.escape(sig['btc_regime'])}"
    )


def format_startup_message() -> str:
    return (
        "✅ <b>COINRADAR PRO++ başladı</b>\n\n"
        "Tarama aktif.\n"
        "MEXC futures market izleniyor.\n"
        "Profesyonel format sinyal sistemi hazır."
    )


def format_heartbeat_message(symbol_count: int, btc_regime: str) -> str:
    return (
        "ℹ️ <b>COINRADAR PRO++ aktif</b>\n\n"
        f"Taranan sözleşme: <b>{symbol_count}</b>\n"
        f"BTC Bias: <b>{html.escape(btc_regime)}</b>\n"
        "Şu turda uygun sinyal bulunmadı."
    )


def format_error_message(err: str) -> str:
    txt = html.escape(err[:700])
    return f"⚠️ <b>COINRADAR HATA</b>\n\n<code>{txt}</code>"


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


def pick_top_signals(signals: List[dict], n: int) -> List[dict]:
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
    run()
