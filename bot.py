import os
import time
import json
import requests
from statistics import mean

# ============================================================
# ETH + USDT.D SCENARIO TELEGRAM BOT
# SPOT API VERSION
# Tek parça, sade, requests dışında ek kütüphane yok
# ============================================================

# -----------------------------
# USER SETTINGS
# -----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "BURAYA_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

POLL_SECONDS = 3
STATE_FILE = "eth_usdtd_bot_state.json"

BINANCE_BASE = "https://api.binance.com"
HTTP_TIMEOUT = 20

ETH_SYMBOL = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"
INTERVAL = "30m"
KLINE_LIMIT = 220

# USDT.D -> ETH senaryo katsayıları
# Bu bölüm model varsayımıdır, kesin piyasa gerçeği değildir.
DOM_BETA_WEAK = -0.80
DOM_BETA_BASE = -1.20
DOM_BETA_STRONG = -1.80

REPLY_COOLDOWN_SECONDS = 2

# -----------------------------
# STATE
# -----------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "last_update_id": 0,
        "last_reply_ts": 0
    }

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# -----------------------------
# HTTP
# -----------------------------
def http_get(url, params=None):
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

def tg_send_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

def tg_get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 20}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

# -----------------------------
# BINANCE SPOT DATA
# -----------------------------
def get_mark_price(symbol):
    data = http_get(
        f"{BINANCE_BASE}/api/v3/ticker/price",
        params={"symbol": symbol}
    )
    return float(data["price"])

def get_klines(symbol, interval="30m", limit=220):
    data = http_get(
        f"{BINANCE_BASE}/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
    )

    klines = []
    for row in data:
        klines.append({
            "open_time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "close_time": int(row[6]),
        })
    return klines

# -----------------------------
# INDICATORS
# -----------------------------
def ema(values, period):
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema_value = mean(values[:period])
    for price in values[period:]:
        ema_value = (price - ema_value) * multiplier + ema_value
    return ema_value

def true_range(curr_high, curr_low, prev_close):
    return max(
        curr_high - curr_low,
        abs(curr_high - prev_close),
        abs(curr_low - prev_close)
    )

def atr(klines, period=14):
    if len(klines) < period + 1:
        return None

    trs = []
    for i in range(1, len(klines)):
        tr = true_range(
            klines[i]["high"],
            klines[i]["low"],
            klines[i - 1]["close"]
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    return mean(trs[-period:])

# -----------------------------
# MARKET CONTEXT
# -----------------------------
def calc_market_context():
    eth_klines = get_klines(ETH_SYMBOL, INTERVAL, KLINE_LIMIT)
    btc_klines = get_klines(BTC_SYMBOL, INTERVAL, KLINE_LIMIT)

    if not eth_klines or not btc_klines:
        raise RuntimeError("Piyasa verisi alınamadı.")

    eth_closes = [x["close"] for x in eth_klines]
    btc_closes = [x["close"] for x in btc_klines]

    eth_price = eth_closes[-1]
    btc_price = btc_closes[-1]

    eth_ema20 = ema(eth_closes, 20)
    eth_ema50 = ema(eth_closes, 50)
    btc_ema20 = ema(btc_closes, 20)
    btc_ema50 = ema(btc_closes, 50)

    eth_atr = atr(eth_klines, 14)

    if eth_ema20 is None or eth_ema50 is None or btc_ema20 is None or btc_ema50 is None or eth_atr is None:
        raise RuntimeError("İndikatör hesaplamak için veri yetersiz.")

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
    elif btc_price > btc_ema20:
        btc_regime = "POZİTİF"
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
        "btc_regime": btc_regime
    }

# -----------------------------
# SCENARIO ENGINE
# -----------------------------
def pct_change_from_dom_change(dom_current, dom_target, beta):
    """
    Örnek:
    dom_current = 7.922
    dom_target  = 7.908
    delta       = -0.014

    beta = -1.20 ise:
    beklenen ETH hareketi = (-0.014 * -1.20) * 100 = +1.68%
    """
    delta = dom_target - dom_current
    return delta * beta * 100.0

def project_price(current_price, pct_move):
    return current_price * (1 + pct_move / 100.0)

