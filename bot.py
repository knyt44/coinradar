#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import math
import traceback
from datetime import datetime

import requests
import pandas as pd
import numpy as np

# =========================
# CONFIG
# =========================

MEXC_BASE_URL = "https://api.mexc.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = os.getenv("STATE_FILE", "mexc_spot_scanner_state.json")

DEBUG = os.getenv("DEBUG", "true").strip().lower() == "true"
SEND_DEBUG_TO_TELEGRAM = os.getenv("SEND_DEBUG_TO_TELEGRAM", "true").strip().lower() == "true"

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "180"))
TOP_SYMBOL_LIMIT = int(os.getenv("TOP_SYMBOL_LIMIT", "50"))
MIN_24H_QUOTE_VOL_USDT = float(os.getenv("MIN_24H_QUOTE_VOL_USDT", "500000"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.0005"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "50000"))

MIN_SIGNAL_GAP_MINUTES = int(os.getenv("MIN_SIGNAL_GAP_MINUTES", "240"))
MAX_ACTIVE_SIGNAL_HOURS = int(os.getenv("MAX_ACTIVE_SIGNAL_HOURS", "18"))

TREND_INTERVAL = os.getenv("TREND_INTERVAL", "15m")
ENTRY_INTERVAL = os.getenv("ENTRY_INTERVAL", "5m")

TREND_KLINE_LIMIT = int(os.getenv("TREND_KLINE_LIMIT", "240"))
ENTRY_KLINE_LIMIT = int(os.getenv("ENTRY_KLINE_LIMIT", "240"))

RSI_BULL_MIN_15M = float(os.getenv("RSI_BULL_MIN_15M", "48"))
RSI_ENTRY_MIN_5M = float(os.getenv("RSI_ENTRY_MIN_5M", "50"))
ATR_PCT_MIN = float(os.getenv("ATR_PCT_MIN", "0.003"))
ATR_PCT_MAX = float(os.getenv("ATR_PCT_MAX", "0.040"))
MIN_VOLUME_BOOST = float(os.getenv("MIN_VOLUME_BOOST", "1.00"))

SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.20"))
TP1_ATR_MULT = float(os.getenv("TP1_ATR_MULT", "1.40"))
TP2_ATR_MULT = float(os.getenv("TP2_ATR_MULT", "2.20"))
TP3_ATR_MULT = float(os.getenv("TP3_ATR_MULT", "3.20"))
MIN_RR = float(os.getenv("MIN_RR", "1.05"))

BTC_FILTER_ENABLED = os.getenv("BTC_FILTER_ENABLED", "false").strip().lower() == "true"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; MEXC-Spot-Scanner/3.0)"
})


# =========================
# LOG / UTIL
# =========================

def now_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg: str):
    print(f"{now_str()} | INFO | {msg}", flush=True)

def log_err(msg: str):
    print(f"{now_str()} | ERROR | {msg}", flush=True)

def safe_float(x, default=np.nan):
    try:
        return float(x)
    except Exception:
        return default

def utc_now_ts():
    return int(time.time())

def minutes_since(ts):
    return (utc_now_ts() - int(ts)) / 60.0

def hours_since(ts):
    return (utc_now_ts() - int(ts)) / 3600.0

def fmt_price(x):
    try:
        x = float(x)
    except Exception:
        return "?"
    if not np.isfinite(x):
        return "?"
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
    return str(symbol).replace("_", "").replace("/", "").upper().strip()

def fetch_json(url, params=None, timeout=20, max_retries=3):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            log_err(f"HTTP hata [{attempt}/{max_retries}] {url} params={params} err={e}")
            time.sleep(min(2.0 * attempt, 5.0))
    raise last_err


# =========================
# TELEGRAM
# =========================

