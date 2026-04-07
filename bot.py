import os
import time
import json
import requests
from statistics import mean

print("BOT DOSYASI YUKLENDI", flush=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", ""))

STATE_FILE = "state.json"
POLL_SECONDS = 25
HTTP_TIMEOUT = 20

ETH_SYMBOL = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"

AUTO_SCAN_ENABLED_DEFAULT = True
AUTO_SCAN_INTERVAL_SECONDS = 300
SIGNAL_COOLDOWN_SECONDS = 3600

ETH_FAST_EMA = 20
ETH_SLOW_EMA = 50
BTC_FAST_EMA = 20
BTC_SLOW_EMA = 50
ATR_PERIOD = 14

MARKET_FAST_EMA = 6
MARKET_SLOW_EMA = 18

CG_CACHE_TTL_SECONDS = 180
HISTORY_LIMIT = 240

CG_IDS = ",".join([
    "bitcoin",
    "ethereum",
    "tether",
    "usd-coin",
    "dai",
    "first-digital-usd",
    "true-usd",
    "pax-dollar",
    "frax",
    "paypal-usd",
    "usdd"
])

STABLE_IDS = [
    "tether",
    "usd-coin",
    "dai",
    "first-digital-usd",
    "true-usd",
    "pax-dollar",
    "frax",
    "paypal-usd",
    "usdd"
]


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
    state.setdefault("manual_usdtd_override", None)
    state.setdefault("cg_cache", {})
    state.setdefault("history", {
        "usdtd": [],
        "total": [],
        "total3": []
    })
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


# -------------------------------------------------
# EXCHANGE DATA
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
            log(f"{symbol} veri kaynagi: {name}")
            return closes, highs, lows, name
        except Exception as e:
            err = f"{name} basarisiz: {e}"
            errors.append(err)
            log(err)

    raise RuntimeError("Tum veri kaynaklari basarisiz:\n" + "\n".join(errors))


# -------------------------------------------------
# COINGECKO MARKET PROXIES
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

        stable_mc = 0.0
        for cid in STABLE_IDS:
            stable_mc += float(caps.get(cid, {}).get("usd_market_cap", 0.0))

        usdt_mc = float(caps.get("tether", {}).get("usd_market_cap", 0.0))

        if total_mc <= 0 or btc_mc <= 0 or eth_mc <= 0 or usdt_mc <= 0:
            raise RuntimeError("CoinGecko verisi eksik veya gecersiz")

        total3_mc = max(total_mc - btc_mc - eth_mc, 0.0)

        usdtd = (usdt_mc / total_mc) * 100.0
        stable_d = (stable_mc / total_mc) * 100.0

        result = {
            "ts": now,
            "total_mc": total_mc,
            "btc_mc": btc_mc,
            "eth_mc": eth_mc,
            "total3_mc": total3_mc,
            "usdt_mc": usdt_mc,
            "stable_mc": stable_mc,
            "usdtd": usdtd,
            "stable_d": stable_d,
            "source": "COINGECKO_LIVE"
        }

        state["cg_cache"] = result
        save_state(state)
        return result

    except Exception as e:
        if cache:
            stale = dict(cache)
            stale["source"] = "COINGECKO_STALE_CACHE"
            log(f"COINGECKO LIVE BASARISIZ, CACHE KULLANILIYOR: {e}")
            return stale
        raise


# -------------------------------------------------
# INDICATORS
# -------------------------------------------------
def ema(data, period):
    if len(data) < period:
        raise RuntimeError(f"EMA icin veri yetersiz. period={period}")
    k = 2 / (period + 1)
    e = mean(data[:period])
    for x in data[period:]:
        e = x * k + e * (1 - k)
    return e


def true_range(high, low, prev_close):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        raise RuntimeError("ATR icin veri yetersiz")
    trs = []
    for i in range(1, len(closes)):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))
    return mean(trs[-period:])


def append_history(state, key, value):
    hist = state["history"].get(key, [])
    hist.append({
        "ts": int(time.time()),
        "value": float(value)
    })
    hist = hist[-HISTORY_LIMIT:]
    state["history"][key] = hist


def get_hist_values(state, key):
    return [x["value"] for x in state["history"].get(key, [])]


