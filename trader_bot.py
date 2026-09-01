import requests
import json
import datetime
import time
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

# Параметры торговли
STOP_LOSS_PCT = 1.5   # -1.5% от входа
TAKE_PROFIT_PCT = 2.0 # +2.0% от входа

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "DOTUSDT",
    "AVAXUSDT", "MATICUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT",
    "BCHUSDT", "XLMUSDT", "PAXGUSDT", "FILUSDT", "TONUSDT",
    "SHIBUSDT", "NEARUSDT", "APTUSDT", "ZECUSDT", "GRTUSDT",
    "WLDUSDT", "FARTCOINUSDT", "GUNUSDT", "SUIUSDT", "SEIUSDT",
    "INJUSDT", "RNDRUSDT", "FETUSDT", "TAOUSDT", "AAVEUSDT",
    "MKRUSDT", "CRVUSDT", "ARBUSDT", "OPUSDT", "STXUSDT",
    "ALGOUSDT", "HBARUSDT", "KASUSDT", "ICPUSDT", "VETUSDT",
    "EGLDUSDT", "RUNEUSDT", "ENSUSDT", "LDOUSDT", "QNTUSDT",
    "HYPEUSDT", "JUPUSDT", "JTOUSDT", "ONDOUSDT",
    "TIAUSDT", "PYTHUSDT", "AEVOUSDT", "WIFUSDT", "POPCATUSDT",
    "PENGUUSDT", "PNUTUSDT", "ACTUSDT", "BONKUSDT", "NOTUSDT",
    "DOGSUSDT", "HMSTRUSDT", "CATIUSDT", "PIXELUSDT", "ALTUSDT",
    "SAGAUSDT", "DYMUSDT", "STRKUSDT", "MANTAUSDT", "ETHFIUSDT",
    "PEPEUSDT", "FLOKIUSDT", "OMUSDT", "ETCUSDT", "XTZUSDT",
    "SANDUSDT", "MANAUSDT", "GALAUSDT", "IMXUSDT", "FLOWUSDT",
    "KAVAUSDT", "ZRXUSDT", "LRCUSDT", "DYDXUSDT", "BLURUSDT",
    "1INCHUSDT", "RAYUSDT", "ZROUSDT", "WUSDT", "GASUSDT",
    "API3USDT", "ARUSDT", "JASMYUSDT", "RSRUSDT", "SYNUSDT"
]

STATE_FILE = "signal_state.json"
DAILY_LIMIT = 26
COOLDOWN_HOURS = 4
MIN_INTERVAL_HOURS = 0.5 # Минимальный интервал между любыми новыми сигналами

# ==========================================================
# КЭШ ДЛЯ НОВОСТЕЙ (ОБНОВЛЕНИЕ КАЖДЫЕ 15 МИНУТ)
# ==========================================================
NEWS_CACHE = {"last_update": 0, "headlines": [], "sentiment": "Не определён"}

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
    unique_headlines = list(dict.fromkeys(headlines))[:3]
    return unique_headlines

def analyze_news_sentiment(news_text):
    """Отправляет новости в нейросеть и получает краткую оценку фона"""
    if not news_text:
        return "Новостей нет"
    
    prompt = f"""
Проанализируй следующие новости криптовалютного рынка:
{news_text}

Дай краткую оценку: позитивный, нейтральный или негативный фон для рынка.
Ответь строго одним словом: ПОЗИТИВНЫЙ, НЕЙТРАЛЬНЫЙ или НЕГАТИВНЫЙ.
"""

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 100
    }

    try:
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=30)
        result = resp.json()
        if "error" in result:
            print(f"⚠️ Ошибка нейросети: {result['error']}")
            return "Не удалось оценить"
        
        raw = (result['choices'][0]['message']['content'] or "").strip().upper()
        
        if "ПОЗИТИВ" in raw:
            return "Позитивный"
        if "НЕГАТИВ" in raw:
            return "Негативный"
        return "Нейтральный"
    except Exception as e:
        print(f"❌ Ошибка вызова нейросети: {e}")
        return "Не удалось оценить"

def update_news_cache():
    global NEWS_CACHE
    now = time.time()
    if now - NEWS_CACHE["last_update"] < 900:
        return NEWS_CACHE["headlines"], NEWS_CACHE["sentiment"]
    
    print("📰 Сканирую RSS-ленты для свежих новостей...")
    new_headlines = fetch_rss_headlines()
    if new_headlines:
        news_text = "\n".join(new_headlines)
        print("🧠 Запрашиваю оценку фона у нейросети...")
        sentiment = analyze_news_sentiment(news_text)
        NEWS_CACHE = {
            "last_update": now,
            "headlines": new_headlines,
            "sentiment": sentiment
        }
        print(f"📰 Новости обновлены: {new_headlines}")
        print(f"🧠 Оценка фона: {sentiment}")
    else:
        print("⚠️ Не удалось получить свежие новости.")
    return NEWS_CACHE["headlines"], NEWS_CACHE["sentiment"]
# ==========================================================

app = Flask(__name__)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_state(data):
    with open(STATE_FILE, 'w') as f:
        json.dump(data, f)

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
                'volume': float(data['quoteVolume'])
            }
        return None
    except:
        return None

