import requests
import json
import datetime
import time
import re
import os
import threading
import xml.etree.ElementTree as ET
from flask import Flask

# ==========================================================
# НАСТРОЙКИ
# ==========================================================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

MODEL = "deepseek/deepseek-v4-pro"

TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 4.0))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 2.0))

# ==========================================================
# РАСШИРЕННЫЙ СПИСОК ДО 100 МОНЕТ
# ==========================================================
SYMBOLS = [
    # ТОП-25 (базовый костяк)
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "DOTUSDT",
    "AVAXUSDT", "MATICUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT",
    "BCHUSDT", "XLMUSDT", "PAXGUSDT", "FILUSDT", "TONUSDT",
    "SHIBUSDT", "NEARUSDT", "APTUSDT", "ZECUSDT", "GRTUSDT",
    # Следующие 25 (волатильные и топ-альты)
    "WLDUSDT", "FARTCOINUSDT", "GUNUSDT", "SUIUSDT", "SEIUSDT",
    "INJUSDT", "RNDRUSDT", "FETUSDT", "TAOUSDT", "AAVEUSDT",
    "MKRUSDT", "CRVUSDT", "ARBUSDT", "OPUSDT", "STXUSDT",
    "ALGOUSDT", "HBARUSDT", "KASUSDT", "ICPUSDT", "VETUSDT",
    "EGLDUSDT", "RUNEUSDT", "ENSUSDT", "LDOUSDT", "QNTUSDT",
    # Ещё 25 (HYPE, ENA и другие активные альты)
    "HYPEUSDT", "ENAUSDT", "JUPUSDT", "JTOUSDT", "ONDOUSDT",
    "TIAUSDT", "PYTHUSDT", "AEVOUSDT", "WIFUSDT", "POPCATUSDT",
    "PENGUUSDT", "PNUTUSDT", "ACTUSDT", "BONKUSDT", "NOTUSDT",
    "DOGSUSDT", "HMSTRUSDT", "CATIUSDT", "PIXELUSDT", "ALTUSDT",
    "SAGAUSDT", "DYMUSDT", "STRKUSDT", "MANTAUSDT", "ETHFIUSDT",
    # НОВЫЕ 25 МОНЕТ (до 100)
    "PEPEUSDT", "FLOKIUSDT", "OMUSDT", "ETCUSDT", "XTZUSDT",
    "SANDUSDT", "MANAUSDT", "GALAUSDT", "IMXUSDT", "FLOWUSDT",
    "KAVAUSDT", "ZRXUSDT", "LRCUSDT", "DYDXUSDT", "BLURUSDT",
    "1INCHUSDT", "RAYUSDT", "ZROUSDT", "WUSDT", "GASUSDT",
    "API3USDT", "ARUSDT", "JASMYUSDT", "RSRUSDT", "SYNUSDT"
]

STATE_FILE = "trade_state.json"
LAST_RUN_FILE = "last_run.txt"
IMPULSE_STATE_FILE = "impulse_state.json"
# ==========================================================

app = Flask(__name__)

# ==========================================================
# МОДУЛЬ RSS-НОВОСТЕЙ (15 МИНУТ)
# ==========================================================
NEWS_CACHE = {"last_update": 0, "headlines": []}

RSS_FEEDS = [
    "https://cointelegraph.com/feed",
    "https://www.coindesk.com/feed/",
    "https://cryptonews.com/news/feed/"
]

def fetch_rss_headlines():
    headlines = []
    for feed_url in RSS_FEEDS:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(feed_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                for item in root.findall('.//item'):
                    title_elem = item.find('title')
                    if title_elem is not None and title_elem.text:
                        headlines.append(title_elem.text.strip())
        except Exception as e:
            print(f"⚠️ Ошибка парсинга {feed_url}: {e}")
    unique_headlines = list(dict.fromkeys(headlines))[:5]
    return unique_headlines

def update_news_cache():
    global NEWS_CACHE
    now = time.time()
    if now - NEWS_CACHE["last_update"] < 900:
        return NEWS_CACHE["headlines"]
    print("📰 Сканирую RSS-ленты для свежих новостей...")
    new_headlines = fetch_rss_headlines()
    if new_headlines:
        NEWS_CACHE = {"last_update": now, "headlines": new_headlines}
        print(f"📰 Новости обновлены: {new_headlines}")
    else:
        print("⚠️ Не удалось получить свежие новости.")
    return NEWS_CACHE["headlines"]
# ==========================================================

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def load_state(filename):
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_state(filename, data):
    try:
        with open(filename, 'w') as f:
            json.dump(data, f)
    except:
        pass

def is_working_hours():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    hour_ekb = (now_utc.hour + 5) % 24
    return (hour_ekb >= 14) or (hour_ekb < 3)

# --- ФУНКЦИИ ДАННЫХ MEXC ---
def get_ticker(symbol):
    try:
        url = f"https://api.mexc.com/api/v3/ticker/24hr?symbol={symbol}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return {
                'price': float(data['lastPrice']),
                'change_24h': float(data['priceChangePercent']),
                'volume_24h': float(data['quoteVolume'])
            }
        else:
            return None
    except Exception as e:
        return None

