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
STOP_LOSS_PCT = 3.0   # -3.0% от входа
TAKE_PROFIT_PCT = 4.0 # +4.0% от входа

# Фильтры
MIN_ADX = 15          # Смягченный ADX (боковик отсекается)
RSI_LONG_MAX = 70     # Не покупать, если RSI выше 70
RSI_SHORT_MIN = 30    # Не продавать, если RSI ниже 30
EMA_1H_PERIOD = 50    # Период EMA на 1H для фильтра тренда

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
DAILY_LIMIT = 40
COOLDOWN_HOURS = 4
MIN_INTERVAL_HOURS = 0.5

# ==========================================================
# КЭШ ДЛЯ НОВОСТЕЙ
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

def update_news_cache():
    global NEWS_CACHE
    now = time.time()
    if now - NEWS_CACHE["last_update"] < 900:
        return NEWS_CACHE["headlines"], NEWS_CACHE["sentiment"]
    
    print("📰 Сканирую RSS-ленты для свежих новостей...")
    new_headlines = fetch_rss_headlines()
    if new_headlines:
        news_text = "\n".join(new_headlines)
        sentiment = "Нейтральный"
        NEWS_CACHE = {
            "last_update": now,
            "headlines": new_headlines,
            "sentiment": sentiment
        }
        print(f"📰 Новости обновлены: {new_headlines}")
    else:
        print("⚠️ Не удалось получить свежие новости.")
    return NEWS_CACHE["headlines"], NEWS_CACHE["sentiment"]

# ==========================================================
# ФУНКЦИИ ДАННЫХ MEXC
# ==========================================================
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

def get_candles(symbol, interval='30m', limit=100):
    try:
        url = f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return [float(candle[4]) for candle in data]
        return None
    except:
        return None

# ==========================================================
# РАСЧЕТ ИНДИКАТОРОВ
# ==========================================================
def calculate_ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = values[0]
    for i in range(1, len(values)):
        ema = (values[i] - ema) * k + ema
    return ema

def calculate_rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    tr_list = []
    plus_dm_list = []
    minus_dm_list = []
    
    for i in range(1, len(closes)):
        high = highs[i]
        low = lows[i]
        prev_high = highs[i-1]
        prev_low = lows[i-1]
        prev_close = closes[i-1]
        
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
        
        up_move = high - prev_high
        down_move = prev_low - low
        
        if up_move > down_move and up_move > 0:
            plus_dm_list.append(up_move)
        else:
            plus_dm_list.append(0)
        
        if down_move > up_move and down_move > 0:
            minus_dm_list.append(down_move)
        else:
            minus_dm_list.append(0)
    
    atr = sum(tr_list[-period:]) / period
    if atr == 0:
        return None
    
    plus_di = 100 * (sum(plus_dm_list[-period:]) / period) / atr
    minus_di = 100 * (sum(minus_dm_list[-period:]) / period) / atr
    
    if (plus_di + minus_di) == 0:
        return None
    
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx
    return adx

# ==========================================================
# ОТПРАВКА В TELEGRAM
# ==========================================================
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text}, timeout=5)
    except:
        pass

