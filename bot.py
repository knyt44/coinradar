import os
import time
import json
import requests
from statistics import mean

print("BOT DOSYASI YÜKLENDİ", flush=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", ""))

STATE_FILE = "state.json"
POLL_SECONDS = 3
HTTP_TIMEOUT = 20

ETH_SYMBOL = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"

def log(msg):
    print(msg, flush=True)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"STATE OKUMA HATASI: {e}")
    return {"last_update_id": 0}

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
    log(f"TELEGRAM SEND BODY: {r.text[:500]}")
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
    log(f"GETUPDATES STATUS: {r.status_code}")
    log(f"GETUPDATES BODY: {r.text[:500]}")
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates hata: {data}")
    return data

def get_binance(symbol):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(
        url,
        params={"symbol": symbol, "interval": "30m", "limit": 200},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    return [float(x[4]) for x in data]

def get_bybit(symbol):
    url = "https://api.bybit.com/v5/market/kline"
    r = requests.get(
        url,
        params={
            "category": "linear",
            "symbol": symbol,
            "interval": "30",
            "limit": 200
        },
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit hata: {data}")
    rows = data["result"]["list"]
    return [float(x[4]) for x in rows[::-1]]

def get_okx(symbol):
    inst_id = symbol.replace("USDT", "-USDT-SWAP")
    url = "https://www.okx.com/api/v5/market/candles"
    r = requests.get(
        url,
        params={"instId": inst_id, "bar": "30m", "limit": 200},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX hata: {data}")
    rows = data["data"]
    return [float(x[4]) for x in rows[::-1]]

def get_price_data(symbol):
    errors = []

    for name, fn in [
        ("Binance", get_binance),
        ("Bybit", get_bybit),
        ("OKX", get_okx),
    ]:
        try:
            data = fn(symbol)
            log(f"{symbol} veri kaynağı: {name}")
            if len(data) < 60:
                raise RuntimeError(f"{name} veri yetersiz")
            return data
        except Exception as e:
            err = f"{name} başarısız: {e}"
            log(err)
            errors.append(err)

    raise RuntimeError("Tüm veri kaynakları başarısız:\n" + "\n".join(errors))

def ema(data, period):
    if len(data) < period:
        raise RuntimeError(f"EMA için veri yetersiz. period={period}")
    k = 2 / (period + 1)
    e = mean(data[:period])
    for x in data[period:]:
        e = x * k + e * (1 - k)
    return e

def atr_like(data, period=14):
    if len(data) < period + 1:
        raise RuntimeError("ATR için veri yetersiz")
    diffs = [abs(data[i] - data[i - 1]) for i in range(1, len(data))]
    return mean(diffs[-period:])

def analyze():
    eth = get_price_data(ETH_SYMBOL)
    btc = get_price_data(BTC_SYMBOL)

    eth_price = eth[-1]
    btc_price = btc[-1]

    eth_ema20 = ema(eth, 20)
    eth_ema50 = ema(eth, 50)
    btc_ema20 = ema(btc, 20)
    btc_ema50 = ema(btc, 50)
    eth_atr = atr_like(eth, 14)

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

    return {
        "eth_price": eth_price,
        "btc_price": btc_price,
        "eth_ema20": eth_ema20,
        "eth_ema50": eth_ema50,
        "btc_ema20": btc_ema20,
        "btc_ema50": btc_ema50,
        "eth_atr": eth_atr,
        "eth_trend": eth_trend,
        "btc_regime": btc_regime,
    }

def scenario(dom_now, dom_target):
    ctx = analyze()

    eth_price = ctx["eth_price"]
    atr_val = ctx["eth_atr"]
    trend = ctx["eth_trend"]
    regime = ctx["btc_regime"]

    delta = dom_target - dom_now

    weak_pct = delta * -0.80 * 100
    base_pct = delta * -1.20 * 100
    strong_pct = delta * -1.80 * 100

    weak_target = eth_price * (1 + weak_pct / 100)
    base_target = eth_price * (1 + base_pct / 100)
    strong_target = eth_price * (1 + strong_pct / 100)

    if delta < 0 and regime != "RISK-OFF":
        bias = "LONG"
        stop = eth_price - 1.2 * atr_val
        tp1 = eth_price + 1.0 * atr_val
        tp2 = eth_price + 1.8 * atr_val
        tp3 = eth_price + 2.8 * atr_val
    elif delta > 0:
        bias = "SHORT"
        stop = eth_price + 1.2 * atr_val
        tp1 = eth_price - 1.0 * atr_val
        tp2 = eth_price - 1.8 * atr_val
        tp3 = eth_price - 2.8 * atr_val
    else:
        bias = "NÖTR"
        stop = eth_price - atr_val
        tp1 = eth_price + atr_val
        tp2 = eth_price + 2 * atr_val
        tp3 = eth_price + 3 * atr_val

    return (
        f"<b>ETH + USDT.D SENARYO</b>\n\n"
        f"ETH: <b>{eth_price:.2f}</b>\n"
        f"Trend: <b>{trend}</b>\n"
        f"BTC Rejimi: <b>{regime}</b>\n\n"
        f"USDT.D: <b>{dom_now:.3f} → {dom_target:.3f}</b>\n"
        f"Delta: <b>{delta:+.3f}</b>\n\n"
        f"Zayıf Senaryo: <b>{weak_target:.2f}</b> ({weak_pct:+.2f}%)\n"
        f"Baz Senaryo: <b>{base_target:.2f}</b> ({base_pct:+.2f}%)\n"
        f"Güçlü Senaryo: <b>{strong_target:.2f}</b> ({strong_pct:+.2f}%)\n\n"
        f"Bias: <b>{bias}</b>\n"
        f"Stop: <b>{stop:.2f}</b>\n"
        f"TP1: <b>{tp1:.2f}</b>\n"
        f"TP2: <b>{tp2:.2f}</b>\n"
        f"TP3: <b>{tp3:.2f}</b>"
    )

def status():
    ctx = analyze()
    return (
        f"<b>DURUM</b>\n\n"
        f"ETH: <b>{ctx['eth_price']:.2f}</b>\n"
        f"BTC: <b>{ctx['btc_price']:.2f}</b>\n"
        f"ETH EMA20 / EMA50: <b>{ctx['eth_ema20']:.2f} / {ctx['eth_ema50']:.2f}</b>\n"
        f"BTC EMA20 / EMA50: <b>{ctx['btc_ema20']:.2f} / {ctx['btc_ema50']:.2f}</b>\n"
        f"ATR: <b>{ctx['eth_atr']:.2f}</b>\n"
        f"ETH Trend: <b>{ctx['eth_trend']}</b>\n"
        f"BTC Rejimi: <b>{ctx['btc_regime']}</b>"
    )

def help_text():
    return (
        "<b>Komutlar</b>\n\n"
        "/durum\n"
        "/senaryo 7.922 7.908\n"
        "/yardim"
    )

def handle_command(text):
    text = (text or "").strip()

    if text in ("/start", "/yardim", "/help"):
        return help_text()

    if text.startswith("/durum"):
        return status()

    if text.startswith("/senaryo"):
        parts = text.split()
        if len(parts) != 3:
            return "Hatalı kullanım.\nÖrnek:\n/senaryo 7.922 7.908"

        try:
            dom_now = float(parts[1].replace(",", "."))
            dom_target = float(parts[2].replace(",", "."))
        except ValueError:
            return "USDT.D değerleri sayı olmalı."

        return scenario(dom_now, dom_target)

    return "Komut tanınmadı. /yardim"

def main():
    log("MAIN BAŞLADI")

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN boş")

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID boş")

    log(f"TOKEN VAR: {TELEGRAM_BOT_TOKEN[:10]}...")
    log(f"CHAT ID: {TELEGRAM_CHAT_ID}")

    state = load_state()

    tg_send("✅ ETH + USDT.D PRO bot aktif.\nKomutlar: /yardim")

    while True:
        try:
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
                    reply = handle_command(text)
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
