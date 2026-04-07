# =========================================================
# ETH + USDT.D PRO SIGNAL BOT V4
# MEXC REST + CoinGecko
# Railway friendly / signal bot only
# =========================================================

import os
import time
import json
import traceback
from datetime import datetime, timezone

import requests

# =========================================================
# ENV
# =========================================================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# =========================================================
# CONFIG
# =========================================================
MEXC_BASE = "https://api.mexc.com"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"

SYMBOL = "ETHUSDT"
TREND_TF = "15m"
ENTRY_TF = "5m"

HTTP_TIMEOUT = 15
CHECK_EVERY_SECONDS = 30

STATE_FILE = "eth_usdtd_v4_state.json"
LOG_PREFIX = "[ETH-USDTD-V4]"

# ---- indicator settings
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
BREAKOUT_LOOKBACK = 15

# ---- filters (V4 = hafif agresif)
MIN_RSI_15M = 51.0
MIN_RSI_5M = 49.0
MIN_ATR_PCT = 0.0018
MAX_ATR_PCT = 0.0220
MAX_SPREAD_PCT = 0.18
MIN_15M_VOL_RATIO = 0.88
ENTRY_BUFFER_PCT = 0.0008
MAX_DISTANCE_FROM_EMA20_ATR = 2.8

# ---- usdt.d rolling filter
USDTD_HISTORY_MAX = 800
USDTD_MIN_POINTS_FOR_SIGNAL = 6
USDTD_LOOKBACK_SHORT_MIN = 10
USDTD_LOOKBACK_LONG_MIN = 45
MAX_USDTD_SHORT_RISE = 0.045
MAX_USDTD_LONG_RISE = 0.120
GOOD_USDTD_DROP = -0.020

# ---- trade / lifecycle
SIGNAL_COOLDOWN_MIN = 35
MAX_TRADE_AGE_HOURS = 18
HEARTBEAT_MIN = 180

# ---- targets
SL_ATR = 1.10
TP1_ATR = 1.20
TP2_ATR = 2.20
TP3_ATR = 3.40
MIN_RR_TO_TP2 = 1.45

# =========================================================
# SESSION
# =========================================================
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

# =========================================================
# RUNTIME STATE
# =========================================================
ACTIVE_TRADE = None
LAST_SIGNAL_TS = 0
LAST_HEARTBEAT_TS = 0
USDTD_HISTORY = []

# =========================================================
# UTILS
# =========================================================
def now_ts():
    return int(time.time())

def utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg):
    print(f"{LOG_PREFIX} {datetime.now().strftime('%H:%M:%S')} | {msg}", flush=True)

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def round_price(x):
    x = float(x)
    if x >= 1000:
        return round(x, 2)
    if x >= 100:
        return round(x, 3)
    if x >= 1:
        return round(x, 4)
    return round(x, 6)

def save_state():
    state = {
        "active_trade": ACTIVE_TRADE,
        "last_signal_ts": LAST_SIGNAL_TS,
        "last_heartbeat_ts": LAST_HEARTBEAT_TS,
        "usdtd_history": USDTD_HISTORY[-USDTD_HISTORY_MAX:],
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_state():
    global ACTIVE_TRADE, LAST_SIGNAL_TS, LAST_HEARTBEAT_TS, USDTD_HISTORY
    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        ACTIVE_TRADE = state.get("active_trade")
        LAST_SIGNAL_TS = int(state.get("last_signal_ts", 0))
        LAST_HEARTBEAT_TS = int(state.get("last_heartbeat_ts", 0))
        USDTD_HISTORY = state.get("usdtd_history", [])
        log("State yüklendi.")
    except Exception as e:
        log(f"State load hata: {e}")

def tg(msg):
    if not TOKEN or not CHAT_ID:
        log("Telegram env eksik.")
        return False
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=HTTP_TIMEOUT
        )
        ok = r.status_code == 200 and r.json().get("ok") is True
        if not ok:
            log(f"Telegram hata: {r.status_code} | {r.text[:300]}")
        return ok
    except Exception as e:
        log(f"Telegram exception: {e}")
        return False

