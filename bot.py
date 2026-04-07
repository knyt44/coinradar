import os
import time
import json
import math
import requests
from statistics import mean

print("BOT DOSYASI YÜKLENDİ", flush=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", ""))

STATE_FILE = "state.json"
POLL_SECONDS = 20
HTTP_TIMEOUT = 20

ETH_SYMBOL = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"

# Otomatik sinyal tarama ayarları
AUTO_SCAN_ENABLED_DEFAULT = True
AUTO_SCAN_INTERVAL_SECONDS = 300   # 5 dk
SIGNAL_COOLDOWN_SECONDS = 60 * 60  # aynı yönde 1 saat tekrar sinyal verme

# Sinyal eşiği
USDTD_FAST_EMA = 6
USDTD_SLOW_EMA = 18
ETH_FAST_EMA = 20
ETH_SLOW_EMA = 50
BTC_FAST_EMA = 20
BTC_SLOW_EMA = 50
ATR_PERIOD = 14

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
    state.setdefault("auto_scan_enabled", AUTO_SCAN_ENABLED_DEFAULT)
    state.setdefault("last_auto_scan_ts", 0)
    state.setdefault("last_signal_ts", 0)
    state.setdefault("last_signal_side", "")
    state.setdefault("last_signal_hash", "")
    state.setdefault("last_snapshot", {})
    state.setdefault("manual_usdtd_override", None)
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
    log(f"TELEGRAM SEND STATUS: {r.status_code}")
    log(f"TELEGRAM SEND BODY: {r.text[:300]}")
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API hata: {data}")
    return data

def tg_updates(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    r = requests.get(url, params={"offset": offset, "timeout": 20}, timeout=30)
    log(f"GETUPDATES STATUS: {r.status_code}")
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates hata: {data}")
    return data

# -----------------------------
# EXCHANGE DATA (ETH/BTC)
# -----------------------------
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
            log(f"{symbol} veri kaynağı: {name}")
            return closes, highs, lows, name
        except Exception as e:
            err = f"{name} başarısız: {e}"
            errors.append(err)
            log(err)
    raise RuntimeError("Tüm veri kaynakları başarısız:\n" + "\n".join(errors))

# -----------------------------
# USDT DOMINANCE PROXY
# -----------------------------
def cg_get_global_total_market_cap():
    # CoinGecko public style endpoint
    url = "https://api.coingecko.com/api/v3/global"
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return float(data["data"]["total_market_cap"]["usd"])

def cg_get_usdt_market_cap():
    # ids=tether market cap
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
    return float(data["tether"]["usd_market_cap"])

def get_usdtd_proxy_current():
    total_mc = cg_get_global_total_market_cap()
    usdt_mc = cg_get_usdt_market_cap()
    if total_mc <= 0 or usdt_mc <= 0:
        raise RuntimeError("USDT dominance hesap verisi geçersiz")
    usdtd = (usdt_mc / total_mc) * 100.0
    return usdtd, usdt_mc, total_mc

def get_usdtd_proxy_series(points=24):
    # Son N noktayı yaklaşık üretmek için CoinGecko global market cap chart + tether market chart kullanılabilir.
    # Public erişimde her ortamda aynı davranmayabilir, o yüzden ilk yol:
    # mevcut değeri alıp kısa hafızada state içine saklayacağız.
    # Eğer geçmiş yoksa seri yerine sabit seri oluşturup yavaşça state ile zenginleştireceğiz.
    raise NotImplementedError

# -----------------------------
# INDICATORS
# -----------------------------
def ema(data, period):
    if len(data) < period:
        raise RuntimeError(f"EMA için veri yetersiz. period={period}")
    k = 2 / (period + 1)
    e = mean(data[:period])
    for x in data[period:]:
        e = x * k + e * (1 - k)
    return e

def true_range(high, low, prev_close):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))

def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        raise RuntimeError("ATR için veri yetersiz")
    trs = []
    for i in range(1, len(closes)):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))
    return mean(trs[-period:])

