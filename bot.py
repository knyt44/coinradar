# =========================
# ETH USDTD CORE FINAL BOT
# =========================

import os
import time
import json
from datetime import datetime, timezone

import requests

# =========================
# CONFIG
# =========================
SYMBOL = "ETHUSDT"
MEXC = "https://api.mexc.com"
CG = "https://api.coingecko.com/api/v3"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK = 20
STATE_FILE = "state.json"

# =========================
# UTILS
# =========================
def now():
    return datetime.now(timezone.utc)

def ts():
    return int(now().timestamp())

def fmt(p):
    return f"{p:.2f}" if p > 100 else f"{p:.4f}"

def day_key(t): return datetime.fromtimestamp(t).strftime("%Y-%m-%d")
def week_key(t): return datetime.fromtimestamp(t).strftime("%Y-W%W")
def month_key(t): return datetime.fromtimestamp(t).strftime("%Y-%m")

def log(x): print(f"[{datetime.now()}] {x}")

# =========================
# TELEGRAM
# =========================
def tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg}
        )
    except:
        pass

# =========================
# DATA
# =========================
def klines(tf):
    r = requests.get(f"{MEXC}/api/v3/klines",
        params={"symbol": SYMBOL, "interval": tf, "limit": 200}).json()
    return [float(x[4]) for x in r]

def price():
    return float(requests.get(
        f"{MEXC}/api/v3/ticker/price",
        params={"symbol": SYMBOL}).json()["price"])

# =========================
# EMA
# =========================
def ema(arr, p):
    k = 2/(p+1)
    e = arr[0]
    for x in arr:
        e = x*k + e*(1-k)
    return e

# =========================
# USDTD
# =========================
def usdtd():
    g = requests.get(f"{CG}/global").json()
    total = g["data"]["total_market_cap"]["usd"]
    usdt = requests.get(
        f"{CG}/simple/price",
        params={"ids":"tether","vs_currencies":"usd","include_market_cap":"true"}
    ).json()["tether"]["usd_market_cap"]
    return usdt/total*100

def usdtd_regime(hist):
    if len(hist) < 20: return "NEUTRAL"
    fast = ema(hist,6)
    slow = ema(hist,18)
    nowv = hist[-1]

    if fast > slow and nowv > fast:
        return "STRONG_SHORT"
    if fast < slow and nowv < fast:
        return "STRONG_LONG"
    if fast > slow:
        return "SHORT"
    if fast < slow:
        return "LONG"
    return "NEUTRAL"

# =========================
# STATE
# =========================
def load():
    if not os.path.exists(STATE_FILE):
        return {
            "usdtd_hist": [],
            "active": None,
            "history": [],
            "daily": None,
            "weekly": None,
            "monthly": None
        }
    return json.load(open(STATE_FILE))

def save(s):
    json.dump(s, open(STATE_FILE,"w"))

# =========================
# SIGNAL
# =========================
def signal(state):
    p = price()

    c5 = klines("5m")
    c15 = klines("15m")
    c1h = klines("1h")
    c4h = klines("4h")

    e5 = ema(c5,9)
    e15 = ema(c15,21)
    e1h = ema(c1h,50)
    e4h = ema(c4h,50)

    # USDTD
    u = usdtd()
    state["usdtd_hist"].append(u)
    state["usdtd_hist"] = state["usdtd_hist"][-100:]
    reg = usdtd_regime(state["usdtd_hist"])

    scoreL = 0
    scoreS = 0

    # 5m trigger
    if p > e5: scoreL += 3
    if p < e5: scoreS += 3

    # 15m
    if p > e15: scoreL += 2
    if p < e15: scoreS += 2

    # 1h
    if p > e1h: scoreL += 2
    if p < e1h: scoreS += 2

    # 4h
    if p > e4h: scoreL += 2
    if p < e4h: scoreS += 2

    # USDTD ağırlık
    if reg == "STRONG_LONG":
        scoreL += 4
        scoreS = 0
    elif reg == "STRONG_SHORT":
        scoreS += 4
        scoreL = 0
    elif reg == "LONG":
        scoreL += 2
        scoreS -= 1
    elif reg == "SHORT":
        scoreS += 2
        scoreL -= 1

    if scoreL > scoreS and scoreL > 8:
        return "LONG", p, scoreL, reg
    if scoreS > scoreL and scoreS > 8:
        return "SHORT", p, scoreS, reg

    return None, p, 0, reg

# =========================
# REPORT
# =========================
def report(state, period):
    nowt = ts()

    if period=="daily":
        key = day_key(nowt)
        title = "📅 GÜNLÜK"
    elif period=="weekly":
        key = week_key(nowt)
        title = "📈 HAFTALIK"
    else:
        key = month_key(nowt)
        title = "🗓 AYLIK"

    total=win=loss=0
    pnl=0

    for s in state["history"]:
        if period=="daily" and day_key(s["t"])!=key: continue
        if period=="weekly" and week_key(s["t"])!=key: continue
        if period=="monthly" and month_key(s["t"])!=key: continue

        total+=1
        if s["res"]=="TP":
            win+=1
            pnl+=3
        else:
            loss+=1
            pnl-=1

    wr = (win/total*100) if total else 0

    return f"""{title} RAPOR

Toplam: {total}
Win: {win}
Loss: {loss}
WinRate: %{wr:.1f}
PnL: {pnl}R"""

# =========================
# AUTO REPORT
# =========================
def auto_report(state):
    n = datetime.now()

    if n.hour==23 and n.minute>=55:
        d = day_key(ts())
        if state["daily"]!=d:
            tg(report(state,"daily"))
            state["daily"]=d

    if n.weekday()==6 and n.hour==23 and n.minute>=55:
        w = week_key(ts())
        if state["weekly"]!=w:
            tg(report(state,"weekly"))
            state["weekly"]=w

    tmr = datetime.fromtimestamp(ts()+86400)
    if n.month!=tmr.month and n.hour==23 and n.minute>=55:
        m = month_key(ts())
        if state["monthly"]!=m:
            tg(report(state,"monthly"))
            state["monthly"]=m

# =========================
# MAIN
# =========================
def run():
    state = load()

    auto_report(state)

    sig, p, sc, reg = signal(state)

    if sig:
        msg = f"""
{'🟢' if sig=='LONG' else '🔴'} ETH {sig}

Fiyat: {fmt(p)}
Skor: {sc}
USDT.D: {reg}

TP1 / TP2 / TP3 aktif
"""
        tg(msg)

    save(state)

# =========================
# LOOP
# =========================
while True:
    try:
        run()
    except Exception as e:
        log(e)
    time.sleep(CHECK)
