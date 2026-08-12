import requests
import json
import datetime
import time
import re
import os
import threading
from flask import Flask

# ==========================================================
# НАСТРОЙКИ (КЛЮЧИ БЕРУТСЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ RENDER)
# ==========================================================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

# МОДЕЛЬ (Llama 3.3 70B для базового сигнала)
MODEL = "meta-llama/llama-3.3-70b-instruct"

# Проценты для базового режима
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 4.0))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 2.0))

STATE_FILE = "trade_state.json"
LAST_RUN_FILE = "last_run.txt"
DUMP_STATE_FILE = "dump_state.json"  # Память для ловца дампов
# ==========================================================

app = Flask(__name__)

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

# --- ПРОВЕРКА ВРЕМЕНИ (ЕКАТЕРИНБУРГ UTC+5) ---
def is_working_hours():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    hour_ekb = (now_utc.hour + 5) % 24
    return 14 <= hour_ekb < 24

# --- ЗАПРОС ТОП-30 МОНЕТ С MEXC (с фильтром ликвидности) ---
def get_top_n_prices_from_mexc(n=30):
    prices = {}
    try:
        url = "https://api.mexc.com/api/v3/ticker/24hr"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)

        if resp.status_code == 200:
            all_tickers = resp.json()
            # Фильтруем только USDT пары и отсекаем стейблкоины
            valid_tickers = [t for t in all_tickers if t['symbol'].endswith('USDT') 
                             and not t['symbol'].startswith('USDC') 
                             and not t['symbol'].startswith('DAI')
                             and not t['symbol'].startswith('BUSD')]
            
            # Сортируем по объёму за 24ч
            valid_tickers.sort(key=lambda x: float(x['quoteVolume']), reverse=True)
            
            # Фильтр ликвидности: минимум $5,000,000
            filtered_tickers = [t for t in valid_tickers if float(t['quoteVolume']) >= 5000000]
            
            top_n = filtered_tickers[:n]
            
            for ticker in top_n:
                sym = ticker['symbol']
                prices[sym] = {
                    'price': float(ticker['lastPrice']),
                    'change_24h': float(ticker['priceChangePercent']),
                    'volume_24h': float(ticker['quoteVolume'])
                }
            print(f"🔍 ТОП-{n} по объёму: {list(prices.keys())}")
        else:
            print(f"⚠️ MEXC (Топ-{n}): статус {resp.status_code}")
    except Exception as e:
        print(f"❌ Ошибка получения топ-{n}: {e}")

    return prices

# --- ЗАПРОС 15-МИНУТНЫХ СВЕЧЕЙ С MEXC (для Ловца дампов) ---
def get_15m_candle(symbol):
    try:
        url = f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval=15m&limit=5"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # Возвращаем последние 5 свечей: [open, high, low, close, volume]
            candles = []
            for candle in data:
                candles.append({
                    'close': float(candle[4]),
                    'volume': float(candle[5])
                })
            return candles
        else:
            print(f"⚠️ Ошибка свечей {symbol}: {resp.status_code}")
            return None
    except Exception as e:
        print(f"❌ Ошибка получения свечей {symbol}: {e}")
        return None

# --- ОТПРАВКА В TELEGRAM ---
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text}, timeout=5)
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")

# ==========================================================
# БАЗОВЫЙ ЦИКЛ (2 ЧАСА, Llama 3.3)
# ==========================================================
def main_cycle():
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE, 'r') as f:
                last_run = int(f.read().strip())
            if time.time() - last_run < 7200:
                print("⏳ Прошло меньше 2 часов. Пропускаю базовый цикл.")
                return
        except:
            pass

    if not is_working_hours():
        print("⏳ Вне рабочего времени. Базовый цикл пропущен.")
        return

    print("⏰ Запускаю базовый цикл...")
    prices = get_top_n_prices_from_mexc(5) # Для базы берем ТОП-5
    if not prices:
        print("❌ Нет цен. Завершаюсь.")
        return

    trade = load_state(STATE_FILE)
    
    # Промпт для Llama 3.3 (жестко 1 сигнал)
    prompt = f"Ты трейдер. ТОП-5 монет:\n"
    for sym, d in prices.items():
        prompt += f"{sym}: {d['price']} (изм. {d['change_24h']}%) объём {d['volume_24h']}\n"
    if trade:
        prompt += f"\nУ меня открыта сделка: {trade['symbol']} по {trade['entry_price']}. Ждать/закрыть?"
    prompt += f"""
Выбери строго ОДНУ монету. Тейк +{TAKE_PROFIT_PCT}%, Стоп -{STOP_LOSS_PCT}%.
Ответь строго JSON:
{{"symbol": "X", "action": "LONG/SHORT/HOLD/CLOSE", "entry_price": X, "take_profit": X, "stop_loss": X, "reason": "X"}}"""

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}
    
    try:
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=30)
        result = resp.json()
        if "error" in result:
            print(f"❌ Ошибка OpenRouter: {result['error']}")
            return
        raw = result['choices'][0]['message']['content']
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            signal = json.loads(match.group())
        else:
            signal = json.loads(raw)
        
        msg = f"📊 БАЗОВЫЙ: {signal.get('action')} {signal.get('symbol')}\n"
        if signal.get('entry_price'): msg += f"🟢 Вход: {signal['entry_price']}\n"
        if signal.get('take_profit'): msg += f"🎯 Тейк: {signal['take_profit']}\n"
        if signal.get('stop_loss'): msg += f"⛔ Стоп: {signal['stop_loss']}\n"
        msg += f"💬 Причина: {signal.get('reason')}"
        send_telegram(msg)
        print(f"✅ Базовый сигнал отправлен")

        if signal['action'] in ["LONG", "SHORT"]:
            save_state(STATE_FILE, {"symbol": signal['symbol'], "entry_price": signal['entry_price']})
        elif signal['action'] == "CLOSE":
            save_state(STATE_FILE, {})
        with open(LAST_RUN_FILE, 'w') as f:
            f.write(str(int(time.time())))
    except Exception as e:
        print(f"❌ Ошибка базового цикла: {e}")