# -----------------------------
# ANALYSIS CORE
# -----------------------------
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

    if eth_price > eth_ema20 > eth_ema50:
        eth_trend = "GÜÇLÜ YUKARI"
    elif eth_price > eth_ema20 and eth_ema20 >= eth_ema50:
        eth_trend = "YUKARI"
    elif eth_price < eth_ema20 < eth_ema50:
        eth_trend = "GÜÇLÜ AŞAĞI"
    elif eth_price < eth_ema20 and eth_ema20 <= eth_ema50:
        eth_trend = "AŞAĞI"
    else:
        eth_trend = "KARIŞIK"

    if btc_price > btc_ema20 > btc_ema50:
        btc_regime = "RISK-ON"
    elif btc_price < btc_ema20 < btc_ema50:
        btc_regime = "RISK-OFF"
    else:
        btc_regime = "NÖTR"

    manual_override = state.get("manual_usdtd_override")
    if manual_override is not None:
        usdtd_now = float(manual_override)
        usdt_mc = None
        total_mc = None
        usdtd_source = "MANUEL"
    else:
        usdtd_now, usdt_mc, total_mc = get_usdtd_proxy_current()
        usdtd_source = "COINGECKO_PROXY"

    snapshot = state.get("last_snapshot", {})
    history = snapshot.get("usdtd_history", [])
    history.append({
        "ts": int(time.time()),
        "value": round(usdtd_now, 6)
    })
    # son 120 nokta kalsın
    history = history[-120:]

    values = [x["value"] for x in history]
    if len(values) >= USDTD_SLOW_EMA:
        usdtd_ema_fast = ema(values, USDTD_FAST_EMA)
        usdtd_ema_slow = ema(values, USDTD_SLOW_EMA)
    else:
        usdtd_ema_fast = usdtd_now
        usdtd_ema_slow = usdtd_now

    state["last_snapshot"] = {
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
        "usdtd_source": usdtd_source,
        "usdtd_ema_fast": usdtd_ema_fast,
        "usdtd_ema_slow": usdtd_ema_slow,
        "usdt_mc": usdt_mc,
        "total_mc": total_mc,
        "eth_source": eth_source,
        "btc_source": btc_source,
        "usdtd_history": history
    }
    save_state(state)
    return state["last_snapshot"]

# -----------------------------
# SIGNAL ENGINE
# -----------------------------
def build_signal(snapshot):
    eth_price = snapshot["eth_price"]
    eth_trend = snapshot["eth_trend"]
    btc_regime = snapshot["btc_regime"]
    eth_atr = snapshot["eth_atr"]

    usdtd_now = snapshot["usdtd_now"]
    usdtd_fast = snapshot["usdtd_ema_fast"]
    usdtd_slow = snapshot["usdtd_ema_slow"]

    usdtd_bearish_for_crypto = usdtd_fast > usdtd_slow and usdtd_now >= usdtd_fast
    usdtd_bullish_for_crypto = usdtd_fast < usdtd_slow and usdtd_now <= usdtd_fast

    long_ok = (
        usdtd_bullish_for_crypto and
        eth_trend in ("YUKARI", "GÜÇLÜ YUKARI") and
        btc_regime in ("RISK-ON", "NÖTR")
    )

    short_ok = (
        usdtd_bearish_for_crypto and
        eth_trend in ("AŞAĞI", "GÜÇLÜ AŞAĞI") and
        btc_regime in ("RISK-OFF", "NÖTR")
    )

    if not long_ok and not short_ok:
        return None

    if long_ok:
        side = "LONG"
        entry = eth_price
        stop = eth_price - 1.25 * eth_atr
        tp1 = eth_price + 1.10 * eth_atr
        tp2 = eth_price + 2.00 * eth_atr
        tp3 = eth_price + 3.00 * eth_atr
        reason = "USDT payı zayıflıyor + ETH yukarı yapıda"
    else:
        side = "SHORT"
        entry = eth_price
        stop = eth_price + 1.25 * eth_atr
        tp1 = eth_price - 1.10 * eth_atr
        tp2 = eth_price - 2.00 * eth_atr
        tp3 = eth_price - 3.00 * eth_atr
        reason = "USDT payı güçleniyor + ETH aşağı yapıda"

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
    if not signal:
        return False

    if state.get("last_signal_hash") == signal["signal_hash"]:
        return False

    if state.get("last_signal_side") == signal["side"]:
        if now_ts - int(state.get("last_signal_ts", 0)) < SIGNAL_COOLDOWN_SECONDS:
            return False

    return True

