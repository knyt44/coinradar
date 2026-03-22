# pip install requests pandas numpy python-dotenv
# .env içeriği:
# BINANCE_API_KEY=...
# BINANCE_API_SECRET=...
# TELEGRAM_BOT_TOKEN=...
# TELEGRAM_CHAT_ID=...

import os
import time
import math
import json
import hmac
import hashlib
import requests
import pandas as pd
import numpy as np
from urllib.parse import urlencode
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# CONFIG
# =========================================================
MODE = "ALERT"          # "ALERT" veya "LIVE"
USE_TESTNET = True      # önce True kullan
SYMBOLS = ["BTCUSDT", "ETHUSDT"]

API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip().encode()
TG_TOKEN = os.getenv("8448822429:AAHgN_Df7SP2zXOKk9HCtc6l-Pf8XRaYcrI", "").strip()
TG_CHAT_ID = os.getenv("917476574", "").strip()

BASE_URL = "https://demo-fapi.binance.com" if USE_TESTNET else "https://fapi.binance.com"
STATE_FILE = "bot_state.json"

TREND_TF = "15m"
ENTRY_TF = "5m"
LOOP_SECONDS = 15
LEVERAGE = 4
POSITION_USDT = 100           # canlı modda işlem başına ayrılan USDT
COOLDOWN_MIN = 45
SETUP_TTL_MIN = 60
ENTRY_ZONE_TOL = 0.0015
MIN_VOL_FACTOR = 1.4
MIN_PREP_SCORE = 6.5
MIN_ENTER_SCORE = 7.3

# =========================================================
# HTTP
# =========================================================
session = requests.Session()
session.headers.update({"X-MBX-APIKEY": API_KEY})

