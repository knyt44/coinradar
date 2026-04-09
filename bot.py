import os
import time
import json
import math
import requests
from statistics import mean

# =========================================================
# AYARLAR
# =========================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()

STATE_FILE = "state.json"
HTTP_TIMEOUT = 20
POLL_SECONDS = 20
AUTO_SCAN_INTERVAL_SECONDS = 180
SIGNAL_COOLDOWN_SECONDS = 3600

ETH_SYMBOL = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"
TIMEFRAME = "30m"
KLINE_LIMIT = 220

ETH_FAST_EMA = 20
ETH_SLOW_EMA = 50
BTC_FAST_EMA = 20
BTC_SLOW_EMA = 50
USDTD_FAST_EMA = 6
USDTD_SLOW_EMA = 18
ATR_PERIOD = 14

# Risk / hedef
SL_ATR_MULT = 1.20
TP1_ATR_MULT = 1.10
TP2_ATR_MULT = 2.00
TP3_ATR_MULT = 3.10
TRAIL_AFTER_TP2_ATR = 1.05

# USDT dominance proxy cache
CG_CACHE_TTL_SECONDS = 180
USDTD_HISTORY_LIMIT = 240

# =========================================================
# YARDIMCI
# =========================================================
def log(msg):
    print(f"[LOG] {msg}", flush=True)


def now_ts():
    return int(time.time())


def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            log(f"STATE okuma hatasi: {e}")
            state = {}
    else:
        state = {}

    state.setdefault("last_update_id", 0)
    state.setdefault("auto_scan_enabled", True)
    state.setdefault("last_auto_scan_ts", 0)
    state.setdefault("last_signal_ts", 0)
    state.setdefault("last_signal_side", "")
    state.setdefault("last_signal_hash", "")
    state.setdefault("manual_usdtd_override", None)
    state.setdefault("cg_cache", {})
    state.setdefault("history", {"usdtd": []})
    state.setdefault("active_trade", None)
    return state


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"STATE yazma hatasi: {e}")


# =========================================================
# TELEGRAM
# =========================================================
def tg_send(text):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN bos")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID bos")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API hata: {data}")
    return data


