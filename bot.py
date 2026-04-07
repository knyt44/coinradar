import os
import time
import json
import requests
from statistics import mean

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", ""))

STATE_FILE = "state.json"
POLL_SECONDS = 30
HTTP_TIMEOUT = 20

ETH_SYMBOL = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"

AUTO_SCAN_INTERVAL_SECONDS = 300
SIGNAL_COOLDOWN_SECONDS = 3600
CG_CACHE_TTL_SECONDS = 180

ETH_FAST_EMA = 20
ETH_SLOW_EMA = 50
BTC_FAST_EMA = 20
BTC_SLOW_EMA = 50
ATR_PERIOD = 14

MARKET_FAST_EMA = 6
MARKET_SLOW_EMA = 18
HISTORY_LIMIT = 240

CG_IDS = "bitcoin,ethereum,tether"
TRAIL_AFTER_TP2_ATR = 1.1


def log(msg):
    print(msg, flush=True)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            log(f"STATE OKUMA HATASI: {e}")
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
    state.setdefault("history", {
        "usdtd": [],
        "total": [],
        "total3": []
    })
    state.setdefault("active_trade", None)
    return state


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"STATE YAZMA HATASI: {e}")


def tg_send(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
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
    r = requests.get(url, params={"offset": offset, "timeout": 20}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates hata: {data}")
    return data


# -------------------------------------------------
# FIYAT VERISI: BINANCE -> BYBIT -> OKX FALLBACK
# -------------------------------------------------
def get_binance(symbol):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(
        url,
        params={"symbol": symbol, "interval": "30m", "limit": 220},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    closes = [float(x[4]) for x in data]
    highs = [float(x[2]) for x in data]
    lows = [float(x[3]) for x in data]
    return closes, highs, lows


def get_bybit(symbol):
    url = "https://api.bybit.com/v5/market/kline"
    r = requests.get(
        url,
        params={
            "category": "linear",
            "symbol": symbol,
            "interval": "30",
            "limit": 220
        },
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit hata: {data}")
    rows = data["result"]["list"][::-1]
    closes = [float(x[4]) for x in rows]
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    return closes, highs, lows


def get_okx(symbol):
    inst_id = symbol.replace("USDT", "-USDT-SWAP")
    url = "https://www.okx.com/api/v5/market/candles"
    r = requests.get(
        url,
        params={"instId": inst_id, "bar": "30m", "limit": 220},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX hata: {data}")
    rows = data["data"][::-1]
    closes = [float(x[4]) for x in rows]
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    return closes, highs, lows


def get_price_data(symbol):
    errors = []
    for name, fn in [
        ("Binance", get_binance),
        ("Bybit", get_bybit),
        ("OKX", get_okx),
    ]:
        try:
            closes, highs, lows = fn(symbol)
            if len(closes) < 60:
                raise RuntimeError(f"{name} veri yetersiz")
            return closes, highs, lows, name
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError("Tum veri kaynaklari basarisiz -> " + " | ".join(errors))


# -------------------------------------------------
# COINGECKO PROXY: USDT.D / TOTAL / TOTAL3
# -------------------------------------------------
def cg_get_global_total_market_cap():
    url = "https://api.coingecko.com/api/v3/global"
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return float(data["data"]["total_market_cap"]["usd"])


def cg_get_selected_market_caps():
    url = "https://api.coingecko.com/api/v3/simple/price"
    r = requests.get(
        url,
        params={
            "ids": CG_IDS,
            "vs_currencies": "usd",
            "include_market_cap": "true"
        },
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    return r.json()


def get_market_proxies(state):
    now = int(time.time())
    cache = state.get("cg_cache", {})

    if cache and now - int(cache.get("ts", 0)) < CG_CACHE_TTL_SECONDS:
        return cache

    try:
        total_mc = cg_get_global_total_market_cap()
        caps = cg_get_selected_market_caps()

        btc_mc = float(caps.get("bitcoin", {}).get("usd_market_cap", 0.0))
        eth_mc = float(caps.get("ethereum", {}).get("usd_market_cap", 0.0))
        usdt_mc = float(caps.get("tether", {}).get("usd_market_cap", 0.0))

        if total_mc <= 0 or btc_mc <= 0 or eth_mc <= 0 or usdt_mc <= 0:
            raise RuntimeError("CoinGecko verisi eksik")

        total3_mc = max(total_mc - btc_mc - eth_mc, 0.0)
        usdtd = (usdt_mc / total_mc) * 100.0

        result = {
            "ts": now,
            "total_mc": total_mc,
            "btc_mc": btc_mc,
            "eth_mc": eth_mc,
            "total3_mc": total3_mc,
            "usdt_mc": usdt_mc,
            "usdtd": usdtd,
            "source": "COINGECKO_LIVE"
        }
        state["cg_cache"] = result
        save_state(state)
        return result

    except Exception as e:
        if cache:
            stale = dict(cache)
            stale["source"] = "COINGECKO_STALE_CACHE"
            log(f"COINGECKO CACHE KULLANILIYOR: {e}")
            return stale
        raise


# -------------------------------------------------
# INDIKATORLER
# -------------------------------------------------
def ema(data, period):
    if len(data) < period:
        return data[-1]
    k = 2 / (period + 1)
    e = mean(data[:period])
    for x in data[period:]:
        e = x * k + e * (1 - k)
    return e


def true_range(high, low, prev_close):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return abs(closes[-1] - closes[-2])
    trs = []
    for i in range(1, len(closes)):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))
    return mean(trs[-period:])


def append_history(state, key, value):
    hist = state["history"].get(key, [])
    hist.append({"ts": int(time.time()), "value": float(value)})
    state["history"][key] = hist[-HISTORY_LIMIT:]


def get_hist_values(state, key):
    return [x["value"] for x in state["history"].get(key, [])]


def fmt_t(v):
    return f"{v/1_000_000_000_000:.3f}T"


# -------------------------------------------------
# ANALIZ
# -------------------------------------------------
def analyze_market(state):
    eth_closes, eth_highs, eth_lows, eth_source = get_price_data(ETH_SYMBOL)
    btc_closes, btc_highs, btc_lows, btc_source = get_price_data(BTC_SYMBOL)
    proxies = get_market_proxies(state)

    eth_price = eth_closes[-1]
    btc_price = btc_closes[-1]

    eth_ema20 = ema(eth_closes, ETH_FAST_EMA)
    eth_ema50 = ema(eth_closes, ETH_SLOW_EMA)
    btc_ema20 = ema(btc_closes, BTC_FAST_EMA)
    btc_ema50 = ema(btc_closes, BTC_SLOW_EMA)
    eth_atr = atr(eth_highs, eth_lows, eth_closes, ATR_PERIOD)

    if eth_price > eth_ema20 > eth_ema50:
        eth_trend = "GUCLU YUKARI"
    elif eth_price > eth_ema20 and eth_ema20 >= eth_ema50:
        eth_trend = "YUKARI"
    elif eth_price < eth_ema20 < eth_ema50:
        eth_trend = "GUCLU ASAGI"
    elif eth_price < eth_ema20 and eth_ema20 <= eth_ema50:
        eth_trend = "ASAGI"
    else:
        eth_trend = "KARISIK"

    if btc_price > btc_ema20 > btc_ema50:
        btc_regime = "RISK-ON"
    elif btc_price < btc_ema20 < btc_ema50:
        btc_regime = "RISK-OFF"
    else:
        btc_regime = "NOTR"

    manual_override = state.get("manual_usdtd_override")
    usdtd_now = float(manual_override) if manual_override is not None else float(proxies["usdtd"])
    total_now = float(proxies["total_mc"])
    total3_now = float(proxies["total3_mc"])

    append_history(state, "usdtd", usdtd_now)
    append_history(state, "total", total_now)
    append_history(state, "total3", total3_now)
    save_state(state)

    usdtd_values = get_hist_values(state, "usdtd")
    total_values = get_hist_values(state, "total")
    total3_values = get_hist_values(state, "total3")

    usdtd_fast = ema(usdtd_values, MARKET_FAST_EMA)
    usdtd_slow = ema(usdtd_values, MARKET_SLOW_EMA)
    total_fast = ema(total_values, MARKET_FAST_EMA)
    total_slow = ema(total_values, MARKET_SLOW_EMA)
    total3_fast = ema(total3_values, MARKET_FAST_EMA)
    total3_slow = ema(total3_values, MARKET_SLOW_EMA)

    snap = {
        "ts": int(time.time()),
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
        "total_now": total_now,
        "total_fast": total_fast,
        "total_slow": total_slow,
        "total3_now": total3_now,
        "total3_fast": total3_fast,
        "total3_slow": total3_slow,
        "eth_source": eth_source,
        "btc_source": btc_source,
        "market_source": proxies["source"]
    }
    return snap


# -------------------------------------------------
# SINYAL MOTORU
# -------------------------------------------------
def build_signal(snapshot):
    usdtd_bullish = snapshot["usdtd_fast"] < snapshot["usdtd_slow"] and snapshot["usdtd_now"] <= snapshot["usdtd_fast"]
    usdtd_bearish = snapshot["usdtd_fast"] > snapshot["usdtd_slow"] and snapshot["usdtd_now"] >= snapshot["usdtd_fast"]

    total_bullish = snapshot["total_fast"] > snapshot["total_slow"] and snapshot["total_now"] >= snapshot["total_fast"]
    total_bearish = snapshot["total_fast"] < snapshot["total_slow"] and snapshot["total_now"] <= snapshot["total_fast"]

    total3_bullish = snapshot["total3_fast"] > snapshot["total3_slow"] and snapshot["total3_now"] >= snapshot["total3_fast"]
    total3_bearish = snapshot["total3_fast"] < snapshot["total3_slow"] and snapshot["total3_now"] <= snapshot["total3_fast"]

    long_ok = (
        usdtd_bullish and
        total_bullish and
        total3_bullish and
        snapshot["eth_trend"] in ("YUKARI", "GUCLU YUKARI") and
        snapshot["btc_regime"] in ("RISK-ON", "NOTR")
    )

    short_ok = (
        usdtd_bearish and
        total_bearish and
        total3_bearish and
        snapshot["eth_trend"] in ("ASAGI", "GUCLU ASAGI") and
        snapshot["btc_regime"] in ("RISK-OFF", "NOTR")
    )

    if not long_ok and not short_ok:
        return None

    side = "LONG" if long_ok else "SHORT"
    entry = snapshot["eth_price"]
    a = snapshot["eth_atr"]

    if side == "LONG":
        stop = entry - 1.25 * a
        tp1 = entry + 1.10 * a
        tp2 = entry + 2.00 * a
        tp3 = entry + 3.00 * a
        reason = "USDT zayif + TOTAL/TOTAL3 guclu + ETH yukari"
    else:
        stop = entry + 1.25 * a
        tp1 = entry - 1.10 * a
        tp2 = entry - 2.00 * a
        tp3 = entry - 3.00 * a
        reason = "USDT guclu + TOTAL/TOTAL3 zayif + ETH asagi"

    rr = abs(tp2 - entry) / max(abs(entry - stop), 1e-9)
    if rr < 1.2:
        return None

    signal_hash = f"{side}|{round(entry,2)}|{round(stop,2)}|{round(snapshot['usdtd_now'],4)}"

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
    now_ts = int(time.time())
    if state.get("last_signal_hash") == signal["signal_hash"]:
        return False
    if state.get("last_signal_side") == signal["side"]:
        if now_ts - int(state.get("last_signal_ts", 0)) < SIGNAL_COOLDOWN_SECONDS:
            return False
    return True


# -------------------------------------------------
# AKTIF ISLEM TAKIBI
# -------------------------------------------------
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
        "opened_ts": int(time.time()),
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
        f"ETH: <b>{snapshot['eth_price']:.2f}</b>\n"
        f"ETH Trend: <b>{snapshot['eth_trend']}</b>\n"
        f"BTC Rejimi: <b>{snapshot['btc_regime']}</b>\n"
        f"USDT.D: <b>{snapshot['usdtd_now']:.3f}</b>\n"
        f"TOTAL: <b>{fmt_t(snapshot['total_now'])}</b>\n"
        f"TOTAL3: <b>{fmt_t(snapshot['total3_now'])}</b>\n\n"
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
    msgs = []

    trade["last_price"] = price

    if trade["side"] == "LONG":
        if not trade["tp1_hit"] and price >= trade["tp1"]:
            trade["tp1_hit"] = True
            msgs.append(f"✅ <b>TP1 GELDI</b>\nLONG trade\nFiyat: <b>{price:.2f}</b>")

        if trade["tp1_hit"] and not trade["breakeven_done"]:
            trade["stop"] = trade["entry"]
            trade["trail_stop"] = trade["entry"]
            trade["breakeven_done"] = True
            msgs.append(f"🛡️ <b>STOP BREAKEVEN</b>\nYeni stop: <b>{trade['stop']:.2f}</b>")

        if not trade["tp2_hit"] and price >= trade["tp2"]:
            trade["tp2_hit"] = True
            trade["trail_active"] = True
            msgs.append(f"🎯 <b>TP2 GELDI</b>\nTrailing aktif.\nFiyat: <b>{price:.2f}</b>")

        if trade["trail_active"]:
            new_trail = price - TRAIL_AFTER_TP2_ATR * atr_now
            if new_trail > trade["trail_stop"]:
                trade["trail_stop"] = new_trail
                trade["stop"] = max(trade["stop"], trade["trail_stop"])

        if not trade["tp3_hit"] and price >= trade["tp3"]:
            trade["tp3_hit"] = True
            trade["is_open"] = False
            msgs.append(f"🏁 <b>TP3 GELDI - ISLEM KAPANDI</b>\nCikis: <b>{price:.2f}</b>")

        if trade["is_open"] and price <= trade["stop"]:
            trade["is_open"] = False
            msgs.append(f"⛔ <b>STOP CALISTI</b>\nCikis: <b>{price:.2f}</b>")

    else:
        if not trade["tp1_hit"] and price <= trade["tp1"]:
            trade["tp1_hit"] = True
            msgs.append(f"✅ <b>TP1 GELDI</b>\nSHORT trade\nFiyat: <b>{price:.2f}</b>")

        if trade["tp1_hit"] and not trade["breakeven_done"]:
            trade["stop"] = trade["entry"]
            trade["trail_stop"] = trade["entry"]
            trade["breakeven_done"] = True
            msgs.append(f"🛡️ <b>STOP BREAKEVEN</b>\nYeni stop: <b>{trade['stop']:.2f}</b>")

        if not trade["tp2_hit"] and price <= trade["tp2"]:
            trade["tp2_hit"] = True
            trade["trail_active"] = True
            msgs.append(f"🎯 <b>TP2 GELDI</b>\nTrailing aktif.\nFiyat: <b>{price:.2f}</b>")

        if trade["trail_active"]:
            new_trail = price + TRAIL_AFTER_TP2_ATR * atr_now
            if new_trail < trade["trail_stop"]:
                trade["trail_stop"] = new_trail
                trade["stop"] = min(trade["stop"], trade["trail_stop"])

        if not trade["tp3_hit"] and price <= trade["tp3"]:
            trade["tp3_hit"] = True
            trade["is_open"] = False
            msgs.append(f"🏁 <b>TP3 GELDI - ISLEM KAPANDI</b>\nCikis: <b>{price:.2f}</b>")

        if trade["is_open"] and price >= trade["stop"]:
            trade["is_open"] = False
            msgs.append(f"⛔ <b>STOP CALISTI</b>\nCikis: <b>{price:.2f}</b>")

    state["active_trade"] = trade
    save_state(state)

    for m in msgs:
        try:
            tg_send(m)
        except Exception as e:
            log(f"TRADE MESAJ HATASI: {e}")

    return state


# -------------------------------------------------
# KOMUTLAR
# -------------------------------------------------
def status_text(state):
    snap = analyze_market(state)
    trade = state.get("active_trade")

    base = (
        f"<b>DURUM</b>\n\n"
        f"ETH: <b>{snap['eth_price']:.2f}</b>\n"
        f"BTC: <b>{snap['btc_price']:.2f}</b>\n"
        f"ETH EMA20 / EMA50: <b>{snap['eth_ema20']:.2f} / {snap['eth_ema50']:.2f}</b>\n"
        f"BTC EMA20 / EMA50: <b>{snap['btc_ema20']:.2f} / {snap['btc_ema50']:.2f}</b>\n"
        f"ATR: <b>{snap['eth_atr']:.2f}</b>\n"
        f"ETH Trend: <b>{snap['eth_trend']}</b>\n"
        f"BTC Rejimi: <b>{snap['btc_regime']}</b>\n\n"
        f"USDT.D: <b>{snap['usdtd_now']:.3f}</b>\n"
        f"USDT EMA6/18: <b>{snap['usdtd_fast']:.3f} / {snap['usdtd_slow']:.3f}</b>\n"
        f"TOTAL: <b>{fmt_t(snap['total_now'])}</b>\n"
        f"TOTAL3: <b>{fmt_t(snap['total3_now'])}</b>\n\n"
        f"Otomatik Tarama: <b>{'ACIK' if state.get('auto_scan_enabled') else 'KAPALI'}</b>"
    )

    if trade and trade.get("is_open"):
        base += (
            f"\n\nAktif Islem: <b>{trade['side']}</b>\n"
            f"Giris: <b>{trade['entry']:.2f}</b>\n"
            f"Stop: <b>{trade['stop']:.2f}</b>\n"
            f"TP1/TP2/TP3: <b>{trade['tp1']:.2f} / {trade['tp2']:.2f} / {trade['tp3']:.2f}</b>"
        )
    else:
        base += "\n\nAktif Islem: <b>YOK</b>"

    return base


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


def handle_command(state, text):
    text = (text or "").strip()

    if text in ("/start", "/yardim", "/help"):
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
            v = float(parts[1].replace(",", "."))
        except ValueError:
            return "Gecersiz sayi."
        state["manual_usdtd_override"] = v
        save_state(state)
        return f"✅ Manuel USDT override aktif: {v:.3f}"

    if text.startswith("/sinyal"):
        snap = analyze_market(state)
        sig = build_signal(snap)
        if not sig:
            return (
                "Su an net sinyal yok.\n\n"
                f"ETH Trend: {snap['eth_trend']}\n"
                f"BTC Rejimi: {snap['btc_regime']}\n"
                f"USDT.D: {snap['usdtd_now']:.3f}\n"
                f"TOTAL: {fmt_t(snap['total_now'])}\n"
                f"TOTAL3: {fmt_t(snap['total3_now'])}"
            )
        return format_new_signal(snap, sig)

    return "Komut taninmadi. /yardim"


# -------------------------------------------------
# OTOMATIK TARAMA
# -------------------------------------------------
def auto_scan_if_needed(state):
    if not state.get("auto_scan_enabled", True):
        return state

    now_ts = int(time.time())
    if now_ts - int(state.get("last_auto_scan_ts", 0)) < AUTO_SCAN_INTERVAL_SECONDS:
        return state

    state["last_auto_scan_ts"] = now_ts
    save_state(state)

    snap = analyze_market(state)

    state = track_active_trade(state, snap)

    trade = state.get("active_trade")
    if trade and trade.get("is_open"):
        return state

    sig = build_signal(snap)
    if sig and should_send_signal(state, sig):
        tg_send(format_new_signal(snap, sig))
        state["last_signal_ts"] = now_ts
        state["last_signal_side"] = sig["side"]
        state["last_signal_hash"] = sig["signal_hash"]
        state["active_trade"] = create_active_trade(snap, sig)
        save_state(state)

    return state


# -------------------------------------------------
# MAIN
# -------------------------------------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN bos")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID bos")

    state = load_state()

    try:
        tg_send("✅ ETH trade signal botu aktif.\nKomutlar: /yardim")
    except Exception as e:
        log(f"Baslangic mesaji hatasi: {e}")

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
                    log(f"Telegram gonderim hatasi: {e}")

                save_state(state)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log(f"ANA DONGU HATASI: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
