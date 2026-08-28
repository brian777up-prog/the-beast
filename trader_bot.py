import requests
import json
import datetime
import time
import os
import threading
from flask import Flask

# ==========================================================
# НАСТРОЙКИ
# ==========================================================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

MODEL = "deepseek/deepseek-v4-pro"

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
    "HYPEUSDT", "ENAUSDT", "JUPUSDT", "JTOUSDT", "ONDOUSDT",
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
DAILY_LIMIT = 5
COOLDOWN_HOURS = 4
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
            return {'price': float(data['lastPrice']), 'volume': float(data['quoteVolume'])}
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
# СТРАТЕГИЯ "РАБОЧАЯ ЛОШАДКА" (С ЛИМИТАМИ)
# ==========================================================
def check_ema_cross():
    if not is_working_hours():
        return

    print("🏇 Сканер (15 мин): ищу пересечение EMA9/EMA21 (с лимитами)...")
    
    state = load_state()
    new_state = {}

    # Определяем сегодняшний день (UTC) для дневного лимита
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    
    # Проверяем дневной лимит
    if state.get('date') != today:
        state = {'date': today}
    
    signals_today = state.get('signals_today', 0)

    # Если уже достигнут лимит, выходим
    if signals_today >= DAILY_LIMIT:
        print(f"🚫 Дневной лимит сигналов ({DAILY_LIMIT}) достигнут. Сегодня больше не отправляем.")
        save_state(state)
        return

    for sym in SYMBOLS:
        # Проверяем, не достигли ли мы лимита внутри цикла
        if signals_today >= DAILY_LIMIT:
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
            # ФИЛЬТР ЛИКВИДНОСТИ (защита от мусора)
            ticker = get_ticker(sym)
            if not ticker:
                continue
            if ticker['price'] < 0.0001 or ticker['volume'] < 500000:
                continue

            # ПРОВЕРКА COOLDOWN (4 часа)
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
            msg += f"Тейк (+{TAKE_PROFIT_PCT}%): {take_profit:.4f}"
            
            send_telegram(msg)
            print(f"✅ Сигнал {direction} по {sym} отправлен!")
            
            # Обновляем состояние
            new_state[sym] = {'signal': direction, 'time': time.time()}
            signals_today += 1
        else:
            # Если пересечения нет, сохраняем предыдущее состояние
            if sym in state:
                new_state[sym] = state[sym]

    # Сохраняем обновленное состояние (включая счетчик сегодняшних сигналов)
    state['signals_today'] = signals_today
    for sym, value in new_state.items():
        state[sym] = value
    save_state(state)

# ==========================================================
# ФОНОВЫЙ ПОТОК
# ==========================================================
def bg_alarm():
    print("🚀 Фоновый поток запущен!", flush=True)
    last_check = time.time() # Ждем 15 минут после запуска
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