# ==========================================================
# ЛОВЕЦ ДАМПОВ (ПОЛНОЦЕННЫЙ СКАНЕР НА 15-МИНУТНЫХ СВЕЧАХ)
# ==========================================================
def check_pump_dump():
    if not is_working_hours():
        return

    print("🎯 Запуск Ловца дампов (ТОП-30, 15-мин свечи)...")
    prices_24h = get_top_n_prices_from_mexc(30)
    if not prices_24h:
        return

    # Текущее состояние отслеживания
    dump_state = load_state(DUMP_STATE_FILE)
    current_time = time.time()
    new_dump_state = {}

    for sym in prices_24h.keys():
        # Получаем 15-минутные свечи для этой монеты
        candles = get_15m_candle(sym)
        if not candles or len(candles) < 2:
            continue

        # Последняя и предпоследняя свечи
        last = candles[-1]
        prev = candles[-2]
        # Средний объем за последние 3 свечи (45 минут)
        avg_volume = sum(c['volume'] for c in candles[-3:]) / 3 if len(candles) >= 3 else prev['volume']

        # Этап 1: Обнаружение пампа
        if sym not in dump_state:
            # Изменение цены за 15 минут
            change = (last['close'] - prev['close']) / prev['close']
            # Рост цены >= 5% И объем текущей свечи в 2.5 раза выше среднего
            if change >= 0.05 and last['volume'] > avg_volume * 2.5:
                print(f"⚡ Обнаружен памп по {sym} за 15 мин!")
                new_dump_state[sym] = {
                    'detected_at': current_time,
                    'pump_price': last['close'],
                    'pump_volume': last['volume'],
                    'phase': 'detected'
                }
        
        # Этап 2: Подтверждение дампа (проверка застывания через 30 минут)
        elif sym in dump_state:
            entry = dump_state[sym]
            time_diff = current_time - entry['detected_at']
            
            # Проверяем, прошло ли 30 минут (и не более 45 минут)
            if 1650 < time_diff < 2700: # Примерно 27-45 минут
                # Цена застыла? (изменение за последние 15 минут < 0.5%)
                change_last_15 = (last['close'] - prev['close']) / prev['close']
                # Объем упал на 60% от пикового объема пампа?
                vol_drop = last['volume'] / entry['pump_volume']

                if abs(change_last_15) < 0.005 and vol_drop <= 0.4:
                    print(f"🎯 Дамп подтвержден по {sym}!")
                    # Вход по цене пика пампа (текущая застывшая цена)
                    entry_price = last['close']
                    take_profit = entry_price * 0.90
                    stop_loss = entry_price * 1.05

                    msg = f"🎯 ЛОВЕЦ ДАМПОВ: SHORT {sym}\n"
                    msg += f"🟢 Вход: {entry_price:.4f}\n"
                    msg += f"🎯 Тейк (-10%): {take_profit:.4f}\n"
                    msg += f"⛔ Стоп (+5%): {stop_loss:.4f}\n"
                    msg += f"💬 Причина: Памп завершён, объём иссяк, цена застыла. Начинается дамп."
                    send_telegram(msg)
                    print(f"✅ Дамп-сигнал отправлен по {sym}")
                    # Удаляем из состояния, чтобы не дублировать
                    continue
                else:
                    new_dump_state[sym] = entry
            elif time_diff > 2700:
                # Снимаем метку, если прошло больше 45 минут (истекло окно)
                continue
            else:
                new_dump_state[sym] = entry

    # Сохраняем обновленное состояние
    save_state(DUMP_STATE_FILE, new_dump_state)

# --- ФОНОВЫЙ ПОТОК (УПРАВЛЕНИЕ РАСПИСАНИЕМ) ---
def bg_alarm():
    last_dump_check = 0
    last_main_check = 0

    while True:
        try:
            now = time.time()
            
            # Базовый цикл (проверка каждые 30 минут, но отправка строго раз в 2 часа)
            if now - last_main_check >= 1800:
                main_cycle()
                last_main_check = now

            # Сканер дампа (строго каждые 15 минут)
            if now - last_dump_check >= 900:
                check_pump_dump()
                last_dump_check = now

            time.sleep(30) # Держим цикл активным
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
    app.run(host="0.0.0.0", port=port)
