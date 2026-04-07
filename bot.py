import os
import time
import json
import requests
from statistics import mean

# ==============================
# SETTINGS
# ==============================
TELEGRAM_BOT_TOKEN = "BURAYA_BOT_TOKEN"
TELEGRAM_CHAT_ID = "BURAYA_CHAT_ID"

STATE_FILE = "state.json"
POLL_SECONDS = 3

HTTP_TIMEOUT = 15

ETH_SYMBOL = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"

# ==============================
# STATE
# ==============================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_update_id": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ==============================
# TELEGRAM
# ==============================
def tg_send(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    })

def tg_updates(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    return requests.get(url, params={"offset": offset, "timeout": 20}).json()

# ==============================
# DATA SOURCES (FALLBACK)
# ==============================
def get_binance(symbol):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url, params={
        "symbol": symbol,
        "interval": "30m",
        "limit": 200
    }, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return [float(x[4]) for x in data]

def get_bybit(symbol):
    url = "https://api.bybit.com/v5/market/kline"
    r = requests.get(url, params={
        "category": "linear",
        "symbol": symbol,
        "interval": "30",
        "limit": 200
    }, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()["result"]["list"]
    return [float(x[4]) for x in data[::-1]]

def get_okx(symbol):
    sym = symbol.replace("USDT", "-USDT-SWAP")
    url = "https://www.okx.com/api/v5/market/candles"
    r = requests.get(url, params={
        "instId": sym,
        "bar": "30m",
        "limit": 200
    }, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()["data"]
    return [float(x[4]) for x in data[::-1]]

def get_price_data(symbol):
    for source in [get_binance, get_bybit, get_okx]:
        try:
            return source(symbol)
        except:
            continue
    raise Exception("Tüm borsalar başarısız")

# ==============================
# INDICATORS
# ==============================
def ema(data, p):
    k = 2 / (p + 1)
    e = mean(data[:p])
    for x in data[p:]:
        e = x * k + e * (1 - k)
    return e

def atr(data):
    return mean([abs(data[i] - data[i-1]) for i in range(1, len(data))][-14:])

# ==============================
# ANALYSIS
# ==============================
def analyze():
    eth = get_price_data(ETH_SYMBOL)
    btc = get_price_data(BTC_SYMBOL)

    eth_price = eth[-1]
    btc_price = btc[-1]

    eth_ema20 = ema(eth, 20)
    eth_ema50 = ema(eth, 50)

    btc_ema20 = ema(btc, 20)
    btc_ema50 = ema(btc, 50)

    eth_atr = atr(eth)

    # trend
    if eth_price > eth_ema20 > eth_ema50:
        trend = "GÜÇLÜ YUKARI"
    elif eth_price < eth_ema20 < eth_ema50:
        trend = "GÜÇLÜ AŞAĞI"
    else:
        trend = "KARIŞIK"

    # btc regime
    if btc_price > btc_ema20 > btc_ema50:
        regime = "RISK-ON"
    elif btc_price < btc_ema20 < btc_ema50:
        regime = "RISK-OFF"
    else:
        regime = "NÖTR"

    return eth_price, eth_atr, trend, regime

# ==============================
# SCENARIO
# ==============================
def scenario(dom_now, dom_target):
    eth_price, atr_val, trend, regime = analyze()

    delta = dom_target - dom_now

    move = -delta * 120  # base model

    target = eth_price * (1 + move/100)

    if delta < 0 and regime != "RISK-OFF":
        bias = "LONG"
    elif delta > 0:
        bias = "SHORT"
    else:
        bias = "NÖTR"

    stop = eth_price - atr_val if bias == "LONG" else eth_price + atr_val

    return f"""
<b>ETH SENARYO</b>

ETH: {eth_price:.2f}
Trend: {trend}
BTC: {regime}

USDT.D: {dom_now} → {dom_target}
Delta: {delta:.3f}

Hedef: {target:.2f}

Bias: {bias}

Stop: {stop:.2f}
TP1: {eth_price + atr_val:.2f}
TP2: {eth_price + 2*atr_val:.2f}
TP3: {eth_price + 3*atr_val:.2f}
"""

def status():
    eth_price, atr_val, trend, regime = analyze()
    return f"""
<b>DURUM</b>

ETH: {eth_price:.2f}
ATR: {atr_val:.2f}
Trend: {trend}
BTC: {regime}
"""

# ==============================
# MAIN
# ==============================
def main():
    state = load_state()
    tg_send("✅ PRO BOT AKTİF")

    while True:
        updates = tg_updates(state["last_update_id"] + 1)

        for u in updates.get("result", []):
            state["last_update_id"] = u["update_id"]

            msg = u.get("message", {})
            chat = str(msg.get("chat", {}).get("id", ""))

            if chat != TELEGRAM_CHAT_ID:
                continue

            text = msg.get("text", "")

            try:
                if text.startswith("/durum"):
                    tg_send(status())

                elif text.startswith("/senaryo"):
                    _, a, b = text.split()
                    tg_send(scenario(float(a), float(b)))

                else:
                    tg_send("Komut yok")

            except Exception as e:
                tg_send(f"HATA: {str(e)}")

            save_state(state)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