def get_15m_candles(symbol):
    try:
        url = f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval=15m&limit=30"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            candles = []
            for candle in data:
                candles.append({
                    'open': float(candle[1]),
                    'high': float(candle[2]),
                    'low': float(candle[3]),
                    'close': float(candle[4]),
                    'volume': float(candle[5])
                })
            return candles
        else:
            return None
    except Exception as e:
        return None

# --- ОТПРАВКА В TELEGRAM ---
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text}, timeout=5)
    except Exception as e:
        pass

# ==========================================================
# БАЗОВЫЙ ЦИКЛ (3 ЧАСА)
# ==========================================================
def main_cycle():
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE, 'r') as f:
                last_run = int(f.read().strip())
            if time.time() - last_run < 10800:
                print("⏳ Прошло меньше 3 часов. Пропускаю базовый цикл.")
                return
        except:
            pass

    if not is_working_hours():
        print("⏳ Вне рабочего времени. Базовый цикл пропущен.")
        return

    print("⏰ Запускаю базовый цикл...")
    prices = {}
    for sym in SYMBOLS:
        ticker = get_ticker(sym)
        if ticker:
            prices[sym] = ticker
    if not prices:
        return

    trade = load_state(STATE_FILE)
    news = update_news_cache()
    news_text = "\n".join([f"- {n}" for n in news]) if news else "Нет свежих новостей."

    prompt = f"Ты трейдер. Вот ТОП-5 монет за 24ч (выбери строго ОДНУ для входа):\n"
    for sym, d in list(prices.items())[:5]:
        prompt += f"{sym}: {d['price']} (изм. {d['change_24h']}%) объём {d['volume_24h']}\n"
    if trade:
        prompt += f"\nУ меня открыта сделка: {trade['symbol']} по {trade['entry_price']}. Ждать/закрыть?"
    prompt += f"""
Свежие новости крипторынка:
{news_text}

Выбери строго ОДНУ монету. Тейк +{TAKE_PROFIT_PCT}%, Стоп -{STOP_LOSS_PCT}%.
Ответь строго JSON:
{{"symbol": "X", "action": "LONG/SHORT/HOLD/CLOSE", "entry_price": X, "take_profit": X, "stop_loss": X, "reason": "X"}}"""

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}

    try:
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=30)
        result = resp.json()
        if "error" in result:
            return
        raw = result['choices'][0]['message']['content']
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        signal = json.loads(match.group()) if match else json.loads(raw)

        msg = f"📊 БАЗОВЫЙ: {signal.get('action')} {signal.get('symbol')}\n"
        if signal.get('entry_price'): msg += f"🟢 Вход: {signal['entry_price']}\n"
        if signal.get('take_profit'): msg += f"🎯 Тейк: {signal['take_profit']}\n"
        if signal.get('stop_loss'): msg += f"⛔ Стоп: {signal['stop_loss']}\n"
        msg += f"💬 Причина: {signal.get('reason')}"
        send_telegram(msg)

        if signal['action'] in ["LONG", "SHORT"]:
            save_state(STATE_FILE, {"symbol": signal['symbol'], "entry_price": signal['entry_price']})
        elif signal['action'] == "CLOSE":
            save_state(STATE_FILE, {})
        with open(LAST_RUN_FILE, 'w') as f:
            f.write(str(int(time.time())))
    except Exception as e:
        pass

# ==========================================================
# ЛОВЕЦ ИМПУЛЬСА (ЯДЕРНАЯ КНОПКА: 0.5% И 1.2x)
# ==========================================================
def check_impulse_ai():
    if not is_working_hours():
        return

    print("⚡ Импульсный сканер (15 мин): ищу движения 0.5%+ в ТОП-100...")

    prices = {}
    for sym in SYMBOLS:
        ticker = get_ticker(sym)
        if ticker:
            prices[sym] = ticker
    if not prices:
        return

    impulse_state = load_state(IMPULSE_STATE_FILE)
    new_impulse_state = {}

    for sym in prices.keys():
        candles = get_15m_candles(sym)
        if not candles or len(candles) < 12:
            continue

        current_price = candles[-1]['close']

        # 1 ЧАС = 4 СВЕЧИ
        base_vol = sum(c['volume'] for c in candles[-8:-4]) / 4
        recent_vol = sum(c['volume'] for c in candles[-4:])
        vol_ratio = recent_vol / base_vol if base_vol > 0 else 0

        price_1h_ago = candles[-4]['close']
        change_1h = (current_price - price_1h_ago) / price_1h_ago

        # УСЛОВИЕ 1: Резкое движение на 0.5% за 1 час
        if abs(change_1h) >= 0.005:
            # УСЛОВИЕ 2: Объем в 1.2 раза выше среднего
            if vol_ratio >= 1.2:
                # УСЛОВИЕ 3: Пробой локального экстремума (последние 45 минут)
                last_three = candles[-3:]
                if change_1h > 0 and current_price > max(c['high'] for c in last_three):
                    direction = 'LONG'
                elif change_1h < 0 and current_price < min(c['low'] for c in last_three):
                    direction = 'SHORT'
                else:
                    continue

                print(f"⚡ Обнаружен импульс по {sym}! Направление: {direction}")
                new_impulse_state[sym] = direction
                _trigger_impulse_decision(sym, direction, current_price, change_1h, vol_ratio, candles)

    save_state(IMPULSE_STATE_FILE, {})