# ==========================================================
# СТРАТЕГИЯ "РАБОЧАЯ ЛОШАДКА" (30m + 1H фильтр)
# ==========================================================
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
def check_ema_cross():
    if not is_working_hours():
        return

    print("🏇 Сканер (30m) с фильтрами: EMA9/21 + 1H тренд + RSI + ADX...")

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

    last_signal_time = state.get('last_signal_time', 0)
    if (time.time() - last_signal_time) < (MIN_INTERVAL_HOURS * 3600):
        print(f"⏳ Прошло меньше {MIN_INTERVAL_HOURS} часов с последнего сигнала. Пропускаю цикл.")
        save_state(state)
        return

    headlines, sentiment = update_news_cache()
    news_text = "\n".join([f"- {n}" for n in headlines]) if headlines else "Нет свежих новостей."

    sent_in_cycle = 0

    for sym in SYMBOLS:
        if signals_today >= DAILY_LIMIT:
            break
        if sent_in_cycle >= 3:
            break

        # Получаем свечи 30м (для EMA) и 1H (для тренда)
        candles_30m = get_candles(sym, '30m', 100)
        if not candles_30m or len(candles_30m) < 30:
            continue

        # --- РАСЧЕТ EMA 9/21 на 30м ---
        ema9_prev = calculate_ema(candles_30m[:-1], 9)
        ema21_prev = calculate_ema(candles_30m[:-1], 21)
        ema9_curr = calculate_ema(candles_30m, 9)
        ema21_curr = calculate_ema(candles_30m, 21)

        if None in (ema9_prev, ema21_prev, ema9_curr, ema21_curr):
            continue

        is_cross_up = ema9_prev <= ema21_prev and ema9_curr > ema21_curr
        is_cross_down = ema9_prev >= ema21_prev and ema9_curr < ema21_curr

        direction = None
        if is_cross_up:
            direction = 'LONG'
        elif is_cross_down:
            direction = 'SHORT'

        if not direction:
            if sym in state:
                new_state[sym] = state[sym]
            continue

        # --- ТИКЕР (цена и объем) ---
        ticker = get_ticker(sym)
        if not ticker:
            continue
        if ticker['price'] < 0.0001 or ticker['volume'] < 500000:
            continue

        # --- ФИЛЬТР СТАРШЕГО ТАЙМФРЕЙМА (1H) ---
        candles_1h = get_candles(sym, '60m', 60)
        if not candles_1h or len(candles_1h) < EMA_1H_PERIOD:
            continue
        ema50_1h = calculate_ema(candles_1h, EMA_1H_PERIOD)
        if ema50_1h is None:
            continue
        current_price_1h = candles_1h[-1]
        
        if direction == 'LONG' and current_price_1h < ema50_1h:
            print(f"⛔ {sym}: LONG отклонен (цена ниже EMA 50 на 1H)")
            continue
        if direction == 'SHORT' and current_price_1h > ema50_1h:
            print(f"⛔ {sym}: SHORT отклонен (цена выше EMA 50 на 1H)")
            continue

        # --- ФИЛЬТР RSI на 30m ---
        rsi_30m = calculate_rsi(candles_30m, 14)
        if rsi_30m is None:
            continue
        if direction == 'LONG' and rsi_30m > RSI_LONG_MAX:
            print(f"⛔ {sym}: LONG отклонен (RSI 30m {rsi_30m:.1f} > {RSI_LONG_MAX})")
            continue
        if direction == 'SHORT' and rsi_30m < RSI_SHORT_MIN:
            print(f"⛔ {sym}: SHORT отклонен (RSI 30m {rsi_30m:.1f} < {RSI_SHORT_MIN})")
            continue

        # --- ФИЛЬТР RSI на 1H ---
        rsi_1h = calculate_rsi(candles_1h, 14)
        if rsi_1h is not None:
            if direction == 'LONG' and rsi_1h > 70:
                print(f"⛔ {sym}: LONG отклонен (RSI 1H {rsi_1h:.1f} > 70)")
                continue

        # --- ФИЛЬТР ADX (сила тренда) ---
        url = f"https://api.mexc.com/api/v3/klines?symbol={sym}&interval=30m&limit=50"
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if resp.status_code != 200:
                continue
            raw = resp.json()
            highs = [float(c[2]) for c in raw]
            lows = [float(c[3]) for c in raw]
            closes = [float(c[4]) for c in raw]
        except:
            continue
        
        adx = calculate_adx(highs, lows, closes, 14)
        if adx is None or adx < MIN_ADX:
            print(f"⛔ {sym}: отклонен (ADX {adx if adx else 0:.1f} < {MIN_ADX} — боковик)")
            continue

        # --- ПРОВЕРКА КУЛДАУНА ---
        last_signal = state.get(sym, {}).get('signal')
        last_time = state.get(sym, {}).get('time', 0)
        if last_signal == direction and (time.time() - last_time) < (COOLDOWN_HOURS * 3600):
            continue

        # --- ФОРМИРОВАНИЕ СИГНАЛА ---
        current_price = candles_30m[-1]
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
        msg += f"📊 RSI 30m: {rsi_30m:.1f} | ADX: {adx:.1f}\n"
        if rsi_1h:
            msg += f"📊 RSI 1H: {rsi_1h:.1f}\n"
        msg += f"📈 1H тренд: {'ВВЕРХ' if current_price_1h > ema50_1h else 'ВНИЗ'}\n"
        msg += f"📰 Новости: \n{news_text}\n"
        msg += f"🧠 Оценка фона: {sentiment}"

        send_telegram(msg)
        print(f"✅ Сигнал {direction} по {sym} отправлен!")

        state['last_signal_time'] = time.time()
        new_state[sym] = {'signal': direction, 'time': time.time()}
        signals_today += 1
        sent_in_cycle += 1

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
                print(f"⏰ Цикл проверки: {datetime.datetime.now().strftime('%H:%M:%S')}")
                check_ema_cross()
                last_check = now
            time.sleep(30)
        except Exception as e:
            print(f"⚠️ Ошибка: {e}")
            time.sleep(300)

# ==========================================================
# ОБРАБОТЧИК ЗАПРОСОВ (ДЛЯ RENDER)
# ==========================================================
app = Flask(__name__)

@app.route('/')
def handler():
    return "OK", 200

# ==========================================================
# ЗАПУСК
# ==========================================================
if __name__ == "__main__":
    alarm_thread = threading.Thread(target=bg_alarm)
    alarm_thread.daemon = True
    alarm_thread.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
