import requests
import time
import logging
import os

# =========================
# CONFIG
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MEXC_BASE = "https://contract.mexc.com"
CHECK_INTERVAL = 60
TOP_N = 3

EXCLUDED = {"BTC_USDT", "ETH_USDT", "PAXG_USDT", "XRP_USDT"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

# =========================
# TELEGRAM
# =========================
def tg_send(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except:
        pass

# =========================
# SYMBOLS
# =========================
def get_symbols():
    try:
        r = requests.get(f"{MEXC_BASE}/api/v1/contract/detail")
        data = r.json()["data"]
        symbols = []

        for item in data:
            s = item.get("symbol", "")
            if not s.endswith("_USDT"):
                continue
            if s in EXCLUDED:
                continue
            symbols.append(s)

        return list(set(symbols))[:120]

    except Exception as e:
        logger.error(e)
        return []

# =========================
# BTC FILTER
# =========================
def get_btc_bias():
    try:
        r = requests.get(f"{MEXC_BASE}/api/v1/contract/kline/BTC_USDT?interval=Min15&limit=50")
        data = r.json()["data"]

        closes = [float(x[4]) for x in data]
        if closes[-1] > sum(closes[-20:]) / 20:
            return "LONG"
        return "SHORT"

    except:
        return "NEUTRAL"

# =========================
# SIGNAL LOGIC (BASİT AMA ÇALIŞIR)
# =========================
def evaluate(symbol, btc_bias):
    try:
        r = requests.get(f"{MEXC_BASE}/api/v1/contract/kline/{symbol}?interval=Min15&limit=50")
        data = r.json()["data"]

        closes = [float(x[4]) for x in data]
        high = max(closes[-10:])
        low = min(closes[-10:])
        price = closes[-1]

        score = abs(price - (sum(closes[-10:]) / 10))

        if price > high * 0.995:
            side = "LONG"
        elif price < low * 1.005:
            side = "SHORT"
        else:
            return None

        if btc_bias == "SHORT" and side == "LONG":
            return None

        atr = (high - low)

        entry = price
        sl = price - atr if side == "LONG" else price + atr
        tp1 = price + atr if side == "LONG" else price - atr
        tp2 = price + atr * 1.5 if side == "LONG" else price - atr * 1.5
        tp3 = price + atr * 2 if side == "LONG" else price - atr * 2

        return {
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "score": score
        }

    except:
        return None

# =========================
# TELEGRAM FORMAT
# =========================
def send_signal(s):
    msg = f"""{s['side']} {s['symbol']}
Entry: {s['entry']:.6f}
SL: {s['sl']:.6f}
TP1: {s['tp1']:.6f}
TP2: {s['tp2']:.6f}
TP3: {s['tp3']:.6f}"""
    tg_send(msg)

# =========================
# MAIN LOOP
# =========================
def run():
    while True:
        btc_bias = get_btc_bias()
        symbols = get_symbols()

        candidates = []

        for sym in symbols:
            sig = evaluate(sym, btc_bias)
            if sig:
                candidates.append(sig)

        if candidates:
            candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)
            top = candidates[:TOP_N]

            for s in top:
                send_signal(s)

        time.sleep(CHECK_INTERVAL)

# =========================
# START
# =========================
if __name__ == "__main__":
    tg_send("BOT BAŞLADI")
    run()