# =========================================================
# HTTP / API
# =========================================================
def http_get(url, params=None):
    r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

def mexc_ping():
    return http_get(f"{MEXC_BASE}/api/v3/ping")

def mexc_server_time():
    return http_get(f"{MEXC_BASE}/api/v3/time")

def mexc_klines(symbol, interval, limit=300):
    return http_get(
        f"{MEXC_BASE}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit}
    )

def mexc_price(symbol):
    data = http_get(f"{MEXC_BASE}/api/v3/ticker/price", params={"symbol": symbol})
    return safe_float(data["price"])

def mexc_book_ticker(symbol):
    return http_get(f"{MEXC_BASE}/api/v3/ticker/bookTicker", params={"symbol": symbol})

def get_usdt_d():
    data = http_get(COINGECKO_GLOBAL_URL)
    return safe_float(data["data"]["market_cap_percentage"]["usdt"])

# =========================================================
# INDICATORS
# =========================================================
def closes(kl):
    return [safe_float(x[4]) for x in kl]

def highs(kl):
    return [safe_float(x[2]) for x in kl]

def lows(kl):
    return [safe_float(x[3]) for x in kl]

def volumes(kl):
    return [safe_float(x[5]) for x in kl]

def ema(vals, period):
    if len(vals) < period:
        return None
    k = 2 / (period + 1)
    e = vals[0]
    for v in vals:
        e = v * k + e * (1 - k)
    return e