def get_15m_candles(symbol):
    try:
        url = f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval=15m&limit=50"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            candles = []
            for candle in data:
                candles.append(float(candle[4]))
            return candles
        return None
    except:
        return None

# --- РАСЧЕТ EMA ---
def calculate_ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = values[0]
    for i in range(1, len(values)):
        ema = (values[i] - ema) * k + ema
    return ema

# --- ОТПРАВКА В TELEGRAM ---
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text}, timeout=5)
    except:
        pass

# ==========================================================
# СТРАТЕГИЯ "РАБОЧАЯ ЛОШАДКА" (2 СИГНАЛА ЗА ЦИКЛ)
# ==========================================================
def check_ema_cross():
    if not is_working_hours():
        return

    print("🏇 Сканер (15 мин): ищу пересечение EMA9/EMA21 (2 сигнала за цикл)...")

    state = load_state()
    new_state = {}

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if state.get('date') != today:
        state = {'date': today}

    signals_today = state.get('signals_today', 0)
    if signals_today >= DAILY_LIMIT:
        print(f"🚫 Дневной лимит ({DAILY_LIMIT}) достигнут.")
        save_state(state)
        return

    # Проверка равномерности: ждём 2 часа с момента последнего сигнала
    last_signal_time = state.get('last_signal_time', 0)
    if (time.time() - last_signal_time) < (MIN_INTERVAL_HOURS * 3600):
        print(f"⏳ Прошло меньше {MIN_INTERVAL_HOURS} часов с последнего сигнала. Пропускаю цикл.")
        save_state(state)
        return

    # Обновляем новости и получаем оценку фона
    headlines, sentiment = update_news_cache()
    news_text = "\n".join([f"- {n}" for n in headlines]) if headlines else "Нет свежих новостей."

    # Счётчик сигналов за текущий цикл
    sent_in_cycle = 0

    for sym in SYMBOLS:
        if signals_today >= DAILY_LIMIT:
            break

        # Если уже отправили 2 сигнала за этот цикл - выходим
        if sent_in_cycle >= 2:
            break

        candles = get_15m_candles(sym)
        if not candles or len(candles) < 30:
            continue

        ema9_prev = calculate_ema(candles[:-1], 9)
        ema21_prev = calculate_ema(candles[:-1], 21)
        ema9_curr = calculate_ema(candles, 9)
        ema21_curr = calculate_ema(candles, 21)

        if ema9_prev is None or ema21_prev is None or ema9_curr is None or ema21_curr is None:
            continue

        is_cross_up = ema9_prev <= ema21_prev and ema9_curr > ema21_curr
        is_cross_down = ema9_prev >= ema21_prev and ema9_curr < ema21_curr

        direction = None
        if is_cross_up:
            direction = 'LONG'
        elif is_cross_down:
            direction = 'SHORT'

        if direction:
            ticker = get_ticker(sym)
            if not ticker:
                continue
            if ticker['price'] < 0.0001 or ticker['volume'] < 500000:
                continue

            last_signal = state.get(sym, {}).get('signal')
            last_time = state.get(sym, {}).get('time', 0)
            if last_signal == direction and (time.time() - last_time) < (COOLDOWN_HOURS * 3600):
                continue

            current_price = candles[-1]
            if direction == 'LONG':
                stop_loss = current_price * (1 - STOP_LOSS_PCT / 100)
                take_profit = current_price * (1 + TAKE_PROFIT_PCT / 100)
            else:
                stop_loss = current_price * (1 + STOP_LOSS_PCT / 100)
                take_profit = current_price * (1 - TAKE_PROFIT_PCT / 100)

            msg = f"🏇 РАБОЧАЯ ЛОШАДКА: {direction} {sym}\n"
            msg += f"Вход: {current_price:.4f}\n"
            msg += f"Стоп (-{STOP_LOSS_PCT}%): {stop_loss:.4f}\n"
            msg += f"Тейк (+{TAKE_PROFIT_PCT}%): {take_profit:.4f}\n"
            msg += f"📰 Новости: \n{news_text}\n"
            msg += f"🧠 Оценка фона: {sentiment}"

            send_telegram(msg)
            print(f"✅ Сигнал {direction} по {sym} отправлен!")

            # Обновляем время последнего сигнала и состояние монеты
            state['last_signal_time'] = time.time()
            new_state[sym] = {'signal': direction, 'time': time.time()}
            signals_today += 1
            sent_in_cycle += 1
        else:
            if sym in state:
                new_state[sym] = state[sym]

    state['signals_today'] = signals_today
    for sym, value in new_state.items():
        state[sym] = value
    save_state(state)

# ==========================================================
# ФОНОВЫЙ ПОТОК
# ==========================================================
def bg_alarm():
    print("🚀 Фоновый поток запущен!", flush=True)
    last_check = time.time()
    while True:
        try:
            now = time.time()
            if now - last_check >= 900:
                check_ema_cross()
                last_check = now
            time.sleep(30)
        except Exception as e:
            print(f"⚠️ Ошибка: {e}")
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
    app.run(host="0.0.0.0", port=port)