def fmt_billions(v):
    return f"{v / 1_000_000_000:.2f}B"


def fmt_trillions(v):
    return f"{v / 1_000_000_000_000:.3f}T"


# -------------------------------------------------
# ANALYSIS CORE
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
    stable_d_now = float(proxies["stable_d"])

    append_history(state, "usdtd", usdtd_now)
    append_history(state, "total", total_now)
    append_history(state, "total3", total3_now)
    save_state(state)

    usdtd_values = get_hist_values(state, "usdtd")
    total_values = get_hist_values(state, "total")
    total3_values = get_hist_values(state, "total3")

    if len(usdtd_values) >= MARKET_SLOW_EMA:
        usdtd_fast = ema(usdtd_values, MARKET_FAST_EMA)
        usdtd_slow = ema(usdtd_values, MARKET_SLOW_EMA)
    else:
        usdtd_fast = usdtd_now
        usdtd_slow = usdtd_now

    if len(total_values) >= MARKET_SLOW_EMA:
        total_fast = ema(total_values, MARKET_FAST_EMA)
        total_slow = ema(total_values, MARKET_SLOW_EMA)
    else:
        total_fast = total_now
        total_slow = total_now

    if len(total3_values) >= MARKET_SLOW_EMA:
        total3_fast = ema(total3_values, MARKET_FAST_EMA)
        total3_slow = ema(total3_values, MARKET_SLOW_EMA)
    else:
        total3_fast = total3_now
        total3_slow = total3_now

    snapshot = {
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
        "stable_d_now": stable_d_now,
        "eth_source": eth_source,
        "btc_source": btc_source,
        "market_source": proxies["source"],
        "usdt_mc": proxies["usdt_mc"],
        "stable_mc": proxies["stable_mc"]
    }

    state["last_snapshot"] = snapshot
    save_state(state)
    return snapshot


# -------------------------------------------------
# SIGNAL ENGINE
# -------------------------------------------------
def build_signal(snapshot):
    eth_price = snapshot["eth_price"]
    eth_trend = snapshot["eth_trend"]
    btc_regime = snapshot["btc_regime"]
    eth_atr = snapshot["eth_atr"]

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
        eth_trend in ("YUKARI", "GUCLU YUKARI") and
        btc_regime in ("RISK-ON", "NOTR")
    )

    short_ok = (
        usdtd_bearish and
        total_bearish and
        total3_bearish and
        eth_trend in ("ASAGI", "GUCLU ASAGI") and
        btc_regime in ("RISK-OFF", "NOTR")
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
        reason = "USDT.D zayif + TOTAL guclu + TOTAL3 guclu + ETH yukari"
    else:
        side = "SHORT"
        entry = eth_price
        stop = eth_price + 1.25 * eth_atr
        tp1 = eth_price - 1.10 * eth_atr
        tp2 = eth_price - 2.00 * eth_atr
        tp3 = eth_price - 3.00 * eth_atr
        reason = "USDT.D guclu + TOTAL zayif + TOTAL3 zayif + ETH asagi"

    rr = abs(tp2 - entry) / max(abs(entry - stop), 1e-9)
    if rr < 1.2:
        return None

    signal_hash = f"{side}|{round(entry,2)}|{round(stop,2)}|{round(snapshot['usdtd_now'],4)}|{round(snapshot['total_now'],0)}|{round(snapshot['total3_now'],0)}"

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
        f"🔥 <b>OTOMATIK {signal['side']} SINYALI</b>\n\n"
        f"ETH: <b>{snapshot['eth_price']:.2f}</b>\n"
        f"ETH Trend: <b>{snapshot['eth_trend']}</b>\n"
        f"BTC Rejimi: <b>{snapshot['btc_regime']}</b>\n\n"
        f"USDT.D Proxy: <b>{snapshot['usdtd_now']:.3f}</b>\n"
        f"USDT EMA{MARKET_FAST_EMA}/{MARKET_SLOW_EMA}: <b>{snapshot['usdtd_fast']:.3f} / {snapshot['usdtd_slow']:.3f}</b>\n"
        f"TOTAL Proxy: <b>{fmt_trillions(snapshot['total_now'])}</b>\n"
        f"TOTAL EMA{MARKET_FAST_EMA}/{MARKET_SLOW_EMA}: <b>{fmt_trillions(snapshot['total_fast'])} / {fmt_trillions(snapshot['total_slow'])}</b>\n"
        f"TOTAL3 Proxy: <b>{fmt_trillions(snapshot['total3_now'])}</b>\n"
        f"TOTAL3 EMA{MARKET_FAST_EMA}/{MARKET_SLOW_EMA}: <b>{fmt_trillions(snapshot['total3_fast'])} / {fmt_trillions(snapshot['total3_slow'])}</b>\n"
        f"Kaynaklar: <b>ETH={snapshot['eth_source']} | BTC={snapshot['btc_source']} | MARKET={snapshot['market_source']}</b>\n\n"
        f"Sebep: <b>{signal['reason']}</b>\n\n"
        f"Giris: <b>{signal['entry']:.2f}</b>\n"
        f"Stop: <b>{signal['stop']:.2f}</b>\n"
        f"TP1: <b>{signal['tp1']:.2f}</b>\n"
        f"TP2: <b>{signal['tp2']:.2f}</b>\n"
        f"TP3: <b>{signal['tp3']:.2f}</b>\n"
        f"RR(TP2): <b>{signal['rr']:.2f}</b>"
    )