def send_telegram(text, markdown=True):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log_err("Telegram env eksik")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4000],
        "disable_web_page_preview": True
    }
    if markdown:
        payload["parse_mode"] = "Markdown"

    try:
        r = SESSION.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            log_err(f"Telegram hata: {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        log_err(f"Telegram gönderim hatası: {e}")
        return False

def debug_telegram(text: str):
    if not SEND_DEBUG_TO_TELEGRAM:
        return
    try:
        safe = text.replace("```", "'''")
        send_telegram(f"```{safe[:3500]}```", markdown=True)
    except Exception:
        pass


# =========================
# STATE
# =========================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_alerts": {}, "active_signals": {}, "sent_startup": False}
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
        return {"last_alerts": {}, "active_signals": {}, "sent_startup": False}

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# =========================
# FILTERS
# =========================

LEVERAGED_SUFFIXES = (
    "UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT",
    "3LUSDT", "3SUSDT", "4LUSDT", "4SUSDT", "5LUSDT", "5SUSDT"
)

STABLE_BASES = {
    "USDC", "USDP", "TUSD", "FDUSD", "BUSD", "DAI", "USDD",
    "EUR", "EURT", "AEUR", "TRY", "BRL", "GBP"
}

BAD_BASE_KEYWORDS = {
    "NFT", "BRC20"
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

    if re.search(r"(BULL|BEAR|UP|DOWN|[345]L|[345]S)$", base):
        return True

    if base in BAD_BASE_KEYWORDS:
        return True

    return False


# =========================
# DATAFRAME SAFETY
# =========================

def safe_tail_rows(df, n=3):
    try:
        if df is None or df.empty:
            return "EMPTY_DF"
        return df.tail(n).to_dict(orient="records")
    except Exception as e:
        return f"tail_err={e}"

def ensure_numeric_ohlcv(df: pd.DataFrame, symbol="?", where="?") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    needed = ["open", "high", "low", "close", "volume"]

    for col in needed:
        if col not in df.columns:
            log_err(f"{symbol} {where} missing column: {col} | cols={list(df.columns)}")
            return pd.DataFrame()

    for col in needed:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=needed).reset_index(drop=True)

    if DEBUG:
        log(f"{symbol} {where} rows={len(df)} dtypes={df.dtypes.astype(str).to_dict()}")

    return df


# =========================
# INDICATORS
# =========================

def ema(series, length):
    series = pd.to_numeric(series, errors="coerce")
    return series.ewm(span=length, adjust=False).mean()

def rsi(series, length=14):
    series = pd.to_numeric(series, errors="coerce")
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)

def atr(df, length=14):
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()

def prepare_indicators(df: pd.DataFrame, symbol="?", where="?") -> pd.DataFrame:
    try:
        df = ensure_numeric_ohlcv(df, symbol=symbol, where=f"prepare_indicators[{where}]")
        if df.empty:
            return pd.DataFrame()

        if len(df) < 50:
            log_err(f"{symbol} {where} yeterli veri yok: rows={len(df)}")
            return pd.DataFrame()

        df["ema20"] = ema(df["close"], 20)
        df["ema50"] = ema(df["close"], 50)
        df["ema200"] = ema(df["close"], 200)
        df["rsi14"] = rsi(df["close"], 14)
        df["atr14"] = atr(df, 14)
        df["vol_ma20"] = pd.to_numeric(df["volume"], errors="coerce").rolling(20).mean()

        df = df.dropna(subset=["ema20", "ema50", "ema200", "rsi14", "atr14"]).reset_index(drop=True)

        if df.empty:
            log_err(f"{symbol} {where} indikatör sonrası df boş")
            return pd.DataFrame()

        return df

    except Exception as e:
        log_err(f"{symbol} prepare_indicators patladı [{where}]: {e}")
        log_err(traceback.format_exc())
        debug_telegram(
            f"{symbol} prepare_indicators patladı [{where}]\n"
            f"err={e}\n"
            f"tail={safe_tail_rows(df)}"
        )
        return pd.DataFrame()


# =========================
# MEXC API
# =========================

def get_mexc_tradeable_symbols():
    url = f"{MEXC_BASE_URL}/api/v3/exchangeInfo"
    data = fetch_json(url, timeout=30)

    out = set()
    for item in data.get("symbols", []):
        try:
            symbol = clean_symbol_text(item.get("symbol", ""))
            status = str(item.get("status", ""))
            if status == "1" and symbol.endswith("USDT") and not is_leveraged_or_bad(symbol):
                out.add(symbol)
        except Exception:
            continue
    return sorted(out)

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
            }
        except Exception:
            continue
    return out

def get_mexc_klines(symbol, interval, limit=200):
    url = f"{MEXC_BASE_URL}/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": min(int(limit), 500)
    }

    try:
        raw = fetch_json(url, params=params, timeout=20)
    except Exception as e:
        log_err(f"{symbol} {interval} kline fetch hatası: {e}")
        return pd.DataFrame()

    if not isinstance(raw, list) or len(raw) == 0:
        log_err(f"{symbol} {interval} boş/invalid kline raw")
        return pd.DataFrame()

    rows = []
    for i, row in enumerate(raw):
        try:
            if not isinstance(row, (list, tuple)):
                continue
            if len(row) < 6:
                continue

            rows.append({
                "open_time": row[0],
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
            })
        except Exception as e:
            log_err(f"{symbol} {interval} row parse hatası idx={i}: {e}")

    if not rows:
        log_err(f"{symbol} {interval} parse sonrası rows boş")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = ensure_numeric_ohlcv(df, symbol=symbol, where=f"get_mexc_klines[{interval}]")

    if df.empty:
        log_err(f"{symbol} {interval} numeric sonrası df boş | raw_sample={str(raw[:2])[:500]}")
        return pd.DataFrame()

    try:
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True, errors="coerce")
    except Exception as e:
        log_err(f"{symbol} {interval} open_time parse hatası: {e}")

    return df.reset_index(drop=True)


