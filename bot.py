import os
import time
import requests
from datetime import datetime

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "ETHUSDT"
BASE = "https://api.mexc.com"
CG = "https://api.coingecko.com/api/v3"

CHECK = 20

# =====================
# TELEGRAM
# =====================
def tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg}
        )
    except:
        pass

# =====================
# SAFE REQUEST
# =====================
def safe_get(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=10)
        return r.json()
    except:
        return None

# =====================
# PRICE
# =====================
def price():
    d = safe_get(f"{BASE}/api/v3/ticker/price", {"symbol": SYMBOL})
    if not d: return None
    return float(d["price"])

# =====================
# KLINES SAFE
# =====================
def klines(tf):
    d = safe_get(f"{BASE}/api/v3/klines",
        {"symbol": SYMBOL, "interval": tf, "limit": 200})

    if not isinstance(d, list): return []

    closes = []
    for x in d:
        if isinstance(x, list) and len(x) > 4:
            closes.append(float(x[4]))

    return closes

# =====================
# EMA
# =====================
def ema(arr, p):
    if len(arr) < p: return None
    k = 2/(p+1)
    e = arr[0]
    for x in arr:
        e = x*k + e*(1-k)
    return e

# =====================
# USDT.D
# =====================
def usdtd():
    try:
        g = safe_get(f"{CG}/global")
        total = g["data"]["total_market_cap"]["usd"]

        t = safe_get(f"{CG}/simple/price",
            {"ids":"tether","vs_currencies":"usd","include_market_cap":"true"})
        usdt = t["tether"]["usd_market_cap"]

        return usdt/total*100
    except:
        return None

# =====================
# SIGNAL
# =====================
def signal():
    p = price()
    if not p: return None

    c5 = klines("5m")
    c15 = klines("15m")
    c1h = klines("1h")
    c4h = klines("4h")

    if len(c5)<50 or len(c15)<50 or len(c1h)<50 or len(c4h)<50:
        return None

    e5 = ema(c5,9)
    e15 = ema(c15,21)
    e1h = ema(c1h,50)
    e4h = ema(c4h,50)

    u = usdtd()
    if u is None:
        return None

    # USDT.D yön
    # artıyorsa SHORT, düşüyorsa LONG
    u_dir = "SHORT" if u > 7 else "LONG"

    long_score = 0
    short_score = 0

    if p > e5: long_score += 2
    if p < e5: short_score += 2

    if p > e15: long_score += 2
    if p < e15: short_score += 2

    if p > e1h: long_score += 2
    if p < e1h: short_score += 2

    if p > e4h: long_score += 2
    if p < e4h: short_score += 2

    # USDT.D ağırlık
    if u_dir == "LONG":
        long_score += 3
    else:
        short_score += 3

    if long_score > short_score and long_score > 7:
        return "LONG", p, long_score, u

    if short_score > long_score and short_score > 7:
        return "SHORT", p, short_score, u

    return None

# =====================
# MAIN
# =====================
last = None

while True:
    try:
        s = signal()

        if s:
            side, p, sc, u = s

            sig = f"{side}-{round(p,2)}"

            if sig != last:
                last = sig

                msg = f"""
{'🟢' if side=='LONG' else '🔴'} ETH {side}

Fiyat: {p:.2f}
Skor: {sc}

USDT.D: {u:.2f}

TP1 TP2 TP3 aktif
"""
                tg(msg)

    except Exception as e:
        print("ERR:", e)

    time.sleep(CHECK)