def trade_bias(dom_current, dom_target, eth_trend, btc_regime):
    dom_delta = dom_target - dom_current

    if dom_delta < 0 and ("YUKARI" in eth_trend) and btc_regime in ("RISK-ON", "POZİTİF"):
        return "LONG BIAS GÜÇLÜ"
    if dom_delta < 0 and btc_regime != "RISK-OFF":
        return "LONG BIAS"
    if dom_delta > 0 and ("AŞAĞI" in eth_trend or btc_regime == "RISK-OFF"):
        return "SHORT / UZAK DUR"
    if dom_delta > 0:
        return "ZAYIF NEGATİF"
    return "NÖTR"

def stop_and_targets(eth_price, eth_atr, bias):
    if "LONG" in bias:
        stop = eth_price - 1.2 * eth_atr
        tp1 = eth_price + 1.0 * eth_atr
        tp2 = eth_price + 1.8 * eth_atr
        tp3 = eth_price + 2.8 * eth_atr
    elif "SHORT" in bias:
        stop = eth_price + 1.2 * eth_atr
        tp1 = eth_price - 1.0 * eth_atr
        tp2 = eth_price - 1.8 * eth_atr
        tp3 = eth_price - 2.8 * eth_atr
    else:
        stop = eth_price - 1.0 * eth_atr
        tp1 = eth_price + 0.8 * eth_atr
        tp2 = eth_price + 1.4 * eth_atr
        tp3 = eth_price + 2.0 * eth_atr

    return stop, tp1, tp2, tp3

def build_scenario_text(dom_current, dom_target):
    ctx = calc_market_context()

    eth_price = ctx["eth_price"]
    eth_trend = ctx["eth_trend"]
    btc_regime = ctx["btc_regime"]
    eth_atr = ctx["eth_atr"]

    weak_pct = pct_change_from_dom_change(dom_current, dom_target, DOM_BETA_WEAK)
    base_pct = pct_change_from_dom_change(dom_current, dom_target, DOM_BETA_BASE)
    strong_pct = pct_change_from_dom_change(dom_current, dom_target, DOM_BETA_STRONG)

    weak_price = project_price(eth_price, weak_pct)
    base_price = project_price(eth_price, base_pct)
    strong_price = project_price(eth_price, strong_pct)

    bias = trade_bias(dom_current, dom_target, eth_trend, btc_regime)
    stop, tp1, tp2, tp3 = stop_and_targets(eth_price, eth_atr, bias)

    dom_delta = dom_target - dom_current
    if dom_delta < 0:
        direction = "DÜŞÜŞ"
    elif dom_delta > 0:
        direction = "YÜKSELİŞ"
    else:
        direction = "YATAY"

    msg = []
    msg.append("<b>ETH + USDT.D Senaryo Analizi</b>")
    msg.append("")
    msg.append(f"• ETH anlık: <b>{eth_price:.2f}</b>")
    msg.append(f"• BTC rejimi: <b>{btc_regime}</b>")
    msg.append(f"• ETH trend: <b>{eth_trend}</b>")
    msg.append(f"• ETH ATR(14,30m): <b>{eth_atr:.2f}</b>")
    msg.append("")
    msg.append(f"• USDT.D mevcut: <b>{dom_current:.3f}</b>")
    msg.append(f"• USDT.D hedef: <b>{dom_target:.3f}</b>")
    msg.append(f"• Değişim: <b>{dom_delta:+.3f}</b> puan ({direction})")
    msg.append("")
    msg.append("<b>Senaryo fiyatları</b>")
    msg.append(f"• Zayıf tepki: <b>{weak_price:.2f}</b> ({weak_pct:+.2f}%)")
    msg.append(f"• Baz senaryo: <b>{base_price:.2f}</b> ({base_pct:+.2f}%)")
    msg.append(f"• Güçlü tepki: <b>{strong_price:.2f}</b> ({strong_pct:+.2f}%)")
    msg.append("")
    msg.append(f"• Bias: <b>{bias}</b>")
    msg.append(f"• Referans stop: <b>{stop:.2f}</b>")
    msg.append(f"• TP1: <b>{tp1:.2f}</b>")
    msg.append(f"• TP2: <b>{tp2:.2f}</b>")
    msg.append(f"• TP3: <b>{tp3:.2f}</b>")
    msg.append("")
    msg.append("<b>Kullanım</b>")
    msg.append("/senaryo 7.922 7.908")
    msg.append("/durum")
    msg.append("/yardim")

    return "\n".join(msg)