# =========================
# ANALYSIS
# =========================

def assess_trend_15m(df15: pd.DataFrame):
    if len(df15) < 210:
        return None

    last = df15.iloc[-1]

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

    swing_low = pd.to_numeric(df15["low"].tail(48), errors="coerce").min()
    swing_high = pd.to_numeric(df15["high"].tail(48), errors="coerce").max()
    denom = max((swing_high - swing_low), 1e-12)
    zone_pos = (close - swing_low) / denom

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
    }

def assess_entry_5m(df5: pd.DataFrame):
    if len(df5) < 80:
        return None

    last = df5.iloc[-1]
    prev = df5.iloc[-2]

    close = float(last["close"])
    high = float(last["high"])
    low = float(last["low"])
    ema20v = float(last["ema20"])
    ema50v = float(last["ema50"])
    rsi5 = float(last["rsi14"])
    atr5 = float(last["atr14"])
    vol = float(last["volume"])
    vol_ma = float(last["vol_ma20"]) if not pd.isna(last["vol_ma20"]) else 0.0

    recent_high = float(pd.to_numeric(df5["high"].iloc[-12:-2], errors="coerce").max())

    breakout = close > recent_high
    reclaim = (
        low <= ema20v * 1.003 and
        close > ema20v and
        close > float(prev["close"])
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
        "tp2_notified": False,
        "status": "ACTIVE"
    }


# =========================
# BTC FILTER
# =========================

def get_btc_filter_state():
    try:
        btc_df = get_mexc_klines("BTCUSDT", TREND_INTERVAL, TREND_KLINE_LIMIT)
        if btc_df.empty:
            return {"ok": False, "reason": "btc_df_empty"}

        btc_df = prepare_indicators(btc_df, symbol="BTCUSDT", where="btc_filter")
        if btc_df.empty:
            return {"ok": False, "reason": "btc_indicators_empty"}

        last = btc_df.iloc[-1]

        close = float(last["close"])
        ema20v = float(last["ema20"])
        ema50v = float(last["ema50"])
        rsi14v = float(last["rsi14"])

        bullish = close > ema20v > ema50v and rsi14v >= 50
        bearish = close < ema20v < ema50v and rsi14v <= 50

        return {
            "ok": True,
            "bullish": bullish,
            "bearish": bearish,
            "close": close,
            "ema20": ema20v,
            "ema50": ema50v,
            "rsi": rsi14v
        }

    except Exception as e:
        log_err(f"BTC filtre hatası: {e}")
        log_err(traceback.format_exc())
        debug_telegram(f"BTC filtre hatası\nerr={e}\n{traceback.format_exc()[:2500]}")
        return {"ok": False, "reason": str(e)}