def format_signal(snapshot, signal):
    return (
        f"🔥 <b>OTOMATİK {signal['side']} SİNYALİ</b>\n\n"
        f"ETH: <b>{snapshot['eth_price']:.2f}</b>\n"
        f"ETH Trend: <b>{snapshot['eth_trend']}</b>\n"
        f"BTC Rejimi: <b>{snapshot['btc_regime']}</b>\n"
        f"USDT Payı: <b>{snapshot['usdtd_now']:.3f}</b>\n"
        f"USDT EMA{USDTD_FAST_EMA}/{USDTD_SLOW_EMA}: <b>{snapshot['usdtd_ema_fast']:.3f} / {snapshot['usdtd_ema_slow']:.3f}</b>\n"
        f"Kaynaklar: <b>ETH={snapshot['eth_source']} | BTC={snapshot['btc_source']} | USDT={snapshot['usdtd_source']}</b>\n\n"
        f"Sebep: <b>{signal['reason']}</b>\n\n"
        f"Giriş: <b>{signal['entry']:.2f}</b>\n"
        f"Stop: <b>{signal['stop']:.2f}</b>\n"
        f"TP1: <b>{signal['tp1']:.2f}</b>\n"
        f"TP2: <b>{signal['tp2']:.2f}</b>\n"
        f"TP3: <b>{signal['tp3']:.2f}</b>\n"
        f"RR(TP2): <b>{signal['rr']:.2f}</b>"
    )

# -----------------------------
# TEXT OUTPUTS
# -----------------------------
def status_text(state):
    snap = analyze_market(state)
    return (
        f"<b>DURUM</b>\n\n"
        f"ETH: <b>{snap['eth_price']:.2f}</b>\n"
        f"BTC: <b>{snap['btc_price']:.2f}</b>\n"
        f"ETH EMA20 / EMA50: <b>{snap['eth_ema20']:.2f} / {snap['eth_ema50']:.2f}</b>\n"
        f"BTC EMA20 / EMA50: <b>{snap['btc_ema20']:.2f} / {snap['btc_ema50']:.2f}</b>\n"
        f"ATR: <b>{snap['eth_atr']:.2f}</b>\n"
        f"ETH Trend: <b>{snap['eth_trend']}</b>\n"
        f"BTC Rejimi: <b>{snap['btc_regime']}</b>\n\n"
        f"USDT Payı: <b>{snap['usdtd_now']:.3f}</b>\n"
        f"USDT EMA{USDTD_FAST_EMA}/{USDTD_SLOW_EMA}: <b>{snap['usdtd_ema_fast']:.3f} / {snap['usdtd_ema_slow']:.3f}</b>\n"
        f"USDT Kaynağı: <b>{snap['usdtd_source']}</b>\n\n"
        f"Otomatik Tarama: <b>{'AÇIK' if state.get('auto_scan_enabled') else 'KAPALI'}</b>"
    )

def scenario_text(state, dom_now, dom_target):
    snap = analyze_market(state)
    eth_price = snap["eth_price"]
    delta = dom_target - dom_now

    weak_pct = delta * -0.80 * 100
    base_pct = delta * -1.20 * 100
    strong_pct = delta * -1.80 * 100

    weak_target = eth_price * (1 + weak_pct / 100)
    base_target = eth_price * (1 + base_pct / 100)
    strong_target = eth_price * (1 + strong_pct / 100)

    return (
        f"<b>ETH + USDT SENARYO</b>\n\n"
        f"ETH: <b>{eth_price:.2f}</b>\n"
        f"ETH Trend: <b>{snap['eth_trend']}</b>\n"
        f"BTC Rejimi: <b>{snap['btc_regime']}</b>\n\n"
        f"USDT Payı: <b>{dom_now:.3f} → {dom_target:.3f}</b>\n"
        f"Delta: <b>{delta:+.3f}</b>\n\n"
        f"Zayıf: <b>{weak_target:.2f}</b> ({weak_pct:+.2f}%)\n"
        f"Baz: <b>{base_target:.2f}</b> ({base_pct:+.2f}%)\n"
        f"Güçlü: <b>{strong_target:.2f}</b> ({strong_pct:+.2f}%)"
    )

def help_text():
    return (
        "<b>Komutlar</b>\n\n"
        "/durum\n"
        "/senaryo 7.922 7.908\n"
        "/otomatikac\n"
        "/otomatikkapat\n"
        "/sinyal\n"
        "/usdtd\n"
        "/usdtdset 7.851\n"
        "/usdtdauto\n"
        "/yardim"
    )

