#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MEXC BINANCE-LISTED SPOT SCANNER - FINAL FIXED
------------------------------------------------
Mantık:
- Universe = Binance spot USDT market
- Market data = MEXC spot candles
- Sadece Binance'te de listeli olan coinleri MEXC'te tarar
- Çöp / leveraged / stable-benzeri pariteleri filtreler
- Sadece LONG spot fırsatı arar
- Telegram'a giriş / stop / TP1 / TP2 / TP3 yollar
- Aktif sinyali takip eder, TP1 sonrası BE mantığı uygular
- Tek dosya, tek parça

Önemli:
- Bu bot otomatik emir AÇMAZ. Sadece sinyal yollar.
- Railway / Render / VPS gibi yerlerde çalıştırabilirsin.
"""

import os
import re
import json
import time
import math
import traceback
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np

# =========================
# AYARLAR
# =========================

BINANCE_BASE_URL = "https://api.binance.com"
MEXC_BASE_URL = "https://api.mexc.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = os.getenv("STATE_FILE", "mexc_binance_listed_scanner_state.json")

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "180"))   # 3 dk
TOP_SYMBOL_LIMIT = int(os.getenv("TOP_SYMBOL_LIMIT", "120"))            # fazla zorlama yapmasın
MIN_24H_QUOTE_VOL_USDT = float(os.getenv("MIN_24H_QUOTE_VOL_USDT", "1500000"))  # 1.5M
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.0005"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "50000"))

MIN_SIGNAL_GAP_MINUTES = int(os.getenv("MIN_SIGNAL_GAP_MINUTES", "240"))
MAX_ACTIVE_SIGNAL_HOURS = int(os.getenv("MAX_ACTIVE_SIGNAL_HOURS", "18"))

# Trend / entry
TREND_INTERVAL = "15m"
ENTRY_INTERVAL = "5m"

TREND_KLINE_LIMIT = 220
ENTRY_KLINE_LIMIT = 220

# Teknik filtreler
RSI_BULL_MIN_15M = 52
RSI_ENTRY_MIN_5M = 54
ATR_PCT_MIN = 0.004
ATR_PCT_MAX = 0.035
MIN_VOLUME_BOOST = 1.10

# Risk
SL_ATR_MULT = 1.20
TP1_ATR_MULT = 1.40
TP2_ATR_MULT = 2.20
TP3_ATR_MULT = 3.20
MIN_RR = 1.15

# Debug
DEBUG = os.getenv("DEBUG", "true").strip().lower() == "true"


# =========================
# YARDIMCILAR
# =========================

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; MEXC-Binance-Listed-Scanner/1.0)"
})


def log(msg: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} | INFO | {msg}", flush=True)


def log_err(msg: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} | ERROR | {msg}", flush=True)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "last_alerts": {},
            "active_signals": {},
            "sent_startup": False
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"last_alerts": {}, "active_signals": {}, "sent_startup": False}
            data.setdefault("last_alerts", {})
            data.setdefault("active_signals", {})
            data.setdefault("sent_startup", False)
            return data
    except Exception as e:
        log_err(f"State okunamadı: {e}")
        return {
            "last_alerts": {},
            "active_signals": {},
            "sent_startup": False
        }


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def utc_now_ts():
    return int(time.time())


def minutes_since(ts):
    return (utc_now_ts() - int(ts)) / 60.0


def hours_since(ts):
    return (utc_now_ts() - int(ts)) / 3600.0


def safe_float(x, default=np.nan):
    try:
        return float(x)
    except Exception:
        return default


def fetch_json(url, params=None, timeout=20, max_retries=3):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if DEBUG:
                log_err(f"HTTP hata [{attempt}/{max_retries}] {url} params={params} err={e}")
            time.sleep(1.2 * attempt)
    raise last_err


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log_err("Telegram env eksik: TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        r = SESSION.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            log_err(f"Telegram hata: {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        log_err(f"Telegram gönderim hatası: {e}")
        return False


def fmt_price(x):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "?"
    x = float(x)
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 100:
        return f"{x:.3f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.1:
        return f"{x:.5f}"
    if x >= 0.01:
        return f"{x:.6f}"
    if x >= 0.001:
        return f"{x:.7f}"
    return f"{x:.8f}"


def clean_symbol_text(symbol):
    return symbol.replace("_", "").replace("/", "").upper().strip()


# =========================
# FİLTRELER
# =========================

LEVERAGED_SUFFIXES = (
    "UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT",
    "3LUSDT", "3SUSDT", "4LUSDT", "4SUSDT", "5LUSDT", "5SUSDT"
)

BAD_KEYWORDS = (
    "1000", "1000000", "MOG",  # istersen bunu azaltırsın; burada çok agresif değiliz
)

STABLE_BASES = {
    "USDC", "USDP", "TUSD", "FDUSD", "BUSD", "DAI", "USDD", "EUR", "EURT", "AEUR", "TRY", "BRL", "GBP"
}


def is_leveraged_or_bad(symbol: str) -> bool:
    s = symbol.upper()
    if not s.endswith("USDT"):
        return True
    if any(s.endswith(x) for x in LEVERAGED_SUFFIXES):
        return True

    base = s[:-4]
    if base in STABLE_BASES:
        return True

    # Çok tuhaf / spam benzeri bazı isimleri ayıklamak için
    if re.search(r"(BULL|BEAR|UP|DOWN|[345]L|[345]S)$", base):
        return True

    # Çok sert filtre değil, istenirse kapatılabilir
    if any(k in base for k in BAD_KEYWORDS):
        return True

    return False


# =========================
# İNDİKATÖRLER
# =========================

def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def rsi(series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def atr(df, length=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/length, adjust=False).mean()


# =========================
# API ÇEKİM
# =========================

def get_binance_spot_symbols():
    """
    Binance spot exchangeInfo -> TRADING + quoteAsset=USDT + isSpotTradingAllowed
    """
    url = f"{BINANCE_BASE_URL}/api/v3/exchangeInfo"
    data = fetch_json(url, timeout=30)

    symbols = []
    for item in data.get("symbols", []):
        try:
            symbol = clean_symbol_text(item.get("symbol", ""))
            status = item.get("status", "")
            quote = item.get("quoteAsset", "")
            is_spot = item.get("isSpotTradingAllowed", False)

            if status != "TRADING":
                continue
            if quote != "USDT":
                continue
            if not is_spot:
                continue
            if is_leveraged_or_bad(symbol):
                continue

            symbols.append(symbol)
        except Exception:
            continue

    return sorted(set(symbols))


def get_mexc_tradeable_symbols():
    """
    MEXC:
    - exchangeInfo status=1
    - defaultSymbols -> API ile desteklenen spot pariteler
    Kesişim alıyoruz ki bozuk / kapalı pair gelmesin.
    """
    exch_url = f"{MEXC_BASE_URL}/api/v3/exchangeInfo"
    default_url = f"{MEXC_BASE_URL}/api/v3/defaultSymbols"

    exchange_info = fetch_json(exch_url, timeout=30)
    default_symbols_resp = fetch_json(default_url, timeout=30)

    exch_symbols = set()
    for item in exchange_info.get("symbols", []):
        try:
            symbol = clean_symbol_text(item.get("symbol", ""))
            status = str(item.get("status", ""))
            if status == "1" and symbol.endswith("USDT") and not is_leveraged_or_bad(symbol):
                exch_symbols.add(symbol)
        except Exception:
            continue

    default_symbols = set()
    for s in default_symbols_resp.get("data", []):
        s = clean_symbol_text(s)
        if s.endswith("USDT") and not is_leveraged_or_bad(s):
            default_symbols.add(s)

    tradeable = exch_symbols.intersection(default_symbols)
    return sorted(tradeable)


def get_mexc_24h_tickers():
    url = f"{MEXC_BASE_URL}/api/v3/ticker/24hr"
    data = fetch_json(url, timeout=30)
    if not isinstance(data, list):
        return {}
    out = {}
    for item in data:
        try:
            symbol = clean_symbol_text(item.get("symbol", ""))
            out[symbol] = {
                "lastPrice": safe_float(item.get("lastPrice")),
                "quoteVolume": safe_float(item.get("quoteVolume")),
                "volume": safe_float(item.get("volume")),
                "priceChangePercent": safe_float(item.get("priceChangePercent")),
                "highPrice": safe_float(item.get("highPrice")),
                "lowPrice": safe_float(item.get("lowPrice")),
            }
        except Exception:
            continue
    return out


def get_mexc_klines(symbol, interval, limit=200):
    """
    MEXC spot v3 klines 8 kolon döner:
    [openTime, open, high, low, close, volume, closeTime, quoteAssetVolume]
    """
    url = f"{MEXC_BASE_URL}/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": min(int(limit), 500)
    }
    raw = fetch_json(url, params=params, timeout=20)

    if not isinstance(raw, list) or len(raw) == 0:
        return pd.DataFrame()

    rows = []
    for row in raw:
        if not isinstance(row, (list, tuple)):
            continue
        if len(row) < 6:
            continue

        # MEXC dökümanına göre 0..7 alanları var
        # Ama ekstra tolerans bırakıyoruz.
        open_time = safe_float(row[0], np.nan)
        opn = safe_float(row[1], np.nan)
        high = safe_float(row[2], np.nan)
        low = safe_float(row[3], np.nan)
        close = safe_float(row[4], np.nan)
        volume = safe_float(row[5], np.nan)
        close_time = safe_float(row[6], np.nan) if len(row) > 6 else np.nan
        quote_volume = safe_float(row[7], np.nan) if len(row) > 7 else np.nan

        rows.append([
            open_time, opn, high, low, close, volume, close_time, quote_volume
        ])

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume"
    ])

    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).copy()
    if df.empty:
        return df

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True, errors="coerce")
    if "close_time" in df.columns:
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True, errors="coerce")

    for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
    return df


# =========================
# ANALİZ
# =========================

def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)
    df["rsi14"] = rsi(df["close"], 14)
    df["atr14"] = atr(df, 14)
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    return df


def assess_trend_15m(df15: pd.DataFrame):
    if len(df15) < 210:
        return None

    d = prepare_indicators(df15)
    last = d.iloc[-1]

    close = float(last["close"])
    ema20v = float(last["ema20"])
    ema50v = float(last["ema50"])
    ema200v = float(last["ema200"])
    rsiv = float(last["rsi14"])
    atrv = float(last["atr14"])

    atr_pct = atrv / close if close > 0 else 0.0

    bullish = (
        close > ema20v > ema50v > ema200v and
        rsiv >= RSI_BULL_MIN_15M and
        ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX
    )

    swing_low = d["low"].tail(48).min()
    swing_high = d["high"].tail(48).max()
    zone_pos = (close - swing_low) / max((swing_high - swing_low), 1e-12)

    zone = "MID"
    if zone_pos <= 0.33:
        zone = "DISCOUNT"
    elif zone_pos >= 0.66:
        zone = "PREMIUM"

    return {
        "bullish": bullish,
        "close": close,
        "ema20": ema20v,
        "ema50": ema50v,
        "ema200": ema200v,
        "rsi": rsiv,
        "atr": atrv,
        "atr_pct": atr_pct,
        "zone": zone,
        "swing_low": float(swing_low),
        "swing_high": float(swing_high),
        "df": d
    }


def assess_entry_5m(df5: pd.DataFrame):
    if len(df5) < 80:
        return None

    d = prepare_indicators(df5)
    last = d.iloc[-1]
    prev = d.iloc[-2]

    close = float(last["close"])
    high = float(last["high"])
    low = float(last["low"])
    ema20v = float(last["ema20"])
    ema50v = float(last["ema50"])
    rsi5 = float(last["rsi14"])
    atr5 = float(last["atr14"])
    vol = float(last["volume"])
    vol_ma = float(last["vol_ma20"]) if not pd.isna(last["vol_ma20"]) else 0.0

    recent_high = float(d["high"].iloc[-12:-2].max())
    recent_low = float(d["low"].iloc[-12:-2].min())

    breakout = close > recent_high
    reclaim = (
        low <= ema20v * 1.003 and
        close > ema20v and
        close > prev["close"]
    )
    momentum = (
        close > ema20v > ema50v and
        rsi5 >= RSI_ENTRY_MIN_5M and
        vol > 0 and vol_ma > 0 and vol >= vol_ma * MIN_VOLUME_BOOST
    )

    return {
        "ok": breakout or (reclaim and momentum),
        "breakout": breakout,
        "reclaim": reclaim,
        "momentum": momentum,
        "close": close,
        "high": high,
        "low": low,
        "ema20": ema20v,
        "ema50": ema50v,
        "rsi": rsi5,
        "atr": atr5,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "df": d
    }


def build_long_signal(symbol: str, trend_info: dict, entry_info: dict, t24: dict):
    if not trend_info or not entry_info:
        return None

    if not trend_info["bullish"]:
        return None

    if trend_info["zone"] == "PREMIUM":
        return None

    if not entry_info["ok"]:
        return None

    entry = entry_info["close"]
    atrv = entry_info["atr"]

    if not np.isfinite(entry) or not np.isfinite(atrv) or atrv <= 0:
        return None

    stop = entry - atrv * SL_ATR_MULT
    tp1 = entry + atrv * TP1_ATR_MULT
    tp2 = entry + atrv * TP2_ATR_MULT
    tp3 = entry + atrv * TP3_ATR_MULT

    if stop <= 0 or not (stop < entry < tp1 < tp2 < tp3):
        return None

    rr = (tp1 - entry) / max((entry - stop), 1e-12)
    if rr < MIN_RR:
        return None

    score = 0
    score += 3 if trend_info["bullish"] else 0
    score += 2 if trend_info["zone"] == "DISCOUNT" else 1
    score += 2 if entry_info["momentum"] else 0
    score += 2 if entry_info["breakout"] else 0
    score += 1 if entry_info["reclaim"] else 0
    score += 1 if trend_info["rsi"] >= 55 else 0
    score += 1 if entry_info["rsi"] >= 58 else 0

    return {
        "symbol": symbol,
        "side": "LONG",
        "entry": float(entry),
        "stop": float(stop),
        "tp1": float(tp1),
        "tp2": float(tp2),
        "tp3": float(tp3),
        "rr": float(rr),
        "score": int(score),
        "trend_rsi": float(trend_info["rsi"]),
        "entry_rsi": float(entry_info["rsi"]),
        "trend_zone": trend_info["zone"],
        "atr_pct_15m": float(trend_info["atr_pct"]),
        "quoteVolume": float(t24.get("quoteVolume", 0.0)) if t24 else 0.0,
        "lastPrice": float(t24.get("lastPrice", entry)) if t24 else float(entry),
        "created_at": utc_now_ts(),
        "tp1_hit": False,
        "status": "ACTIVE"
    }


# =========================
# TELEGRAM FORMAT
# =========================

def format_signal_message(sig: dict) -> str:
    qv = sig.get("quoteVolume", 0.0)
    atr_pct = sig.get("atr_pct_15m", 0.0) * 100

    return (
        f"*MEXC SPOT LONG SİNYALİ*\n\n"
        f"*Coin:* `{sig['symbol']}`\n"
        f"*Yön:* `{sig['side']}`\n"
        f"*Skor:* `{sig['score']}`\n"
        f"*15m Zone:* `{sig['trend_zone']}`\n\n"
        f"*Giriş:* `{fmt_price(sig['entry'])}`\n"
        f"*Stop:* `{fmt_price(sig['stop'])}`\n"
        f"*TP1:* `{fmt_price(sig['tp1'])}`\n"
        f"*TP2:* `{fmt_price(sig['tp2'])}`\n"
        f"*TP3:* `{fmt_price(sig['tp3'])}`\n"
        f"*RR:* `{sig['rr']:.2f}`\n\n"
        f"*15m RSI:* `{sig['trend_rsi']:.1f}`\n"
        f"*5m RSI:* `{sig['entry_rsi']:.1f}`\n"
        f"*15m ATR%:* `{atr_pct:.2f}%`\n"
        f"*24s Hacim:* `{qv:,.0f} USDT`"
    )


def format_update_message(symbol: str, event: str, sig: dict, price: float) -> str:
    if event == "TP1":
        return (
            f"*SİNYAL GÜNCELLEME*\n\n"
            f"`{symbol}` TP1 gördü ✅\n"
            f"*Fiyat:* `{fmt_price(price)}`\n"
            f"*Yeni durum:* `BE modu`\n"
            f"*Eski giriş:* `{fmt_price(sig['entry'])}`"
        )
    elif event == "TP2":
        return (
            f"*SİNYAL GÜNCELLEME*\n\n"
            f"`{symbol}` TP2 gördü 🚀\n"
            f"*Fiyat:* `{fmt_price(price)}`\n"
            f"*Takip etmeye devam*"
        )
    elif event == "TP3":
        return (
            f"*SİNYAL SONUCU*\n\n"
            f"`{symbol}` TP3 gördü 🎯\n"
            f"*Fiyat:* `{fmt_price(price)}`\n"
            f"*Durum:* `Tam hedef`"
        )
    elif event == "STOP":
        return (
            f"*SİNYAL SONUCU*\n\n"
            f"`{symbol}` stop oldu ❌\n"
            f"*Fiyat:* `{fmt_price(price)}`"
        )
    elif event == "BE_STOP":
        return (
            f"*SİNYAL SONUCU*\n\n"
            f"`{symbol}` BE stop oldu ⚖️\n"
            f"*Fiyat:* `{fmt_price(price)}`"
        )
    return f"{symbol} update: {event} @ {fmt_price(price)}"


# =========================
# SİNYAL / STATE MANTIK
# =========================

def can_send_new_alert(state, symbol):
    last_alerts = state.get("last_alerts", {})
    ts = last_alerts.get(symbol)
    if not ts:
        return True
    return minutes_since(ts) >= MIN_SIGNAL_GAP_MINUTES


def mark_alert_sent(state, symbol):
    state["last_alerts"][symbol] = utc_now_ts()


def cleanup_old_signals(state):
    active = state.get("active_signals", {})
    to_delete = []
    for symbol, sig in active.items():
        created_at = sig.get("created_at", utc_now_ts())
        if hours_since(created_at) > MAX_ACTIVE_SIGNAL_HOURS:
            to_delete.append(symbol)
    for symbol in to_delete:
        active.pop(symbol, None)


def monitor_active_signals(state, tickers24):
    active = state.get("active_signals", {})
    changed = False
    remove_list = []

    for symbol, sig in list(active.items()):
        try:
            if symbol not in tickers24:
                continue

            price = safe_float(tickers24[symbol].get("lastPrice"))
            if not np.isfinite(price) or price <= 0:
                continue

            entry = float(sig["entry"])
            stop = float(sig["stop"])
            tp1 = float(sig["tp1"])
            tp2 = float(sig["tp2"])
            tp3 = float(sig["tp3"])
            tp1_hit = bool(sig.get("tp1_hit", False))

            if not tp1_hit and price >= tp1:
                sig["tp1_hit"] = True
                sig["stop"] = entry  # BE
                changed = True
                send_telegram(format_update_message(symbol, "TP1", sig, price))

            if price >= tp2 and not sig.get("tp2_notified", False):
                sig["tp2_notified"] = True
                changed = True
                send_telegram(format_update_message(symbol, "TP2", sig, price))

            if price >= tp3:
                send_telegram(format_update_message(symbol, "TP3", sig, price))
                remove_list.append(symbol)
                changed = True
                continue

            current_stop = float(sig["stop"])
            if price <= current_stop:
                event = "BE_STOP" if tp1_hit else "STOP"
                send_telegram(format_update_message(symbol, event, sig, price))
                remove_list.append(symbol)
                changed = True

        except Exception as e:
            log_err(f"Aktif sinyal izleme hatası {symbol}: {e}")

    for symbol in remove_list:
        active.pop(symbol, None)

    return changed


# =========================
# ANA TARAMA
# =========================

def build_symbol_universe():
    log("Binance universe çekiliyor...")
    binance_symbols = set(get_binance_spot_symbols())
    log(f"Binance uygun USDT spot: {len(binance_symbols)}")

    log("MEXC tradeable symbols çekiliyor...")
    mexc_symbols = set(get_mexc_tradeable_symbols())
    log(f"MEXC tradeable USDT: {len(mexc_symbols)}")

    common = sorted(binance_symbols.intersection(mexc_symbols))
    common = [s for s in common if not is_leveraged_or_bad(s)]

    log(f"Ortak uygun sembol: {len(common)}")
    return common


def rank_symbols_by_liquidity(symbols, tickers24):
    ranked = []
    for s in symbols:
        t = tickers24.get(s)
        if not t:
            continue

        qv = safe_float(t.get("quoteVolume"), 0.0)
        price = safe_float(t.get("lastPrice"), np.nan)

        if not np.isfinite(price):
            continue
        if price < MIN_PRICE or price > MAX_PRICE:
            continue
        if qv < MIN_24H_QUOTE_VOL_USDT:
            continue

        ranked.append((s, qv))

    ranked.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in ranked[:TOP_SYMBOL_LIMIT]]


def analyze_symbol(symbol, t24):
    try:
        df15 = get_mexc_klines(symbol, TREND_INTERVAL, TREND_KLINE_LIMIT)
        if df15.empty or len(df15) < 210:
            return None

        df5 = get_mexc_klines(symbol, ENTRY_INTERVAL, ENTRY_KLINE_LIMIT)
        if df5.empty or len(df5) < 80:
            return None

        trend_info = assess_trend_15m(df15)
        if not trend_info or not trend_info["bullish"]:
            return None

        entry_info = assess_entry_5m(df5)
        if not entry_info:
            return None

        sig = build_long_signal(symbol, trend_info, entry_info, t24)
        return sig

    except Exception as e:
        log_err(f"Sembol analiz hatası {symbol}: {e}")
        if DEBUG:
            traceback.print_exc()
        return None


def main():
    log("MEXC BINANCE-LISTED SCANNER başlıyor")

    state = load_state()

    if not state.get("sent_startup", False):
        ok = send_telegram("✅ MEXC Binance-listed spot scanner başlatıldı.")
        if ok:
            state["sent_startup"] = True
            save_state(state)

    universe = build_symbol_universe()
    if not universe:
        log_err("Universe boş geldi. 60 saniye sonra tekrar denenecek.")
        time.sleep(60)

    while True:
        cycle_start = time.time()

        try:
            cleanup_old_signals(state)

            log("MEXC 24s ticker verisi çekiliyor...")
            tickers24 = get_mexc_24h_tickers()

            # aktif sinyalleri izle
            changed = monitor_active_signals(state, tickers24)
            if changed:
                save_state(state)

            # universe tekrar oluşturulabilir; ama her tur ağır olmasın
            if not universe:
                universe = build_symbol_universe()

            ranked_symbols = rank_symbols_by_liquidity(universe, tickers24)
            log(f"Taranacak sembol sayısı: {len(ranked_symbols)}")

            found = 0
            for symbol in ranked_symbols:
                try:
                    if not can_send_new_alert(state, symbol):
                        continue

                    t24 = tickers24.get(symbol, {})
                    sig = analyze_symbol(symbol, t24)
                    if not sig:
                        continue

                    msg = format_signal_message(sig)
                    ok = send_telegram(msg)
                    if ok:
                        state["active_signals"][symbol] = sig
                        mark_alert_sent(state, symbol)
                        save_state(state)
                        found += 1
                        log(f"Sinyal gönderildi: {symbol}")

                        # Aynı tur çok fazla spam atmasın
                        if found >= 4:
                            break

                    time.sleep(0.20)

                except Exception as e:
                    log_err(f"Tarama içi hata {symbol}: {e}")

            elapsed = time.time() - cycle_start
            log(f"Tarama tamamlandı. Bulunan sinyal: {found}. Süre: {elapsed:.1f}s")

        except Exception as e:
            log_err(f"Ana döngü hatası: {e}")
            if DEBUG:
                traceback.print_exc()

        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