def public_get(path, params=None):
    url = BASE_URL + path
    r = session.get(url, params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()

def signed_request(method, path, params=None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params, doseq=True)
    sig = hmac.new(API_SECRET, query.encode(), hashlib.sha256).hexdigest()
    url = BASE_URL + path + "?" + query + f"&signature={sig}"
    if method == "GET":
        r = session.get(url, timeout=20)
    elif method == "POST":
        r = session.post(url, timeout=20)
    else:
        raise ValueError("unsupported method")
    r.raise_for_status()
    return r.json()

# =========================================================
# TELEGRAM
# =========================================================
def tg(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        print(text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        print("telegram error:", e)

# =========================================================
# STATE
# =========================================================
def default_symbol_state():
    return {
        "phase": "IDLE",  # IDLE / PREPARE_LONG / PREPARE_SHORT / IN_LONG / IN_SHORT
        "last_alert_ts": 0,
        "last_setup_id": "",
        "setup": None,
        "entry_price": 0.0,
        "qty": 0.0,
        "tp1_hit": False,
        "tp2_hit": False,
        "oi_hist": [],
    }

state = {s: default_symbol_state() for s in SYMBOLS}

def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for s in SYMBOLS:
                if s in raw:
                    state[s].update(raw[s])
        except Exception as e:
            print("load_state error:", e)

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def now_ts():
    return time.time()

def mins_since(ts):
    return (now_ts() - ts) / 60 if ts else 1e9

def in_cooldown(symbol):
    return mins_since(state[symbol]["last_alert_ts"]) < COOLDOWN_MIN

# =========================================================
# MARKET DATA
# =========================================================
def get_klines(symbol, interval, limit=300):
    data = public_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    cols = ["open_time","open","high","low","close","volume","close_time","qav","trades","tb_base","tb_quote","ignore"]
    df = pd.DataFrame(data, columns=cols)
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df

def get_open_interest(symbol):
    data = public_get("/fapi/v1/openInterest", {"symbol": symbol})
    return float(data["openInterest"])

def get_book_ticker(symbol):
    return public_get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})

def get_exchange_info():
    return public_get("/fapi/v1/exchangeInfo")

# =========================================================
# INDICATORS
# =========================================================
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ma_up = up.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    ma_dn = dn.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = ma_up / ma_dn.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def atr(df, n=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def add_indicators(df):
    df = df.copy()
    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)
    df["rsi"] = rsi(df["close"], 14)
    df["atr"] = atr(df, 14)
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    return df

# =========================================================
# ANALYSIS
# =========================================================
def near(a, b, tol):
    return abs(a - b) / b <= tol if b else False

def trend_15m(df):
    l = df.iloc[-1]
    p = df.iloc[-2]
    if l["close"] > l["ema200"] and l["ema50"] > l["ema200"] and l["rsi"] > 52 and l["close"] > p["low"]:
        return "LONG"
    if l["close"] < l["ema200"] and l["ema50"] < l["ema200"] and l["rsi"] < 48 and l["close"] < p["high"]:
        return "SHORT"
    return "NEUTRAL"

def bos_5m(df, direction):
    seg = df.iloc[-7:-1]
    last = df.iloc[-1]
    if direction == "LONG":
        return last["close"] > seg["high"].max()
    return last["close"] < seg["low"].min()

def volume_factor(df):
    l = df.iloc[-1]
    if pd.isna(l["vol_ma20"]) or l["vol_ma20"] == 0:
        return 0.0
    return float(l["volume"] / l["vol_ma20"])

def liquidity_flags(df):
    seg = df.iloc[-24:]
    lows = seg["low"].tolist()
    highs = seg["high"].tolist()

    eq_low = False
    eq_high = False
    for i in range(len(lows)):
        for j in range(i+1, len(lows)):
            if near(lows[i], lows[j], 0.0012):
                eq_low = True
                break
        if eq_low:
            break

    for i in range(len(highs)):
        for j in range(i+1, len(highs)):
            if near(highs[i], highs[j], 0.0012):
                eq_high = True
                break
        if eq_high:
            break

    return {
        "eq_low": eq_low,
        "eq_high": eq_high,
        "swing_low": float(seg["low"].min()),
        "swing_high": float(seg["high"].max())
    }

def sweep_reclaim(df, direction):
    seg = df.iloc[-27:]
    last = seg.iloc[-1]
    if direction == "LONG":
        recent_low = seg["low"].iloc[:-1].min()
        swept = last["low"] < recent_low * (1 - 0.0004)
        reclaimed = last["close"] > seg["low"].iloc[-4:-1].min()
        return swept and reclaimed
    else:
        recent_high = seg["high"].iloc[:-1].max()
        swept = last["high"] > recent_high * (1 + 0.0004)
        reclaimed = last["close"] < seg["high"].iloc[-4:-1].max()
        return swept and reclaimed

def oi_change_pct(symbol):
    hist = state[symbol]["oi_hist"]
    if len(hist) < 2 or hist[0] == 0:
        return 0.0
    return (hist[-1] - hist[0]) / hist[0] * 100.0

def book_pressure(symbol):
    bt = get_book_ticker(symbol)
    bid_qty = float(bt["bidQty"])
    ask_qty = float(bt["askQty"])
    den = bid_qty + ask_qty
    return 0.0 if den <= 0 else (bid_qty - ask_qty) / den

def round_qty(qty, step):
    if step <= 0:
        return qty
    return math.floor(qty / step) * step

def symbol_filters():
    info = get_exchange_info()
    out = {}
    for s in info["symbols"]:
        if s["symbol"] not in SYMBOLS:
            continue
        step = 0.0
        tick = 0.0
        for f in s["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
            if f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
        out[s["symbol"]] = {"stepSize": step, "tickSize": tick}
    return out

FILTERS = None

def score_signal(symbol, df15, df5, direction):
    l15 = df15.iloc[-1]
    l5 = df5.iloc[-1]
    vf = volume_factor(df5)
    liq = liquidity_flags(df5)
    sweep = sweep_reclaim(df5, direction)
    oip = oi_change_pct(symbol)
    bp = book_pressure(symbol)

    score = 0.0
    reasons = []
    liquidity_note = []
    whale_note = []

    if direction == "LONG":
        if l15["close"] > l15["ema200"]:
            score += 1.2; reasons.append("15m EMA200 üstü")
        if l15["ema50"] > l15["ema200"]:
            score += 1.0; reasons.append("15m EMA50>EMA200")
        if l15["rsi"] > 52:
            score += 0.8; reasons.append("15m RSI güçlü")
        if bos_5m(df5, "LONG"):
            score += 1.4; reasons.append("5m BOS yukarı")
        if l5["rsi"] > 54:
            score += 0.8; reasons.append("5m momentum pozitif")
        if vf >= MIN_VOL_FACTOR:
            score += 1.1; reasons.append(f"Hacim x{vf:.2f}")
        if sweep:
            score += 1.2; reasons.append("Alt sweep + reclaim")
        if liq["eq_low"]:
            score += 0.5; reasons.append("Alt likidite")
            liquidity_note.append("Alt likidite kümeleri")
        if oip > 0.6:
            score += 0.9 if oip <= 1.5 else 1.3
            reasons.append(f"OI %+{oip:.2f}")
            whale_note.append(f"OI artışı %{oip:.2f}")
        if bp > 0.12:
            score += 0.6; reasons.append("Bid baskısı")
            whale_note.append("Bid tarafı baskın")
        liquidity_note.append(f"Destek {liq['swing_low']:.2f}")
    else:
        if l15["close"] < l15["ema200"]:
            score += 1.2; reasons.append("15m EMA200 altı")
        if l15["ema50"] < l15["ema200"]:
            score += 1.0; reasons.append("15m EMA50<EMA200")
        if l15["rsi"] < 48:
            score += 0.8; reasons.append("15m RSI zayıf")
        if bos_5m(df5, "SHORT"):
            score += 1.4; reasons.append("5m BOS aşağı")
        if l5["rsi"] < 46:
            score += 0.8; reasons.append("5m momentum negatif")
        if vf >= MIN_VOL_FACTOR:
            score += 1.1; reasons.append(f"Hacim x{vf:.2f}")
        if sweep:
            score += 1.2; reasons.append("Üst sweep + reject")
        if liq["eq_high"]:
            score += 0.5; reasons.append("Üst likidite")
            liquidity_note.append("Üst likidite kümeleri")
        if oip > 0.6:
            score += 0.9 if oip <= 1.5 else 1.3
            reasons.append(f"OI %+{oip:.2f}")
            whale_note.append(f"OI artışı %{oip:.2f}")
        if bp < -0.12:
            score += 0.6; reasons.append("Ask baskısı")
            whale_note.append("Ask tarafı baskın")
        liquidity_note.append(f"Direnç {liq['swing_high']:.2f}")

    return round(score, 2), " | ".join(reasons[:8]), " | ".join(liquidity_note), " | ".join(whale_note)

def build_setup(symbol, df15, df5, direction):
    score, reason, liquidity_note, whale_note = score_signal(symbol, df15, df5, direction)
    if score < MIN_PREP_SCORE:
        return None

    last = df5.iloc[-1]
    atrv = float(last["atr"])
    if pd.isna(atrv) or atrv <= 0:
        return None

    entry = float(last["close"])

    if direction == "LONG":
        sl = min(df5["low"].iloc[-6:-1].min(), entry - atrv * 0.9)
        risk = entry - sl
        tp1 = entry + risk * 1.2
        tp2 = entry + risk * 2.0
        tp3 = entry + risk * 3.0
    else:
        sl = max(df5["high"].iloc[-6:-1].max(), entry + atrv * 0.9)
        risk = sl - entry
        tp1 = entry - risk * 1.2
        tp2 = entry - risk * 2.0
        tp3 = entry - risk * 3.0

    if risk <= 0:
        return None

    rr = abs((tp2 - entry) / (entry - sl)) if direction == "LONG" else abs((entry - tp2) / (sl - entry))
    if rr < 1.8:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "entry": round(entry, 2),
        "entry_zone_low": round(entry * (1 - ENTRY_ZONE_TOL), 2),
        "entry_zone_high": round(entry * (1 + ENTRY_ZONE_TOL), 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "tp3": round(tp3, 2),
        "created_at": now_ts(),
        "reason": reason,
        "liquidity_note": liquidity_note,
        "whale_note": whale_note,
    }

def setup_id(setup):
    return f"{setup['symbol']}|{setup['direction']}|{setup['entry']}|{setup['sl']}"

# =========================================================
# ORDERS
# =========================================================
def set_leverage(symbol, leverage=4):
    return signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

def place_market_order(symbol, side, qty, reduce_only=False):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty,
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return signed_request("POST", "/fapi/v1/order", params)

def enter_live_trade(symbol, setup):
    global FILTERS
    if FILTERS is None:
        FILTERS = symbol_filters()

    step = FILTERS[symbol]["stepSize"]
    px = float(get_book_ticker(symbol)["askPrice"] if setup["direction"] == "LONG" else get_book_ticker(symbol)["bidPrice"])
    notional = POSITION_USDT * LEVERAGE
    qty = round_qty(notional / px, step)
    if qty <= 0:
        raise ValueError("qty <= 0")

    set_leverage(symbol, LEVERAGE)

    side = "BUY" if setup["direction"] == "LONG" else "SELL"
    res = place_market_order(symbol, side, qty, reduce_only=False)
    return qty, res

def close_live_trade(symbol, direction, qty):
    side = "SELL" if direction == "LONG" else "BUY"
    return place_market_order(symbol, side, qty, reduce_only=True)

# =========================================================
# MESSAGES
# =========================================================
def msg_prepare(s):
    e = "🟢" if s["direction"] == "LONG" else "🔴"
    return (
        f"{e} <b>{s['symbol']} {s['direction']} HAZIRLIK</b>\n"
        f"Skor: <b>{s['score']}/10</b>\n"
        f"Kaldıraç: <b>Max {LEVERAGE}x</b>\n"
        f"Giriş bölgesi: <b>{s['entry_zone_low']} - {s['entry_zone_high']}</b>\n"
        f"Referans giriş: <b>{s['entry']}</b>\n"
        f"SL: <b>{s['sl']}</b>\n"
        f"TP1: <b>{s['tp1']}</b>\n"
        f"TP2: <b>{s['tp2']}</b>\n"
        f"TP3: <b>{s['tp3']}</b>\n"
        f"Analiz: {s['reason']}\n"
        f"Likidite: {s['liquidity_note']}\n"
        f"Flow/Balina: {s['whale_note']}\n"
        f"Durum: Entry teyidi bekleniyor."
    )

def msg_enter(s, px, live=False):
    tag = "🚀" if s["direction"] == "LONG" else "⚡"
    mode = "CANLI EMİR AÇILDI" if live else "İŞLEME GİR"
    return (
        f"{tag} <b>{s['symbol']} {s['direction']} {mode}</b>\n"
        f"Anlık fiyat: <b>{px:.2f}</b>\n"
        f"Giriş bölgesi: <b>{s['entry_zone_low']} - {s['entry_zone_high']}</b>\n"
        f"SL: <b>{s['sl']}</b>\n"
        f"TP1: <b>{s['tp1']}</b>\n"
        f"TP2: <b>{s['tp2']}</b>\n"
        f"TP3: <b>{s['tp3']}</b>\n"
        f"Skor: <b>{s['score']}/10</b>"
    )

def msg_cancel(symbol, reason):
    return f"❌ <b>{symbol} SETUP İPTAL</b>\nNeden: <b>{reason}</b>"

def msg_tp(symbol, label, px):
    return f"✅ <b>{symbol} {label}</b>\nFiyat: <b>{px:.2f}</b>"

def msg_stop(symbol, px):
    return f"🛑 <b>{symbol} STOP</b>\nFiyat: <b>{px:.2f}</b>"

# =========================================================
# ENGINE
# =========================================================
def update_oi(symbol):
    try:
        oi = get_open_interest(symbol)
        hist = state[symbol]["oi_hist"]
        hist.append(oi)
        if len(hist) > 12:
            hist[:] = hist[-12:]
    except Exception as e:
        print(symbol, "OI error:", e)

def monitor_in_position(symbol, price):
    st = state[symbol]
    s = st["setup"]
    if not s:
        state[symbol] = default_symbol_state()
        return

    direction = s["direction"]

    if direction == "LONG":
        if (not st["tp1_hit"]) and price >= s["tp1"]:
            st["tp1_hit"] = True
            tg(msg_tp(symbol, "TP1", price))
        if (not st["tp2_hit"]) and price >= s["tp2"]:
            st["tp2_hit"] = True
            tg(msg_tp(symbol, "TP2", price))
        if price >= s["tp3"]:
            if MODE == "LIVE" and st["qty"] > 0:
                close_live_trade(symbol, direction, st["qty"])
            tg(msg_tp(symbol, "TP3 / TAMAM", price))
            state[symbol] = default_symbol_state()
        elif price <= s["sl"]:
            if MODE == "LIVE" and st["qty"] > 0:
                close_live_trade(symbol, direction, st["qty"])
            tg(msg_stop(symbol, price))
            state[symbol] = default_symbol_state()

    else:
        if (not st["tp1_hit"]) and price <= s["tp1"]:
            st["tp1_hit"] = True
            tg(msg_tp(symbol, "TP1", price))
        if (not st["tp2_hit"]) and price <= s["tp2"]:
            st["tp2_hit"] = True
            tg(msg_tp(symbol, "TP2", price))
        if price <= s["tp3"]:
            if MODE == "LIVE" and st["qty"] > 0:
                close_live_trade(symbol, direction, st["qty"])
            tg(msg_tp(symbol, "TP3 / TAMAM", price))
            state[symbol] = default_symbol_state()
        elif price >= s["sl"]:
            if MODE == "LIVE" and st["qty"] > 0:
                close_live_trade(symbol, direction, st["qty"])
            tg(msg_stop(symbol, price))
            state[symbol] = default_symbol_state()

def analyze_symbol(symbol):
    update_oi(symbol)

    df15 = add_indicators(get_klines(symbol, TREND_TF))
    df5 = add_indicators(get_klines(symbol, ENTRY_TF))

    trend = trend_15m(df15)
    price = float(df5.iloc[-1]["close"])
    st = state[symbol]
    s = st["setup"]

    if st["phase"].startswith("IN_"):
        monitor_in_position(symbol, price)
        return

    if st["phase"].startswith("PREPARE") and s:
        if mins_since(s["created_at"]) > SETUP_TTL_MIN:
            tg(msg_cancel(symbol, "Setup süresi doldu"))
            state[symbol] = default_symbol_state()
            state[symbol]["last_alert_ts"] = now_ts()
            return

        if trend != s["direction"]:
            tg(msg_cancel(symbol, "15m trend bozuldu"))
            state[symbol] = default_symbol_state()
            state[symbol]["last_alert_ts"] = now_ts()
            return

        score_now, _, _, _ = score_signal(symbol, df15, df5, s["direction"])
        if s["entry_zone_low"] <= price <= s["entry_zone_high"] and score_now >= MIN_ENTER_SCORE:
            if MODE == "LIVE":
                try:
                    qty, _ = enter_live_trade(symbol, s)
                    st["qty"] = qty
                    tg(msg_enter(s, price, live=True))
                except Exception as e:
                    tg(f"❌ <b>{symbol}</b> canlı emir hatası: <b>{str(e)}</b>")
                    state[symbol] = default_symbol_state()
                    state[symbol]["last_alert_ts"] = now_ts()
                    return
            else:
                tg(msg_enter(s, price, live=False))

            st["phase"] = f"IN_{s['direction']}"
            st["entry_price"] = price
            st["last_alert_ts"] = now_ts()
        return

    if trend not in ("LONG", "SHORT"):
        return
    if in_cooldown(symbol):
        return

    setup = build_setup(symbol, df15, df5, trend)
    if not setup:
        return

    sid = setup_id(setup)
    if sid == st["last_setup_id"]:
        return

    tg(msg_prepare(setup))
    st["phase"] = f"PREPARE_{setup['direction']}"
    st["last_alert_ts"] = now_ts()
    st["last_setup_id"] = sid
    st["setup"] = setup
    st["entry_price"] = 0.0
    st["qty"] = 0.0
    st["tp1_hit"] = False
    st["tp2_hit"] = False

# =========================================================
# MAIN
# =========================================================
def main():
    load_state()
    tg(
        f"🤖 <b>BTCUSDT / ETHUSDT Bot başladı</b>\n"
        f"Mod: <b>{MODE}</b>\n"
        f"Ortam: <b>{'TESTNET' if USE_TESTNET else 'LIVE'}</b>\n"
        f"Strateji: <b>15m trend + 5m entry + OI + likidite + sweep + hacim</b>\n"
        f"Kaldıraç: <b>Max {LEVERAGE}x</b>"
    )

    while True:
        try:
            for symbol in SYMBOLS:
                try:
                    analyze_symbol(symbol)
                    save_state()
                except Exception as e:
                    print(symbol, "analyze error:", e)
            time.sleep(LOOP_SECONDS)
        except KeyboardInterrupt:
            save_state()
            break
        except Exception as e:
            print("main loop error:", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
