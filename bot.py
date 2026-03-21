import requests
import time

TOKEN = "8448822429:AAHgN_Df7SP2zXOKk9HCtc6l-Pf8XRaYcrI"
CHAT_ID = "917476574"

def send(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

def check():
    url = "https://api.mexc.com/api/v3/ticker/24hr"
    data = requests.get(url).json()

    for coin in data:
        change = float(coin["priceChangePercent"])
        if change > 5:
            send(f"{coin['symbol']} yükseliyor %{change}")

while True:
    check()
    time.sleep(60)