def _trigger_impulse_decision(sym, direction, current_price, change_1h, vol_ratio, candles):
    atr = calculate_atr(candles)
    if atr is None or atr == 0:
        atr = current_price * 0.01

    news = update_news_cache()
    news_text = "\n".join([f"- {n}" for n in news]) if news else "Нет свежих новостей."

    tp_dist = atr * 3.0
    sl_dist = atr * 1.5

    prompt = f"""
Ты трейдер. По монете {sym} наблюдается сильный импульс.
Направление: {direction}.
Текущая цена: {current_price}.
Изменение за 1 час: {change_1h*100:.1f}%.
Объем вырос в {vol_ratio:.1f} раз.

Свежие новости:
{news_text}

Это реальный импульс или ложный пробой?
Если да, подтверди действие. Дай Тейк от ATR (x2-x3) и Стоп от ATR (x1-x1.5).
Ответь строго JSON:
{{"confirm": true/false, "reason": "...", "tp_percent": float, "sl_percent": float}}
"""

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}

    try:
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=30)
        result = resp.json()
        if "error" in result:
            return
        raw = result['choices'][0]['message']['content']
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        decision = json.loads(match.group()) if match else json.loads(raw)

        if decision.get('confirm') is True:
            tp_percent = decision.get('tp_percent', (tp_dist / current_price) * 100)
            sl_percent = decision.get('sl_percent', (sl_dist / current_price) * 100)

            if direction == 'SHORT':
                tp_percent = -abs(tp_percent)
                sl_percent = abs(sl_percent)
            elif direction == 'LONG':
                tp_percent = abs(tp_percent)
                sl_percent = -abs(sl_percent)

            tp_percent = max(-20.0, min(20.0, tp_percent))
            sl_percent = max(-20.0, min(20.0, sl_percent))

            msg = f"⚡ ЛОВЕЦ ИМПУЛЬСА (ИИ): {direction} {sym}\n"
            msg += f"🟢 Вход: {current_price:.4f}\n"
            msg += f"🎯 Тейк ({tp_percent:.1f}%): {current_price * (1 + tp_percent/100):.4f}\n"
            msg += f"⛔ Стоп ({sl_percent:.1f}%): {current_price * (1 + sl_percent/100):.4f}\n"
            msg += f"💬 Причина: {decision.get('reason')}"
            send_telegram(msg)
            print(f"✅ DeepSeek подтвердила {direction} по {sym}. Сигнал отправлен!")
            return
    except Exception as e:
        print(f"❌ Ошибка DeepSeek по {sym}: {e}")

# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ATR ---
def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    tr_values = []
    for i in range(1, len(candles)):
        high = candles[i]['high']
        low = candles[i]['low']
        prev_close = candles[i-1]['close']
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
    if len(tr_values) < period:
        return None
    return sum(tr_values[-period:]) / period

# ==========================================================
# ФОНОВЫЙ ПОТОК
# ==========================================================
def bg_alarm():
    print("🚀 Фоновый поток запущен!", flush=True)
    last_impulse_check = 0
    last_main_check = 0

    while True:
        try:
            now = time.time()

            if now - last_main_check >= 1800:
                main_cycle()
                last_main_check = now

            if now - last_impulse_check >= 900:
                check_impulse_ai()
                last_impulse_check = now

            time.sleep(30)
        except Exception as e:
            print(f"⚠️ Ошибка фонового потока: {e}")
            time.sleep(300)

# --- ОБРАБОТЧИК ЗАПРОСОВ (ДЛЯ RENDER) ---
@app.route('/')
def handler():
    return "OK", 200

# --- ЗАПУСК ---
if __name__ == "__main__":
    alarm_thread = threading.Thread(target=bg_alarm)
    alarm_thread.daemon = True
    alarm_thread.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=10000)