# =========================
# SIGNAL TEXT
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
            f"*Yeni durum:* `BE modu`"
        )
    elif event == "TP2":
        return (
            f"*SİNYAL GÜNCELLEME*\n\n"
            f"`{symbol}` TP2 gördü 🚀\n"
            f"*Fiyat:* `{fmt_price(price)}`"
        )
    elif event == "TP3":
        return (
            f"*SİNYAL SONUCU*\n\n"
            f"`{symbol}` TP3 gördü 🎯\n"
            f"*Fiyat:* `{fmt_price(price)}`"
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
# STATE OPS
# =========================

def can_send_new_alert(state, symbol):
    ts = state.get("last_alerts", {}).get(symbol)
    if not ts:
        return True
    return minutes_since(ts) >= MIN_SIGNAL_GAP_MINUTES

def mark_alert_sent(state, symbol):
    state["last_alerts"][symbol] = utc_now_ts()

def cleanup_old_signals(state):
    active = state.get("active_signals", {})
    remove_list = []
    for symbol, sig in active.items():
        created_at = sig.get("created_at", utc_now_ts())
        if hours_since(created_at) > MAX_ACTIVE_SIGNAL_HOURS:
            remove_list.append(symbol)
    for symbol in remove_list:
        active.pop(symbol, None)

def monitor_active_signals(state, tickers24):
    active = state.get("active_signals", {})
    changed = False
    remove_list = []

    for symbol, sig in list(active.items()):
        try:
            t = tickers24.get(symbol)
            if not t:
                continue

            price = safe_float(t.get("lastPrice"))
            if not np.isfinite(price) or price <= 0:
                continue

            entry = float(sig["entry"])
            tp1 = float(sig["tp1"])
            tp2 = float(sig["tp2"])
            tp3 = float(sig["tp3"])
            tp1_hit = bool(sig.get("tp1_hit", False))

            if not tp1_hit and price >= tp1:
                sig["tp1_hit"] = True
                sig["stop"] = entry
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
                event = "BE_STOP" if sig.get("tp1_hit", False) else "STOP"
                send_telegram(format_update_message(symbol, event, sig, price))
                remove_list.append(symbol)
                changed = True

        except Exception as e:
            log_err(f"Aktif sinyal izleme hatası {symbol}: {e}")

    for symbol in remove_list:
        active.pop(symbol, None)

    return changed


# =========================
# UNIVERSE / RANKING
# =========================

def build_symbol_universe():
    log("Sadece MEXC universe çekiliyor...")
    mexc_symbols = set(get_mexc_tradeable_symbols())
    symbols = [s for s in mexc_symbols if not is_leveraged_or_bad(s)]
    log(f"Filtrelenmiş MEXC sembol sayısı: {len(symbols)}")
    return sorted(symbols)

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


# =========================
# ANALYZE SYMBOL
# =========================

def analyze_symbol(symbol, t24, btc_state=None):
    try:
        if BTC_FILTER_ENABLED and btc_state and btc_state.get("ok", False):
            if not btc_state.get("bullish", False):
                return None

        df15 = get_mexc_klines(symbol, TREND_INTERVAL, TREND_KLINE_LIMIT)
        if df15.empty:
            return None

        df15 = prepare_indicators(df15, symbol=symbol, where="15m")
        if df15.empty:
            return None

        df5 = get_mexc_klines(symbol, ENTRY_INTERVAL, ENTRY_KLINE_LIMIT)
        if df5.empty:
            return None

        df5 = prepare_indicators(df5, symbol=symbol, where="5m")
        if df5.empty:
            return None

        trend_info = assess_trend_15m(df15)
        if not trend_info or not trend_info["bullish"]:
            return None

        entry_info = assess_entry_5m(df5)
        if not entry_info:
            return None

        return build_long_signal(symbol, trend_info, entry_info, t24)

    except Exception as e:
        log_err(f"Sembol analiz hatası {symbol}: {e}")
        log_err(traceback.format_exc())
        debug_telegram(
            f"Sembol analiz hatası {symbol}\n"
            f"err={e}\n"
            f"df15_tail={safe_tail_rows(df15 if 'df15' in locals() else None)}\n"
            f"df5_tail={safe_tail_rows(df5 if 'df5' in locals() else None)}"
        )
        return None


# =========================
# MAIN
# =========================

def main():
    log("MEXC ONLY SPOT SCANNER başlıyor")
    state = load_state()

    if not state.get("sent_startup", False):
        ok = send_telegram(
            "✅ *MEXC only spot scanner başlatıldı.*\n"
            f"MEXC market: `{MEXC_BASE_URL}`\n"
            f"Saat: `{now_str()}`"
        )
        if ok:
            state["sent_startup"] = True
            save_state(state)

    universe = build_symbol_universe()
    if not universe:
        log_err("Universe boş geldi. 60 saniye bekleniyor.")
        time.sleep(60)

    while True:
        cycle_start = time.time()

        try:
            cleanup_old_signals(state)

            tickers24 = get_mexc_24h_tickers()

            changed = monitor_active_signals(state, tickers24)
            if changed:
                save_state(state)

            if not universe:
                universe = build_symbol_universe()

            btc_state = {"ok": False, "reason": "disabled"}
            if BTC_FILTER_ENABLED:
                btc_state = get_btc_filter_state()
                if not btc_state.get("ok", False):
                    log_err(f"BTC filtre devre dışı, sebep={btc_state.get('reason')}")
                else:
                    log(f"BTC filtre aktif | bullish={btc_state['bullish']} rsi={btc_state['rsi']:.2f}")

            ranked_symbols = rank_symbols_by_liquidity(universe, tickers24)
            log(f"Taranacak sembol sayısı: {len(ranked_symbols)}")

            found = 0
            for symbol in ranked_symbols:
                try:
                    if not can_send_new_alert(state, symbol):
                        continue

                    t24 = tickers24.get(symbol, {})
                    sig = analyze_symbol(symbol, t24, btc_state=btc_state)
                    if not sig:
                        continue

                    ok = send_telegram(format_signal_message(sig))
                    if ok:
                        state["active_signals"][symbol] = sig
                        mark_alert_sent(state, symbol)
                        save_state(state)
                        found += 1
                        log(f"Sinyal gönderildi: {symbol}")

                        if found >= 4:
                            break

                    time.sleep(0.20)

                except Exception as e:
                    log_err(f"Tarama içi hata {symbol}: {e}")
                    log_err(traceback.format_exc())

            elapsed = time.time() - cycle_start
            log(f"Tarama tamamlandı | sinyal={found} | süre={elapsed:.1f}s")

        except Exception as e:
            log_err(f"Ana döngü hatası: {e}")
            log_err(traceback.format_exc())
            debug_telegram(f"Ana döngü hatası\nerr={e}\n{traceback.format_exc()[:2500]}")

        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