# -------------------------------------------------
# TEXT OUTPUTS
# -------------------------------------------------
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
        f"USDT.D Proxy: <b>{snap['usdtd_now']:.3f}</b>\n"
        f"TOTAL Proxy: <b>{fmt_trillions(snap['total_now'])}</b>\n"
        f"TOTAL3 Proxy: <b>{fmt_trillions(snap['total3_now'])}</b>\n"
        f"Stable.D Proxy: <b>{snap['stable_d_now']:.3f}</b>\n\n"
        f"Tarama: <b>{'ACIK' if state.get('auto_scan_enabled') else 'KAPALI'}</b>\n"
        f"Kaynaklar: <b>ETH={snap['eth_source']} | BTC={snap['btc_source']} | MARKET={snap['market_source']}</b>"
    )


def market_text(state):
    snap = analyze_market(state)
    return (
        f"<b>MARKET PROXY DURUMU</b>\n\n"
        f"USDT.D Proxy: <b>{snap['usdtd_now']:.3f}</b>\n"
        f"USDT EMA{MARKET_FAST_EMA}/{MARKET_SLOW_EMA}: <b>{snap['usdtd_fast']:.3f} / {snap['usdtd_slow']:.3f}</b>\n\n"
        f"TOTAL Proxy: <b>{fmt_trillions(snap['total_now'])}</b>\n"
        f"TOTAL EMA{MARKET_FAST_EMA}/{MARKET_SLOW_EMA}: <b>{fmt_trillions(snap['total_fast'])} / {fmt_trillions(snap['total_slow'])}</b>\n\n"
        f"TOTAL3 Proxy: <b>{fmt_trillions(snap['total3_now'])}</b>\n"
        f"TOTAL3 EMA{MARKET_FAST_EMA}/{MARKET_SLOW_EMA}: <b>{fmt_trillions(snap['total3_fast'])} / {fmt_trillions(snap['total3_slow'])}</b>\n\n"
        f"USDT MC: <b>{fmt_billions(snap['usdt_mc'])}</b>\n"
        f"Stable MC: <b>{fmt_billions(snap['stable_mc'])}</b>\n"
        f"Kaynak: <b>{snap['market_source']}</b>"
    )


def usdtd_text(state):
    snap = analyze_market(state)
    return (
        f"<b>USDT DURUMU</b>\n\n"
        f"USDT.D Proxy: <b>{snap['usdtd_now']:.3f}</b>\n"
        f"EMA{MARKET_FAST_EMA}: <b>{snap['usdtd_fast']:.3f}</b>\n"
        f"EMA{MARKET_SLOW_EMA}: <b>{snap['usdtd_slow']:.3f}</b>\n"
        f"USDT MC: <b>{fmt_billions(snap['usdt_mc'])}</b>\n"
        f"Stable.D Proxy: <b>{snap['stable_d_now']:.3f}</b>\n"
        f"Kaynak: <b>{snap['market_source']}</b>"
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
        f"USDT.D: <b>{dom_now:.3f} -> {dom_target:.3f}</b>\n"
        f"Delta: <b>{delta:+.3f}</b>\n\n"
        f"Zayif: <b>{weak_target:.2f}</b> ({weak_pct:+.2f}%)\n"
        f"Baz: <b>{base_target:.2f}</b> ({base_pct:+.2f}%)\n"
        f"Guclu: <b>{strong_target:.2f}</b> ({strong_pct:+.2f}%)"
    )