def rsi(vals, period=14):
    if len(vals) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(vals)):
        diff = vals[i] - vals[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def atr(kl, period=14):
    if len(kl) < period + 1:
        return None
    trs = []
    for i in range(1, len(kl)):
        h = safe_float(kl[i][2])
        l = safe_float(kl[i][3])
        pc = safe_float(kl[i - 1][4])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period

# =========================================================
# USDT.D MEMORY
# =========================================================
def update_usdtd_history():
    global USDTD_HISTORY
    try:
        val = get_usdt_d()
        ts = now_ts()

        if USDTD_HISTORY and abs(ts - USDTD_HISTORY[-1]["ts"]) < 20:
            USDTD_HISTORY[-1] = {"ts": ts, "value": val}
        else:
            USDTD_HISTORY.append({"ts": ts, "value": val})

        if len(USDTD_HISTORY) > USDTD_HISTORY_MAX:
            USDTD_HISTORY = USDTD_HISTORY[-USDTD_HISTORY_MAX:]
    except Exception as e:
        log(f"USDT.D update hata: {e}")

def get_history_value_minutes_ago(minutes):
    if not USDTD_HISTORY:
        return None
    target = now_ts() - (minutes * 60)
    for x in reversed(USDTD_HISTORY):
        if x["ts"] <= target:
            return x["value"]
    return None

def usdtd_bias():
    current = USDTD_HISTORY[-1]["value"] if USDTD_HISTORY else None

    if current is None or len(USDTD_HISTORY) < USDTD_MIN_POINTS_FOR_SIGNAL:
        return {
            "ready": False,
            "current": current,
            "short_change": None,
            "long_change": None,
            "bullish_ok": False,
            "strong_bullish": False
        }

    short_old = get_history_value_minutes_ago(USDTD_LOOKBACK_SHORT_MIN)
    long_old = get_history_value_minutes_ago(USDTD_LOOKBACK_LONG_MIN)

    short_change = None if short_old is None else current - short_old
    long_change = None if long_old is None else current - long_old

    bullish_ok = True
    strong_bullish = False

    if short_change is not None and short_change > MAX_USDTD_SHORT_RISE:
        bullish_ok = False
    if long_change is not None and long_change > MAX_USDTD_LONG_RISE:
        bullish_ok = False
    if long_change is not None and long_change <= GOOD_USDTD_DROP:
        strong_bullish = True

    return {
        "ready": True,
        "current": round(current, 4),
        "short_change": None if short_change is None else round(short_change, 4),
        "long_change": None if long_change is None else round(long_change, 4),
        "bullish_ok": bullish_ok,
        "strong_bullish": strong_bullish
    }

# =========================================================
# ANALYSIS
# =========================================================
def get_spread_pct():
    try:
        book = mexc_book_ticker(SYMBOL)
        bid = safe_float(book["bidPrice"])
        ask = safe_float(book["askPrice"])
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        mid = (bid + ask) / 2.0
        return ((ask - bid) / mid) * 100.0
    except Exception as e:
        log(f"Spread hata: {e}")
        return None

def analyze_market():
    kl15 = mexc_klines(SYMBOL, TREND_TF, 300)
    kl5 = mexc_klines(SYMBOL, ENTRY_TF, 300)

    c15 = closes(kl15)
    h15 = highs(kl15)
    v15 = volumes(kl15)

    c5 = closes(kl5)

    last15 = c15[-1]
    last5 = c5[-1]

    ema20_15 = ema(c15[-EMA_FAST * 3:], EMA_FAST)
    ema50_15 = ema(c15[-EMA_MID * 3:], EMA_MID)
    ema200_15 = ema(c15[-EMA_SLOW * 2:], EMA_SLOW)

    ema20_5 = ema(c5[-EMA_FAST * 3:], EMA_FAST)
    ema50_5 = ema(c5[-EMA_MID * 3:], EMA_MID)

    rsi15 = rsi(c15[-120:], RSI_PERIOD)
    rsi5 = rsi(c5[-120:], RSI_PERIOD)

    atr15 = atr(kl15[-120:], ATR_PERIOD)
    atr_pct_15 = (atr15 / last15) if atr15 and last15 else 0.0

    prev_hh = max(h15[-(BREAKOUT_LOOKBACK + 1):-1])
    breakout_ok = last15 > prev_hh * (1 + ENTRY_BUFFER_PCT)

    trend_ok = (
        ema20_15 and ema50_15 and ema200_15 and
        last15 > ema20_15 > ema50_15 > ema200_15
    )

    early_trend_ok = (
        ema20_15 and ema50_15 and ema200_15 and
        last15 > ema20_15 > ema50_15 and last15 > ema200_15
    )

    entry_ok = (
        ema20_5 and ema50_5 and
        last5 > ema20_5 and ema20_5 >= ema50_5
    )

    pullback_entry_ok = (
        ema20_5 and ema50_5 and
        last5 >= ema20_5 * 0.999 and ema20_5 >= ema50_5
    )

    vol15_now = v15[-1]
    vol15_avg = sum(v15[-20:]) / 20 if len(v15) >= 20 else vol15_now
    vol_ok = vol15_now >= (vol15_avg * MIN_15M_VOL_RATIO)

    spread_pct = get_spread_pct()
    spread_ok = spread_pct is not None and spread_pct <= MAX_SPREAD_PCT

    distance_atr = 999.0
    if atr15 and ema20_15:
        distance_atr = abs(last15 - ema20_15) / atr15
    not_extended = distance_atr <= MAX_DISTANCE_FROM_EMA20_ATR

    score = 0
    if trend_ok: score += 3
    elif early_trend_ok: score += 2

    if breakout_ok: score += 2
    if entry_ok: score += 2
    elif pullback_entry_ok: score += 1

    if rsi15 and rsi15 >= MIN_RSI_15M: score += 2
    if rsi5 and rsi5 >= MIN_RSI_5M: score += 1
    if vol_ok: score += 1
    if spread_ok: score += 1
    if not_extended: score += 1
    if MIN_ATR_PCT <= atr_pct_15 <= MAX_ATR_PCT: score += 1

    aggressive_ok = (
        early_trend_ok and
        (entry_ok or pullback_entry_ok) and
        spread_ok and
        vol_ok and
        not_extended and
        rsi15 is not None and rsi15 >= MIN_RSI_15M and
        rsi5 is not None and rsi5 >= MIN_RSI_5M
    )

    return {
        "price": last5,
        "trend_ok": trend_ok,
        "early_trend_ok": early_trend_ok,
        "entry_ok": entry_ok,
        "pullback_entry_ok": pullback_entry_ok,
        "breakout_ok": breakout_ok,
        "vol_ok": vol_ok,
        "spread_ok": spread_ok,
        "not_extended": not_extended,
        "score": score,
        "rsi15": rsi15,
        "rsi5": rsi5,
        "atr15": atr15,
        "atr_pct_15": atr_pct_15,
        "spread_pct": spread_pct,
        "aggressive_ok": aggressive_ok
    }

def build_signal():
    global LAST_SIGNAL_TS

    if now_ts() - LAST_SIGNAL_TS < SIGNAL_COOLDOWN_MIN * 60:
        return None

    us = usdtd_bias()
    if not us["ready"]:
        return None
    if not us["bullish_ok"]:
        return None

    m = analyze_market()

    standard_ok = (
        m["score"] >= 9 and
        m["spread_ok"] and
        m["vol_ok"] and
        (m["trend_ok"] or m["early_trend_ok"]) and
        (m["entry_ok"] or m["pullback_entry_ok"])
    )

    aggressive_ok = (
        m["score"] >= 8 and
        m["aggressive_ok"] and
        us["strong_bullish"]
    )

    if not (standard_ok or aggressive_ok):
        return None

    entry = m["price"]
    atrv = m["atr15"] or (entry * 0.006)

    sl = entry - (atrv * SL_ATR)
    tp1 = entry + (atrv * TP1_ATR)
    tp2 = entry + (atrv * TP2_ATR)
    tp3 = entry + (atrv * TP3_ATR)

    rr_tp2 = (tp2 - entry) / max(entry - sl, 1e-9)
    if rr_tp2 < MIN_RR_TO_TP2:
        return None

    mode = "AGRESIF" if aggressive_ok and not standard_ok else "STANDART"

    return {
        "symbol": SYMBOL,
        "side": "LONG",
        "entry": round_price(entry),
        "sl": round_price(sl),
        "tp1": round_price(tp1),
        "tp2": round_price(tp2),
        "tp3": round_price(tp3),
        "atr": round_price(atrv),
        "score": m["score"],
        "mode": mode,
        "rsi15": round(m["rsi15"], 2) if m["rsi15"] is not None else None,
        "rsi5": round(m["rsi5"], 2) if m["rsi5"] is not None else None,
        "spread_pct": round(m["spread_pct"], 4) if m["spread_pct"] is not None else None,
        "usdtd": us,
        "created_ts": now_ts(),
        "created_at": utc_now_str(),
        "status": "OPEN",
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "closed_reason": None,
        "highest_seen": round_price(entry),
        "lowest_seen": round_price(entry)
    }

# =========================================================
# TELEGRAM MESSAGES
# =========================================================
def startup_message():
    return (
        f"<b>🟢 BOT AKTİF V4</b>\n\n"
        f"<b>Coin:</b> {SYMBOL}\n"
        f"<b>Trend TF:</b> {TREND_TF}\n"
        f"<b>Entry TF:</b> {ENTRY_TF}\n"
        f"<b>Kaynak:</b> MEXC REST + CoinGecko\n"
        f"<b>Zaman:</b> {utc_now_str()}\n\n"
        f"Bot uygun setup bekliyor."
    )

def heartbeat_message():
    return (
        f"<b>💓 BOT CANLI</b>\n\n"
        f"<b>Coin:</b> {SYMBOL}\n"
        f"<b>Zaman:</b> {utc_now_str()}\n"
        f"<b>Durum:</b> {'Aktif trade var' if ACTIVE_TRADE else 'Yeni setup bekleniyor'}"
    )

def signal_message(t):
    u = t["usdtd"]
    us_txt = "POZİTİF" if u["bullish_ok"] else "NEGATİF"
    if u["strong_bullish"]:
        us_txt += " / GÜÇLÜ"

    return (
        f"<b>🚀 ETH LONG SIGNAL V4</b>\n\n"
        f"<b>Coin:</b> {t['symbol']}\n"
        f"<b>Yön:</b> {t['side']}\n"
        f"<b>Mod:</b> {t['mode']}\n"
        f"<b>Giriş:</b> {t['entry']}\n"
        f"<b>Stop:</b> {t['sl']}\n\n"
        f"<b>TP1:</b> {t['tp1']}\n"
        f"<b>TP2:</b> {t['tp2']}\n"
        f"<b>TP3:</b> {t['tp3']}\n\n"
        f"<b>Skor:</b> {t['score']}/13\n"
        f"<b>RSI 15m:</b> {t['rsi15']}\n"
        f"<b>RSI 5m:</b> {t['rsi5']}\n"
        f"<b>Spread:</b> %{t['spread_pct']}\n"
        f"<b>ATR:</b> {t['atr']}\n\n"
        f"<b>USDT.D:</b> {u['current']}%\n"
        f"<b>10dk değişim:</b> {u['short_change']}\n"
        f"<b>45dk değişim:</b> {u['long_change']}\n"
        f"<b>Filtre:</b> {us_txt}\n\n"
        f"<b>Plan:</b> TP1'de stop entry, TP2'de stop TP1, TP3'te full kapanış.\n"
        f"<b>Zaman:</b> {t['created_at']}"
    )

def tp1_message(t, p):
    return (
        f"<b>✅ TP1 GELDİ</b>\n\n"
        f"<b>Coin:</b> {t['symbol']}\n"
        f"<b>Fiyat:</b> {round_price(p)}\n"
        f"<b>TP1:</b> {t['tp1']}\n"
        f"<b>Yeni Stop:</b> {t['sl']}\n\n"
        f"Stop artık <b>ENTRY</b> seviyesine çekildi.\n"
        f"Trade açık, TP2 ve TP3 takip ediliyor."
    )

def tp2_message(t, p):
    return (
        f"<b>🔥 TP2 GELDİ</b>\n\n"
        f"<b>Coin:</b> {t['symbol']}\n"
        f"<b>Fiyat:</b> {round_price(p)}\n"
        f"<b>TP2:</b> {t['tp2']}\n"
        f"<b>Yeni Stop:</b> {t['sl']}\n\n"
        f"Stop artık <b>TP1</b> seviyesine çekildi.\n"
        f"Trade açık, son hedef TP3."
    )

def tp3_message(t, p):
    return (
        f"<b>🏆 TP3 GELDİ / FULL KAPANIŞ</b>\n\n"
        f"<b>Coin:</b> {t['symbol']}\n"
        f"<b>Kapanış:</b> {round_price(p)}\n"
        f"<b>Entry:</b> {t['entry']}\n"
        f"<b>TP3:</b> {t['tp3']}\n\n"
        f"Trade tam kârla kapandı."
    )

def stop_message(t, p):
    return (
        f"<b>❌ STOP ÇALIŞTI</b>\n\n"
        f"<b>Coin:</b> {t['symbol']}\n"
        f"<b>Kapanış:</b> {round_price(p)}\n"
        f"<b>Stop:</b> {t['sl']}\n"
        f"<b>Entry:</b> {t['entry']}\n\n"
        f"Trade kapandı. Sistem yeni setup bekliyor."
    )

def timeout_message(t, p):
    return (
        f"<b>⌛ SÜRE DOLDU / TRADE KAPANDI</b>\n\n"
        f"<b>Coin:</b> {t['symbol']}\n"
        f"<b>Fiyat:</b> {round_price(p)}\n"
        f"<b>Entry:</b> {t['entry']}\n\n"
        f"Trade fazla uzadı. Sistem takipten çıkardı."
    )

# =========================================================
# TRADE MGMT
# =========================================================
def manage_trade():
    global ACTIVE_TRADE

    if not ACTIVE_TRADE:
        return

    p = mexc_price(SYMBOL)

    ACTIVE_TRADE["highest_seen"] = max(ACTIVE_TRADE["highest_seen"], round_price(p))
    ACTIVE_TRADE["lowest_seen"] = min(ACTIVE_TRADE["lowest_seen"], round_price(p))

    if (not ACTIVE_TRADE["tp1_hit"]) and p >= ACTIVE_TRADE["tp1"]:
        ACTIVE_TRADE["tp1_hit"] = True
        ACTIVE_TRADE["sl"] = ACTIVE_TRADE["entry"]
        tg(tp1_message(ACTIVE_TRADE, p))
        save_state()

    if (not ACTIVE_TRADE["tp2_hit"]) and p >= ACTIVE_TRADE["tp2"]:
        ACTIVE_TRADE["tp2_hit"] = True
        ACTIVE_TRADE["sl"] = ACTIVE_TRADE["tp1"]
        tg(tp2_message(ACTIVE_TRADE, p))
        save_state()

    if (not ACTIVE_TRADE["tp3_hit"]) and p >= ACTIVE_TRADE["tp3"]:
        ACTIVE_TRADE["tp3_hit"] = True
        ACTIVE_TRADE["status"] = "CLOSED"
        ACTIVE_TRADE["closed_reason"] = "TP3"
        tg(tp3_message(ACTIVE_TRADE, p))
        ACTIVE_TRADE = None
        save_state()
        return

    if p <= ACTIVE_TRADE["sl"]:
        ACTIVE_TRADE["status"] = "CLOSED"
        ACTIVE_TRADE["closed_reason"] = "STOP"
        tg(stop_message(ACTIVE_TRADE, p))
        ACTIVE_TRADE = None
        save_state()
        return

    age_sec = now_ts() - ACTIVE_TRADE["created_ts"]
    if age_sec >= MAX_TRADE_AGE_HOURS * 3600:
        ACTIVE_TRADE["status"] = "CLOSED"
        ACTIVE_TRADE["closed_reason"] = "TIMEOUT"
        tg(timeout_message(ACTIVE_TRADE, p))
        ACTIVE_TRADE = None
        save_state()
        return

# =========================================================
# MAIN
# =========================================================
def maybe_send_heartbeat():
    global LAST_HEARTBEAT_TS
    if now_ts() - LAST_HEARTBEAT_TS >= HEARTBEAT_MIN * 60:
        tg(heartbeat_message())
        LAST_HEARTBEAT_TS = now_ts()
        save_state()

def maybe_signal():
    global ACTIVE_TRADE, LAST_SIGNAL_TS
    if ACTIVE_TRADE is not None:
        return

    sig = build_signal()
    if not sig:
        return

    ACTIVE_TRADE = sig
    LAST_SIGNAL_TS = now_ts()
    tg(signal_message(sig))
    save_state()

def warmup_usdtd():
    for _ in range(3):
        update_usdtd_history()
        save_state()
        time.sleep(2)

def main():
    global LAST_HEARTBEAT_TS

    load_state()

    try:
        mexc_ping()
        st = mexc_server_time()
        log(f"MEXC OK | serverTime={st.get('serverTime')}")
    except Exception as e:
        log(f"MEXC bağlantı hata: {e}")

    warmup_usdtd()

    tg(startup_message())
    LAST_HEARTBEAT_TS = now_ts()
    save_state()

    while True:
        try:
            update_usdtd_history()

            if ACTIVE_TRADE:
                manage_trade()
            else:
                maybe_signal()

            maybe_send_heartbeat()
            save_state()
            time.sleep(CHECK_EVERY_SECONDS)

        except KeyboardInterrupt:
            log("Bot durduruldu.")
            break
        except Exception as e:
            log(f"HATA: {e}")
            log(traceback.format_exc())
            time.sleep(15)

if __name__ == "__main__":
    main()
