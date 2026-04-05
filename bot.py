# MEXC FINAL PRO SIGNAL BOT (RAILWAY READY)

import os, time, json
from datetime import datetime
import requests
import pandas as pd
import numpy as np

BASE_URL = "https://api.mexc.com"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","ARBUSDT",
    "APTUSDT","FETUSDT","RNDRUSDT","SEIUSDT","DOGEUSDT"
]

SCAN_INTERVAL = 60

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")

session = requests.Session()

def tg(msg):
    try:
        session.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                     data={"chat_id":CHAT,"text":msg,"parse_mode":"HTML"})
    except: pass

def get_klines(symbol, interval):
    r = session.get(f"{BASE_URL}/api/v3/klines",
        params={"symbol":symbol,"interval":interval,"limit":200}, timeout=15)
    data = r.json()

    df = pd.DataFrame(data)
    df = df[[0,1,2,3,4,5]]
    df.columns = ["t","o","h","l","c","v"]

    df["c"]=pd.to_numeric(df["c"])
    df["h"]=pd.to_numeric(df["h"])
    df["l"]=pd.to_numeric(df["l"])
    df["v"]=pd.to_numeric(df["v"])

    return df

def ema(s,n): return s.ewm(span=n).mean()

def rsi(s, n=14):
    d=s.diff()
    u=d.clip(lower=0)
    l=-d.clip(upper=0)
    rs=u.ewm(alpha=1/n).mean()/l.ewm(alpha=1/n).mean()
    return 100-(100/(1+rs))

def atr(df,n=14):
    tr = (df["h"]-df["l"]).rolling(n).mean()
    return tr

def analyze(symbol):
    df1 = get_klines(symbol,"1h")
    df2 = get_klines(symbol,"15m")

    if len(df1)<50 or len(df2)<50:
        return None

    df1["ema50"]=ema(df1["c"],50)
    df1["ema200"]=ema(df1["c"],200)
    df1["rsi"]=rsi(df1["c"])

    df2["ema20"]=ema(df2["c"],20)
    df2["rsi"]=rsi(df2["c"])
    df2["atr"]=atr(df2)

    x1=df1.iloc[-1]
    x2=df2.iloc[-1]

    # 🔥 SPREAD + FAKE FILTER yerine volatility filtresi
    if x2["atr"]/x2["c"] < 0.0035:
        return None

    # LONG
    if x1["c"]>x1["ema50"]>x1["ema200"] and x1["rsi"]>54:
        if x2["c"]>x2["ema20"] and 48<x2["rsi"]<65:
            entry=x2["c"]
            sl=entry - x2["atr"]*1.2
            tp1=entry+(entry-sl)*1.2
            tp2=entry+(entry-sl)*2.2
            tp3=entry+(entry-sl)*3.2

            return ("LONG", entry, sl, tp1, tp2, tp3)

    # SHORT (sınırlı)
    if symbol in ["BTCUSDT","ETHUSDT","ARBUSDT","FETUSDT","RNDRUSDT","APTUSDT"]:
        if x1["c"]<x1["ema50"]<x1["ema200"] and x1["rsi"]<46:
            if x2["c"]<x2["ema20"] and 35<x2["rsi"]<52:
                entry=x2["c"]
                sl=entry + x2["atr"]*1.2
                tp1=entry-(sl-entry)*1.1
                tp2=entry-(sl-entry)*2.0
                tp3=entry-(sl-entry)*2.8

                return ("SHORT", entry, sl, tp1, tp2, tp3)

    return None

def format_msg(symbol, side, e, sl, t1, t2, t3):
    return f"""
🔥 <b>MEXC PRO SIGNAL</b>

<b>{symbol}</b> | {side}

Entry: {e:.4f}
SL: {sl:.4f}

TP1: {t1:.4f}
TP2: {t2:.4f}
TP3: {t3:.4f}
"""

def main():
    tg("🚀 MEXC BOT BAŞLADI")

    while True:
        try:
            for s in SYMBOLS:
                sig = analyze(s)
                if sig:
                    side,e,sl,t1,t2,t3 = sig
                    tg(format_msg(s,side,e,sl,t1,t2,t3))
                    time.sleep(2)

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            print("ERR:",e)
            time.sleep(10)

if __name__=="__main__":
    main()