def help_text():
    return (
        "<b>Komutlar</b>\n\n"
        "/durum\n"
        "/usdtd\n"
        "/market\n"
        "/sinyal\n"
        "/otomatikac\n"
        "/otomatikkapat\n"
        "/senaryo 7.851 7.800\n"
        "/usdtdset 7.851\n"
        "/usdtdauto\n"
        "/yardim"
    )


# -------------------------------------------------
# COMMANDS
# -------------------------------------------------
def handle_command(state, text):
    text = (text or "").strip()

    if text in ("/start", "/yardim", "/help"):
        return help_text()

    if text.startswith("/durum"):
        return status_text(state)

    if text.startswith("/usdtdauto"):
        state["manual_usdtd_override"] = None
        save_state(state)
        return "✅ USDT.D tekrar otomatik moda alindi."

    if text.startswith("/usdtdset"):
        parts = text.split()
        if len(parts) != 2:
            return "Kullanim: /usdtdset 7.851"
        try:
            val = float(parts[1].replace(",", "."))
        except ValueError:
            return "Gecersiz sayi."
        state["manual_usdtd_override"] = val
        save_state(state)
        return f"✅ USDT.D manuel override aktif: {val:.3f}"

    if text.startswith("/usdtd"):
        return usdtd_text(state)

    if text.startswith("/market"):
        return market_text(state)

    if text.startswith("/otomatikac"):
        state["auto_scan_enabled"] = True
        save_state(state)
        return "✅ Otomatik sinyal taramasi acildi."

    if text.startswith("/otomatikkapat"):
        state["auto_scan_enabled"] = False
        save_state(state)
        return "🛑 Otomatik sinyal taramasi kapatildi."

    if text.startswith("/sinyal"):
        snap = analyze_market(state)
        sig = build_signal(snap)
        if not sig:
            return (
                "Su an net sinyal yok.\n\n"
                f"ETH Trend: {snap['eth_trend']}\n"
                f"BTC Rejimi: {snap['btc_regime']}\n"
                f"USDT.D: {snap['usdtd_now']:.3f}\n"
                f"TOTAL: {fmt_trillions(snap['total_now'])}\n"
                f"TOTAL3: {fmt_trillions(snap['total3_now'])}"
            )
        return format_signal(snap, sig)

    if text.startswith("/senaryo"):
        parts = text.split()
        if len(parts) != 3:
            return "Hatali kullanim.\nOrnek:\n/senaryo 7.851 7.800"
        try:
            dom_now = float(parts[1].replace(",", "."))
            dom_target = float(parts[2].replace(",", "."))
        except ValueError:
            return "USDT degerleri sayi olmali."
        return scenario_text(state, dom_now, dom_target)

    return "Komut taninmadi. /yardim"


# -------------------------------------------------
# AUTO SCAN
# -------------------------------------------------
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


# -------------------------------------------------
# MAIN
# -------------------------------------------------
def main():
    log("MAIN BASLADI")

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN bos")

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID bos")

    log(f"TOKEN VAR: {TELEGRAM_BOT_TOKEN[:10]}...")
    log(f"CHAT ID: {TELEGRAM_CHAT_ID}")

    state = load_state()

    try:
        tg_send("✅ ETH + USDT.D + TOTAL + TOTAL3 botu aktif.\nKomutlar: /yardim")
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
                log(f"GELEN MESAJ: {text}")

                try:
                    reply = handle_command(state, text)
                except Exception as e:
                    reply = f"HATA:\n{str(e)}"
                    log(f"KOMUT HATASI: {e}")

                try:
                    tg_send(reply)
                except Exception as e:
                    log(f"TELEGRAM GONDERIM HATASI: {e}")

                save_state(state)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log(f"ANA DONGU HATASI: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