def build_status_text():
    ctx = calc_market_context()

    msg = []
    msg.append("<b>Bot Durumu</b>")
    msg.append("")
    msg.append(f"• ETH: <b>{ctx['eth_price']:.2f}</b>")
    msg.append(f"• BTC: <b>{ctx['btc_price']:.2f}</b>")
    msg.append(f"• ETH EMA20 / EMA50: <b>{ctx['eth_ema20']:.2f} / {ctx['eth_ema50']:.2f}</b>")
    msg.append(f"• BTC EMA20 / EMA50: <b>{ctx['btc_ema20']:.2f} / {ctx['btc_ema50']:.2f}</b>")
    msg.append(f"• ETH ATR(14,30m): <b>{ctx['eth_atr']:.2f}</b>")
    msg.append(f"• ETH trend: <b>{ctx['eth_trend']}</b>")
    msg.append(f"• BTC rejimi: <b>{ctx['btc_regime']}</b>")
    msg.append("")
    msg.append("Komut: /senaryo 7.922 7.908")
    return "\n".join(msg)

def build_help_text():
    return (
        "<b>Komutlar</b>\n\n"
        "/durum\n"
        "Anlık ETH/BTC trend bilgisini verir.\n\n"
        "/senaryo MEVCUT_USDTD HEDEF_USDTD\n"
        "Örnek: /senaryo 7.922 7.908\n\n"
        "/yardim\n"
        "Yardım ekranı.\n"
    )

# -----------------------------
# COMMANDS
# -----------------------------
def parse_command(text):
    text = (text or "").strip()
    if not text:
        return None, []
    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]
    return cmd, args

def handle_message(text):
    cmd, args = parse_command(text)

    if cmd in ("/start", "/yardim", "/help"):
        return build_help_text()

    if cmd == "/durum":
        return build_status_text()

    if cmd == "/senaryo":
        if len(args) != 2:
            return "Hatalı kullanım.\nÖrnek:\n/senaryo 7.922 7.908"

        try:
            dom_current = float(args[0].replace(",", "."))
            dom_target = float(args[1].replace(",", "."))
        except ValueError:
            return "USDT.D değerleri sayı olmalı.\nÖrnek:\n/senaryo 7.922 7.908"

        if dom_current <= 0 or dom_target <= 0:
            return "USDT.D değerleri 0'dan büyük olmalı."

        return build_scenario_text(dom_current, dom_target)

    return "Bilinmeyen komut.\n/yardim yazarak komutları görebilirsin."

# -----------------------------
# MAIN
# -----------------------------
def main():
    if "BURAYA_BOT_TOKEN" in TELEGRAM_BOT_TOKEN or "BURAYA_CHAT_ID" in TELEGRAM_CHAT_ID:
        raise RuntimeError("Önce TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID alanlarını doldur.")

    state = load_state()
    print("Bot başladı...")

    try:
        tg_send_message("✅ ETH + USDT.D senaryo botu başlatıldı.\nKomutlar için /yardim")
    except Exception as e:
        print("Telegram başlangıç mesajı gönderilemedi:", e)
        raise

    while True:
        try:
            data = tg_get_updates(offset=state["last_update_id"] + 1)

            if not data.get("ok"):
                time.sleep(POLL_SECONDS)
                continue

            for item in data.get("result", []):
                update_id = item["update_id"]
                state["last_update_id"] = update_id

                message = item.get("message") or item.get("edited_message")
                if not message:
                    continue

                chat_id = str(message.get("chat", {}).get("id", ""))
                if str(chat_id) != str(TELEGRAM_CHAT_ID):
                    continue

                text = message.get("text", "")
                now_ts = time.time()

                if now_ts - state.get("last_reply_ts", 0) < REPLY_COOLDOWN_SECONDS:
                    continue

                try:
                    reply = handle_message(text)
                except requests.exceptions.HTTPError as e:
                    reply = f"İşlem sırasında ağ hatası oluştu:\n{str(e)}"
                except requests.exceptions.RequestException as e:
                    reply = f"Bağlantı hatası oluştu:\n{str(e)}"
                except Exception as e:
                    reply = f"İşlem sırasında hata oluştu:\n{str(e)}"

                try:
                    tg_send_message(reply)
                except Exception as send_err:
                    print("Telegram mesaj gönderme hatası:", send_err)

                state["last_reply_ts"] = now_ts
                save_state(state)

            save_state(state)
            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            print("Bot kapatıldı.")
            break
        except Exception as e:
            print("Ana döngü hatası:", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