# -----------------------------
# COMMANDS
# -----------------------------
def handle_command(state, text):
    text = (text or "").strip()

    if text in ("/start", "/yardim", "/help"):
        return help_text()

    if text.startswith("/durum"):
        return status_text(state)

    if text.startswith("/usdtdauto"):
        state["manual_usdtd_override"] = None
        save_state(state)
        return "✅ USDT otomatik moda alındı."

    if text.startswith("/usdtdset"):
        parts = text.split()
        if len(parts) != 2:
            return "Kullanım: /usdtdset 7.851"
        try:
            val = float(parts[1].replace(",", "."))
        except ValueError:
            return "Geçersiz sayı."
        state["manual_usdtd_override"] = val
        save_state(state)
        return f"✅ USDT manuel override aktif: {val:.3f}"

    if text.startswith("/usdtd"):
        snap = analyze_market(state)
        return (
            f"<b>USDT DURUMU</b>\n\n"
            f"USDT Payı: <b>{snap['usdtd_now']:.3f}</b>\n"
            f"EMA{USDTD_FAST_EMA}: <b>{snap['usdtd_ema_fast']:.3f}</b>\n"
            f"EMA{USDTD_SLOW_EMA}: <b>{snap['usdtd_ema_slow']:.3f}</b>\n"
            f"Kaynak: <b>{snap['usdtd_source']}</b>"
        )

    if text.startswith("/otomatikac"):
        state["auto_scan_enabled"] = True
        save_state(state)
        return "✅ Otomatik sinyal taraması açıldı."

    if text.startswith("/otomatikkapat"):
        state["auto_scan_enabled"] = False
        save_state(state)
        return "🛑 Otomatik sinyal taraması kapatıldı."

    if text.startswith("/sinyal"):
        snap = analyze_market(state)
        sig = build_signal(snap)
        if not sig:
            return (
                "Şu an net sinyal yok.\n\n"
                f"ETH Trend: {snap['eth_trend']}\n"
                f"BTC Rejimi: {snap['btc_regime']}\n"
                f"USDT Payı: {snap['usdtd_now']:.3f}\n"
                f"USDT EMA{USDTD_FAST_EMA}/{USDTD_SLOW_EMA}: {snap['usdtd_ema_fast']:.3f}/{snap['usdtd_ema_slow']:.3f}"
            )
        return format_signal(snap, sig)

    if text.startswith("/senaryo"):
        parts = text.split()
        if len(parts) != 3:
            return "Hatalı kullanım.\nÖrnek:\n/senaryo 7.922 7.908"
        try:
            dom_now = float(parts[1].replace(",", "."))
            dom_target = float(parts[2].replace(",", "."))
        except ValueError:
            return "USDT değerleri sayı olmalı."
        return scenario_text(state, dom_now, dom_target)

    return "Komut tanınmadı. /yardim"

# -----------------------------
# AUTO SCAN
# -----------------------------
def auto_scan_if_needed(state):
    if not state.get("auto_scan_enabled", True):
        return state

    now_ts = int(time.time())
    if now_ts - int(state.get("last_auto_scan_ts", 0)) < AUTO_SCAN_INTERVAL_SECONDS:
        return state

    state["last_auto_scan_ts"] = now_ts
    save_state(state)

    try:
        snap = analyze_market(state)
        sig = build_signal(snap)
        if sig and should_send_signal(state, sig):
            tg_send(format_signal(snap, sig))
            state["last_signal_ts"] = now_ts
            state["last_signal_side"] = sig["side"]
            state["last_signal_hash"] = sig["signal_hash"]
            save_state(state)
    except Exception as e:
        log(f"AUTO SCAN HATASI: {e}")

    return state

# -----------------------------
# MAIN
# -----------------------------
def main():
    log("MAIN BAŞLADI")

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN boş")

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID boş")

    log(f"TOKEN VAR: {TELEGRAM_BOT_TOKEN[:10]}...")
    log(f"CHAT ID: {TELEGRAM_CHAT_ID}")

    state = load_state()

    try:
        tg_send("✅ ETH + USDT otomatik sinyal botu aktif.\nKomutlar: /yardim")
    except Exception as e:
        log(f"Başlangıç mesajı hatası: {e}")

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
                log(f"GELEN MESAJ: {text}")

                try:
                    reply = handle_command(state, text)
                except Exception as e:
                    reply = f"HATA:\n{str(e)}"
                    log(f"KOMUT HATASI: {e}")

                try:
                    tg_send(reply)
                except Exception as e:
                    log(f"TELEGRAM GÖNDERİM HATASI: {e}")

                save_state(state)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log(f"ANA DÖNGÜ HATASI: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
