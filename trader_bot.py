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

# Модель ИИ
MODEL = "deepseek/deepseek-v4-flash"

# Проценты для Тейка и Стопа (можно менять через переменные окружения)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 4.0))   # по умолчанию 4%
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 2.0))       # по умолчанию 2%

STATE_FILE = "trade_state.json"
LAST_RUN_FILE = "last_run.txt"
# ==========================================================

app = Flask(__name__)

# --- ПРОВЕРКА ВРЕМЕНИ (ЕКАТЕРИНБУРГ UTC+5) ---
def is_working_hours():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    hour_ekb = (now_utc.hour + 5) % 24
    return 14 <= hour_ekb < 24

# --- ДИНАМИЧЕСКИЙ ТОП-5 МОНЕТ ПО ОБЪЕМУ С MEXC ---
def get_prices():
    prices = {}
    try:
        url = "https://api.mexc.com/api/v3/ticker/24hr"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)

        if resp.status_code == 200:
            all_tickers = resp.json()
            # Фильтруем только USDT пары
            usdt_tickers = [t for t in all_tickers if t['symbol'].endswith('USDT')]
            # Сортируем по объему (quoteVolume) по убыванию
            usdt_tickers.sort(key=lambda x: float(x['quoteVolume']), reverse=True)
            # Берем топ-5
            top5 = usdt_tickers[:5]

            for ticker in top5:
                sym = ticker['symbol']
                prices[sym] = {
                    'price': float(ticker['lastPrice']),
                    'change': float(ticker['priceChangePercent']),
                    'volume': float(ticker['quoteVolume'])
                }
            print(f"✅ ТОП-5 по объему: {list(prices.keys())}")
        else:
            print(f"⚠️ MEXC: статус {resp.status_code}")
    except Exception as e:
        print(f"❌ Ошибка получения топ-5 монет: {e}")

    return prices

# --- ЗАПРОС К OPENROUTER (ИИ) ---
def ask_ai(prices, trade=None):
    # Формируем промпт с жесткими процентами
    prompt = f"Ты трейдер. Вот ТОП-5 монет за 24ч по объему:\n"
    for sym, d in prices.items():
        prompt += f"{sym}: {d['price']} (изм. {d['change']}%) объем {d['volume']}\n"

    if trade:
        prompt += f"\nУ меня открыта сделка: {trade['symbol']} по {trade['entry_price']}. Что делать с ней? Ждать/закрыть?"

    # Жесткое требование по процентам
    prompt += f"""
Дай 1 лучший сигнал (действие: LONG / SHORT / CLOSE / HOLD).
Тейк-профит рассчитывай строго как +{TAKE_PROFIT_PCT}% от цены входа.
Стоп-лосс рассчитывай строго как -{STOP_LOSS_PCT}% от цены входа.
Ответь строго JSON:
{{"symbol": "X", "action": "X", "entry_price": X, "take_profit": X, "stop_loss": X, "reason": "X"}}"""

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}]
    }
    try:
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=30)
        result = resp.json()
        if "error" in result:
            print(f"❌ Ошибка OpenRouter: {result['error']}")
            return None
        raw = result['choices'][0]['message']['content']
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        return json.loads(raw)
    except Exception as e:
        print(f"❌ Ошибка сети или парсинга OpenRouter: {e}")
        return None

# --- ОТПРАВКА В TELEGRAM ---
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text}, timeout=5)
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")

# --- ПАМЯТЬ СДЕЛОК ---
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return None

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def clear_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

# --- ГЛАВНЫЙ ЦИКЛ (ОСНОВНАЯ ЛОГИКА) ---
def main_cycle():
    # Проверяем, не прошло ли меньше 2 часов с последнего запуска
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE, 'r') as f:
                last_run = int(f.read().strip())
            if time.time() - last_run < 7200: # 7200 секунд = 2 часа
                print("⏳ Прошло меньше 2 часов. Пропускаю цикл.")
                return
        except:
            pass

    # Проверяем рабочее время
    if not is_working_hours():
        print("⏳ Вне рабочего времени (14:00-24:00 Екб). Завершаюсь.")
        return

    print("⏰ Запускаю анализ...")
    prices = get_prices()
    if not prices:
        print("❌ Нет цен. Завершаюсь.")
        return

    trade = load_state()
    signal = ask_ai(prices, trade)
    if not signal:
        print("❌ Нет сигнала от ИИ.")
        return

    msg = f"📊 {signal.get('action')} {signal.get('symbol')}\n"
    if signal.get('entry_price'): msg += f"🟢 Вход: {signal['entry_price']}\n"
    if signal.get('take_profit'): msg += f"🎯 Тейк: {signal['take_profit']}\n"
    if signal.get('stop_loss'): msg += f"⛔ Стоп: {signal['stop_loss']}\n"
    msg += f"💬 Причина: {signal.get('reason')}"

    send_telegram(msg)
    print(f"✅ Отправлено: {msg}")

    if signal['action'] in ["LONG", "SHORT"]:
        save_state({"symbol": signal['symbol'], "entry_price": signal['entry_price']})
    elif signal['action'] == "CLOSE":
        clear_state()

    # Записываем время успешного запуска
    with open(LAST_RUN_FILE, 'w') as f:
        f.write(str(int(time.time())))

# --- ВСТРОЕННЫЙ БУДИЛЬНИК (ФОНОВЫЙ ПОТОК) ---
def bg_alarm():
    while True:
        try:
            if is_working_hours():
                print("🔔 Рабочее время. Проверяю, не пора ли делать анализ...")
                main_cycle()
                time.sleep(1800)  # 30 минут
            else:
                print("🌙 Ночь или вне рабочего диапазона. Сплю 1 час...")
                time.sleep(3600)  # 1 час
        except Exception as e:
            print(f"⚠️ Ошибка в фоновом будильнике: {e}")
            time.sleep(300)

# --- ОБРАБОТЧИК ЗАПРОСОВ (ДЛЯ RENDER И ПРОВЕРКИ ЖИВУЧЕСТИ) ---
@app.route('/')
def handler():
    # Мгновенный ответ, чтобы тайм-аутов не было
    return "OK", 200

# --- ЗАПУСК ВЕБ-СЕРВЕРА И ФОНОВОГО БУДИЛЬНИКА ---
if __name__ == "__main__":
    # Запускаем будильник в отдельном потоке
    alarm_thread = threading.Thread(target=bg_alarm)
    alarm_thread.daemon = True
    alarm_thread.start()

    # Запускаем Flask-сервер
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