def tg_updates(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    r = requests.get(
        url,
        params={"offset": offset, "timeout": 20},
        timeout=30
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates hata: {data}")
    return data


# =========================================================
# PİYASA VERİSİ
# Önce Binance, olmazsa Bybit fallback
# =========================================================
def get_binance_klines(symbol, interval=TIMEFRAME, limit=KLINE_LIMIT):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(
        url,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or len(rows) < 60:
        raise RuntimeError("Binance veri yetersiz")
    closes = [float(x[4]) for x in rows]
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    return closes, highs, lows, "BINANCE"


def get_bybit_klines(symbol, interval="30", limit=KLINE_LIMIT):
    url = "https://api.bybit.com/v5/market/kline"
    r = requests.get(
        url,
        params={
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        },
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit hata: {data}")
    rows = data["result"]["list"][::-1]
    if len(rows) < 60:
        raise RuntimeError("Bybit veri yetersiz")
    closes = [float(x[4]) for x in rows]
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    return closes, highs, lows, "BYBIT"


def get_price_data(symbol):
    errors = []

    try:
        return get_binance_klines(symbol)
    except Exception as e:
        errors.append(f"Binance: {e}")

    try:
        return get_bybit_klines(symbol)
    except Exception as e:
        errors.append(f"Bybit: {e}")

    raise RuntimeError("Fiyat verisi alinamadi -> " + " | ".join(errors))


# =========================================================
# COINGECKO -> USDT.D PROXY
# USDT.D ~= tether market cap / total market cap * 100
# =========================================================
def cg_get_global_total_market_cap():
    url = "https://api.coingecko.com/api/v3/global"
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    total_mc = safe_float(data.get("data", {}).get("total_market_cap", {}).get("usd"))
    if total_mc <= 0:
        raise RuntimeError("Global market cap alinamadi")
    return total_mc


def cg_get_tether_market_cap():
    url = "https://api.coingecko.com/api/v3/simple/price"
    r = requests.get(
        url,
        params={
            "ids": "tether",
            "vs_currencies": "usd",
            "include_market_cap": "true"
        },
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    usdt_mc = safe_float(data.get("tether", {}).get("usd_market_cap"))
    if usdt_mc <= 0:
        raise RuntimeError("Tether market cap alinamadi")
    return usdt_mc


def get_usdtd_proxy(state):
    manual = state.get("manual_usdtd_override")
    if manual is not None:
        return {
            "usdtd": float(manual),
            "source": "MANUAL_OVERRIDE",
            "ts": now_ts()
        }

    cache = state.get("cg_cache", {})
    if cache and now_ts() - int(cache.get("ts", 0)) < CG_CACHE_TTL_SECONDS:
        return cache

    try:
        total_mc = cg_get_global_total_market_cap()
        usdt_mc = cg_get_tether_market_cap()
        usdtd = (usdt_mc / total_mc) * 100.0

        result = {
            "usdtd": usdtd,
            "source": "COINGECKO_LIVE",
            "ts": now_ts()
        }
        state["cg_cache"] = result
        save_state(state)
        return result

    except Exception as e:
        if cache:
            stale = dict(cache)
            stale["source"] = "COINGECKO_STALE_CACHE"
            log(f"CoinGecko cache kullaniliyor: {e}")
            return stale
        raise RuntimeError(f"USDT.D proxy alinamadi: {e}")


# =========================================================
# İNDİKATÖRLER
# =========================================================
def ema(data, period):
    if not data:
        return 0.0
    if len(data) < period:
        return mean(data)

    k = 2 / (period + 1)
    e = mean(data[:period])
    for x in data[period:]:
        e = x * k + e * (1 - k)
    return e


def true_range(high, low, prev_close):
    return max(
        high - low,
        abs(high - prev_close),
        abs(low - prev_close)
    )


def atr(highs, lows, closes, period=14):
    if len(closes) < 3:
        return abs(closes[-1] - closes[-2]) if len(closes) >= 2 else 0.0

    trs = []
    for i in range(1, len(closes)):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))

    if not trs:
        return 0.0

    if len(trs) < period:
        return mean(trs)
    return mean(trs[-period:])


def append_usdtd_history(state, value):
    hist = state["history"].get("usdtd", [])
    hist.append({"ts": now_ts(), "value": float(value)})
    state["history"]["usdtd"] = hist[-USDTD_HISTORY_LIMIT:]
    save_state(state)


def get_usdtd_values(state):
    return [safe_float(x.get("value")) for x in state.get("history", {}).get("usdtd", [])]


# =========================================================
# ANALİZ
# =========================================================
def analyze_market(state):
    eth_closes, eth_highs, eth_lows, eth_source = get_price_data(ETH_SYMBOL)
    btc_closes, btc_highs, btc_lows, btc_source = get_price_data(BTC_SYMBOL)

    eth_price = eth_closes[-1]
    btc_price = btc_closes[-1]

    eth_ema20 = ema(eth_closes, ETH_FAST_EMA)
    eth_ema50 = ema(eth_closes, ETH_SLOW_EMA)
    btc_ema20 = ema(btc_closes, BTC_FAST_EMA)
    btc_ema50 = ema(btc_closes, BTC_SLOW_EMA)
    eth_atr = atr(eth_highs, eth_lows, eth_closes, ATR_PERIOD)

    usdtd_info = get_usdtd_proxy(state)
    usdtd_now = float(usdtd_info["usdtd"])
    append_usdtd_history(state, usdtd_now)
    usdtd_values = get_usdtd_values(state)

    usdtd_fast = ema(usdtd_values, USDTD_FAST_EMA)
    usdtd_slow = ema(usdtd_values, USDTD_SLOW_EMA)

    if eth_price > eth_ema20 > eth_ema50:
        eth_trend = "GUCLU_YUKARI"
    elif eth_price > eth_ema20 and eth_ema20 >= eth_ema50:
        eth_trend = "YUKARI"
    elif eth_price < eth_ema20 < eth_ema50:
        eth_trend = "GUCLU_ASAGI"
    elif eth_price < eth_ema20 and eth_ema20 <= eth_ema50:
        eth_trend = "ASAGI"
    else:
        eth_trend = "KARISIK"

    if btc_price > btc_ema20 > btc_ema50:
        btc_regime = "RISK_ON"
    elif btc_price < btc_ema20 < btc_ema50:
        btc_regime = "RISK_OFF"
    else:
        btc_regime = "NOTR"

    snap = {
        "ts": now_ts(),
        "eth_price": eth_price,
        "btc_price": btc_price,
        "eth_ema20": eth_ema20,
        "eth_ema50": eth_ema50,
        "btc_ema20": btc_ema20,
        "btc_ema50": btc_ema50,
        "eth_atr": eth_atr,
        "eth_trend": eth_trend,
        "btc_regime": btc_regime,
        "usdtd_now": usdtd_now,
        "usdtd_fast": usdtd_fast,
        "usdtd_slow": usdtd_slow,
        "eth_source": eth_source,
        "btc_source": btc_source,
        "usdtd_source": usdtd_info["source"]
    }
    return snap


# =========================================================
# SİNYAL MOTORU
# =========================================================
def build_signal(snapshot):
    # USDT.D düşüyorsa risk-on, yükseliyorsa risk-off
    usdtd_bullish = (
        snapshot["usdtd_fast"] < snapshot["usdtd_slow"]
        and snapshot["usdtd_now"] <= snapshot["usdtd_fast"]
    )
    usdtd_bearish = (
        snapshot["usdtd_fast"] > snapshot["usdtd_slow"]
        and snapshot["usdtd_now"] >= snapshot["usdtd_fast"]
    )

    eth_bullish = snapshot["eth_trend"] in ("YUKARI", "GUCLU_YUKARI")
    eth_bearish = snapshot["eth_trend"] in ("ASAGI", "GUCLU_ASAGI")

    btc_bullish = snapshot["btc_regime"] == "RISK_ON"
    btc_bearish = snapshot["btc_regime"] == "RISK_OFF"

    long_ok = usdtd_bullish and eth_bullish and btc_bullish
    short_ok = usdtd_bearish and eth_bearish and btc_bearish

    if not long_ok and not short_ok:
        return None

    side = "LONG" if long_ok else "SHORT"
    entry = snapshot["eth_price"]
    a = snapshot["eth_atr"]

    if a <= 0:
        return None

    if side == "LONG":
        stop = entry - SL_ATR_MULT * a
        tp1 = entry + TP1_ATR_MULT * a
        tp2 = entry + TP2_ATR_MULT * a
        tp3 = entry + TP3_ATR_MULT * a
        reason = "BTC yukari + USDT.D zayif + ETH yukari"
    else:
        stop = entry + SL_ATR_MULT * a
        tp1 = entry - TP1_ATR_MULT * a
        tp2 = entry - TP2_ATR_MULT * a
        tp3 = entry - TP3_ATR_MULT * a
        reason = "BTC asagi + USDT.D guclu + ETH asagi"

    risk = abs(entry - stop)
    reward_tp2 = abs(tp2 - entry)
    if risk <= 0:
        return None

    rr = reward_tp2 / risk
    if rr < 1.20:
        return None

    signal_hash = (
        f"{side}|"
        f"{round(entry,2)}|{round(stop,2)}|"
        f"{round(snapshot['usdtd_now'],4)}|"
        f"{snapshot['btc_regime']}|{snapshot['eth_trend']}"
    )

    return {
        "side": side,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr": rr,
        "reason": reason,
        "signal_hash": signal_hash
    }


def should_send_signal(state, signal):
    last_hash = state.get("last_signal_hash", "")
    last_side = state.get("last_signal_side", "")
    last_ts = int(state.get("last_signal_ts", 0))
    cur_ts = now_ts()

    if signal["signal_hash"] == last_hash:
        return False

    if signal["side"] == last_side and (cur_ts - last_ts) < SIGNAL_COOLDOWN_SECONDS:
        return False

    return True


# =========================================================
# AKTİF İŞLEM
# =========================================================
def create_active_trade(snapshot, signal):
    return {
        "is_open": True,
        "side": signal["side"],
        "entry": signal["entry"],
        "stop": signal["stop"],
        "tp1": signal["tp1"],
        "tp2": signal["tp2"],
        "tp3": signal["tp3"],
        "rr": signal["rr"],
        "reason": signal["reason"],
        "opened_ts": now_ts(),
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "breakeven_done": False,
        "trail_active": False,
        "trail_stop": signal["stop"],
        "last_price": snapshot["eth_price"]
    }


def format_new_signal(snapshot, signal):
    return (
        f"🔥 <b>YENI {signal['side']} SINYALI</b>\n\n"
        f"Parite: <b>{ETH_SYMBOL}</b>\n"
        f"Fiyat: <b>{snapshot['eth_price']:.2f}</b>\n"
        f"ETH Trend: <b>{snapshot['eth_trend']}</b>\n"
        f"BTC Rejim: <b>{snapshot['btc_regime']}</b>\n"
        f"USDT.D: <b>{snapshot['usdtd_now']:.3f}</b>\n"
        f"USDT EMA{USDTD_FAST_EMA}/{USDTD_SLOW_EMA}: "
        f"<b>{snapshot['usdtd_fast']:.3f} / {snapshot['usdtd_slow']:.3f}</b>\n\n"
        f"Giris: <b>{signal['entry']:.2f}</b>\n"
        f"Stop: <b>{signal['stop']:.2f}</b>\n"
        f"TP1: <b>{signal['tp1']:.2f}</b>\n"
        f"TP2: <b>{signal['tp2']:.2f}</b>\n"
        f"TP3: <b>{signal['tp3']:.2f}</b>\n"
        f"RR(TP2): <b>{signal['rr']:.2f}</b>\n\n"
        f"Sebep: <b>{signal['reason']}</b>"
    )


def format_active_trade(trade, price):
    return (
        f"<b>AKTIF ISLEM</b>\n\n"
        f"Yon: <b>{trade['side']}</b>\n"
        f"Anlik Fiyat: <b>{price:.2f}</b>\n"
        f"Giris: <b>{trade['entry']:.2f}</b>\n"
        f"Stop: <b>{trade['stop']:.2f}</b>\n"
        f"TP1: <b>{trade['tp1']:.2f}</b> ({'✓' if trade['tp1_hit'] else '-'})\n"
        f"TP2: <b>{trade['tp2']:.2f}</b> ({'✓' if trade['tp2_hit'] else '-'})\n"
        f"TP3: <b>{trade['tp3']:.2f}</b> ({'✓' if trade['tp3_hit'] else '-'})\n"
        f"Breakeven: <b>{'AKTIF' if trade['breakeven_done'] else 'PASIF'}</b>\n"
        f"Trailing: <b>{'AKTIF' if trade['trail_active'] else 'PASIF'}</b>\n"
        f"Trail Stop: <b>{trade['trail_stop']:.2f}</b>"
    )


def track_active_trade(state, snapshot):
    trade = state.get("active_trade")
    if not trade or not trade.get("is_open"):
        return state

    price = snapshot["eth_price"]
    atr_now = snapshot["eth_atr"]
    messages = []

    trade["last_price"] = price

    if trade["side"] == "LONG":
        if not trade["tp1_hit"] and price >= trade["tp1"]:
            trade["tp1_hit"] = True
            messages.append(
                f"✅ <b>TP1 GELDI</b>\n"
                f"LONG {ETH_SYMBOL}\n"
                f"Fiyat: <b>{price:.2f}</b>"
            )

        if trade["tp1_hit"] and not trade["breakeven_done"]:
            trade["stop"] = trade["entry"]
            trade["trail_stop"] = trade["entry"]
            trade["breakeven_done"] = True
            messages.append(
                f"🛡️ <b>STOP BREAKEVEN</b>\n"
                f"Yeni stop: <b>{trade['stop']:.2f}</b>"
            )

        if not trade["tp2_hit"] and price >= trade["tp2"]:
            trade["tp2_hit"] = True
            trade["trail_active"] = True
            messages.append(
                f"🎯 <b>TP2 GELDI</b>\n"
                f"Trailing aktif.\n"
                f"Fiyat: <b>{price:.2f}</b>"
            )

        if trade["trail_active"] and atr_now > 0:
            new_trail = price - TRAIL_AFTER_TP2_ATR * atr_now
            if new_trail > trade["trail_stop"]:
                trade["trail_stop"] = new_trail
                trade["stop"] = max(trade["stop"], trade["trail_stop"])

        if not trade["tp3_hit"] and price >= trade["tp3"]:
            trade["tp3_hit"] = True
            trade["is_open"] = False
            messages.append(
                f"🏁 <b>TP3 GELDI - ISLEM KAPANDI</b>\n"
                f"Cikis: <b>{price:.2f}</b>"
            )

        if trade["is_open"] and price <= trade["stop"]:
            trade["is_open"] = False
            messages.append(
                f"⛔ <b>STOP CALISTI</b>\n"
                f"Cikis: <b>{price:.2f}</b>"
            )

    else:
        if not trade["tp1_hit"] and price <= trade["tp1"]:
            trade["tp1_hit"] = True
            messages.append(
                f"✅ <b>TP1 GELDI</b>\n"
                f"SHORT {ETH_SYMBOL}\n"
                f"Fiyat: <b>{price:.2f}</b>"
            )

        if trade["tp1_hit"] and not trade["breakeven_done"]:
            trade["stop"] = trade["entry"]
            trade["trail_stop"] = trade["entry"]
            trade["breakeven_done"] = True
            messages.append(
                f"🛡️ <b>STOP BREAKEVEN</b>\n"
                f"Yeni stop: <b>{trade['stop']:.2f}</b>"
            )

        if not trade["tp2_hit"] and price <= trade["tp2"]:
            trade["tp2_hit"] = True
            trade["trail_active"] = True
            messages.append(
                f"🎯 <b>TP2 GELDI</b>\n"
                f"Trailing aktif.\n"
                f"Fiyat: <b>{price:.2f}</b>"
            )

        if trade["trail_active"] and atr_now > 0:
            new_trail = price + TRAIL_AFTER_TP2_ATR * atr_now
            if new_trail < trade["trail_stop"]:
                trade["trail_stop"] = new_trail
                trade["stop"] = min(trade["stop"], trade["trail_stop"])

        if not trade["tp3_hit"] and price <= trade["tp3"]:
            trade["tp3_hit"] = True
            trade["is_open"] = False
            messages.append(
                f"🏁 <b>TP3 GELDI - ISLEM KAPANDI</b>\n"
                f"Cikis: <b>{price:.2f}</b>"
            )

        if trade["is_open"] and price >= trade["stop"]:
            trade["is_open"] = False
            messages.append(
                f"⛔ <b>STOP CALISTI</b>\n"
                f"Cikis: <b>{price:.2f}</b>"
            )

    state["active_trade"] = trade
    save_state(state)

    for msg in messages:
        try:
            tg_send(msg)
        except Exception as e:
            log(f"Trade mesaj hatasi: {e}")

    return state


# =========================================================
# METİNLER
# =========================================================
def help_text():
    return (
        "<b>Komutlar</b>\n\n"
        "/durum\n"
        "/sinyal\n"
        "/aktif\n"
        "/otomatikac\n"
        "/otomatikkapat\n"
        "/usdtdset 7.850\n"
        "/usdtdauto\n"
        "/yardim"
    )


def status_text(state):
    snap = analyze_market(state)
    trade = state.get("active_trade")

    text = (
        f"<b>DURUM</b>\n\n"
        f"Parite: <b>{ETH_SYMBOL}</b>\n"
        f"ETH: <b>{snap['eth_price']:.2f}</b>\n"
        f"BTC: <b>{snap['btc_price']:.2f}</b>\n\n"
        f"ETH EMA20 / EMA50: <b>{snap['eth_ema20']:.2f} / {snap['eth_ema50']:.2f}</b>\n"
        f"BTC EMA20 / EMA50: <b>{snap['btc_ema20']:.2f} / {snap['btc_ema50']:.2f}</b>\n"
        f"ATR: <b>{snap['eth_atr']:.2f}</b>\n\n"
        f"ETH Trend: <b>{snap['eth_trend']}</b>\n"
        f"BTC Rejim: <b>{snap['btc_regime']}</b>\n\n"
        f"USDT.D: <b>{snap['usdtd_now']:.3f}</b>\n"
        f"USDT EMA{USDTD_FAST_EMA}/{USDTD_SLOW_EMA}: "
        f"<b>{snap['usdtd_fast']:.3f} / {snap['usdtd_slow']:.3f}</b>\n"
        f"USDT Kaynak: <b>{snap['usdtd_source']}</b>\n\n"
        f"Otomatik Tarama: <b>{'ACIK' if state.get('auto_scan_enabled') else 'KAPALI'}</b>"
    )

    if trade and trade.get("is_open"):
        text += (
            f"\n\nAktif Islem: <b>{trade['side']}</b>\n"
            f"Giris: <b>{trade['entry']:.2f}</b>\n"
            f"Stop: <b>{trade['stop']:.2f}</b>\n"
            f"TP1/TP2/TP3: <b>{trade['tp1']:.2f} / {trade['tp2']:.2f} / {trade['tp3']:.2f}</b>"
        )
    else:
        text += "\n\nAktif Islem: <b>YOK</b>"

    return text


# =========================================================
# KOMUTLAR
# =========================================================
def handle_command(state, text):
    text = (text or "").strip()

    if text in ("/start", "/help", "/yardim"):
        return help_text()

    if text.startswith("/durum"):
        return status_text(state)

    if text.startswith("/aktif"):
        snap = analyze_market(state)
        trade = state.get("active_trade")
        if trade and trade.get("is_open"):
            return format_active_trade(trade, snap["eth_price"])
        return "Aktif islem yok."

    if text.startswith("/otomatikac"):
        state["auto_scan_enabled"] = True
        save_state(state)
        return "✅ Otomatik tarama acildi."

    if text.startswith("/otomatikkapat"):
        state["auto_scan_enabled"] = False
        save_state(state)
        return "🛑 Otomatik tarama kapatildi."

    if text.startswith("/usdtdauto"):
        state["manual_usdtd_override"] = None
        save_state(state)
        return "✅ USDT tekrar otomatik moda alindi."

    if text.startswith("/usdtdset"):
        parts = text.split()
        if len(parts) != 2:
            return "Kullanim: /usdtdset 7.850"
        try:
            value = float(parts[1].replace(",", "."))
        except ValueError:
            return "Gecersiz sayi."
        state["manual_usdtd_override"] = value
        save_state(state)
        return f"✅ Manuel USDT override aktif: {value:.3f}"

    if text.startswith("/sinyal"):
        snap = analyze_market(state)
        sig = build_signal(snap)
        if not sig:
            return (
                "Su an net sinyal yok.\n\n"
                f"ETH Trend: {snap['eth_trend']}\n"
                f"BTC Rejim: {snap['btc_regime']}\n"
                f"USDT.D: {snap['usdtd_now']:.3f}\n"
                f"USDT EMA{USDTD_FAST_EMA}/{USDTD_SLOW_EMA}: "
                f"{snap['usdtd_fast']:.3f} / {snap['usdtd_slow']:.3f}"
            )
        return format_new_signal(snap, sig)

    return "Komut taninmadi. /yardim"


# =========================================================
# OTOMATİK TARAMA
# =========================================================
def auto_scan_if_needed(state):
    if not state.get("auto_scan_enabled", True):
        return state

    current_ts = now_ts()
    last_scan = int(state.get("last_auto_scan_ts", 0))

    if current_ts - last_scan < AUTO_SCAN_INTERVAL_SECONDS:
        return state

    state["last_auto_scan_ts"] = current_ts
    save_state(state)

    snap = analyze_market(state)
    state = track_active_trade(state, snap)

    trade = state.get("active_trade")
    if trade and trade.get("is_open"):
        return state

    sig = build_signal(snap)
    if sig and should_send_signal(state, sig):
        tg_send(format_new_signal(snap, sig))
        state["last_signal_ts"] = current_ts
        state["last_signal_side"] = sig["side"]
        state["last_signal_hash"] = sig["signal_hash"]
        state["active_trade"] = create_active_trade(snap, sig)
        save_state(state)

    return state


# =========================================================
# MAIN
# =========================================================
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN bos")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID bos")

    state = load_state()

    try:
        tg_send(
            "✅ ETH sade trade signal botu aktif.\n"
            "Filtre: BTC + USDT.D + ETH trend\n"
            "Komutlar: /yardim"
        )
    except Exception as e:
        log(f"Baslangic mesaji gonderilemedi: {e}")

    while True:
        try:
            state = auto_scan_if_needed(state)

            updates = tg_updates(state["last_update_id"] + 1)
            for u in updates.get("result", []):
                state["last_update_id"] = u["update_id"]

                msg = u.get("message") or u.get("edited_message") or {}
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != TELEGRAM_CHAT_ID:
                    continue

                text = msg.get("text", "")

                try:
                    reply = handle_command(state, text)
                except Exception as e:
                    reply = f"HATA:\n{str(e)}"

                try:
                    tg_send(reply)
                except Exception as e:
                    log(f"Telegram cevap gonderim hatasi: {e}")

                save_state(state)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log(f"Ana dongu hatasi: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
