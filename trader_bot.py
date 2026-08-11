import requests
import json
import datetime
import time
import re
import os
from flask import Flask

# ==========================================================
# НАСТРОЙКИ (КЛЮЧИ БЕРУТСЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ RENDER)
# ==========================================================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

MODEL = "deepseek/deepseek-v4-flash"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
STATE_FILE = "trade_state.json"
LAST_RUN_FILE = "last_run.txt"
# ==========================================================

app = Flask(__name__)

# --- ПРОВЕРКА ВРЕМЕНИ (ЕКАТЕРИНБУРГ UTC+5) ---
def is_working_hours():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    hour_ekb = (now_utc.hour + 5) % 24
    return 14 <= hour_ekb < 24

# --- ЗАПРОС ЦЕН С MEXC (публичный, без ключей) ---
def get_prices():
    prices = {}
    for sym in SYMBOLS:
        try:
            url = f"https://api.mexc.com/api/v3/ticker/24hr?symbol={sym}"
            headers = {"User-Agent": "Mozilla/5.0"}
            
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                prices[sym] = {
                    'price': float(data['lastPrice']),
                    'change': float(data['priceChangePercent']),
                    'volume': float(data['quoteVolume'])
                }
            else:
                print(f"⚠️ MEXC {sym}: статус {resp.status_code}")
            time.sleep(0.8) # Защита от блокировки
            
        except Exception as e:
            print(f"❌ Ошибка получения цены для {sym} (MEXC): {e}")
            continue
    return prices

# --- ЗАПРОС К OPENROUTER (ИИ) ---
def ask_ai(prices, trade=None):
    prompt = f"Ты трейдер. Вот ТОП-5 монет за 24ч:\n"
    for sym, d in prices.items():
        prompt += f"{sym}: {d['price']} (изм. {d['change']}%) объем {d['volume']}\n"
    
    if trade:
        prompt += f"\nУ меня открыта сделка: {trade['symbol']} по {trade['entry_price']}. Что делать с ней? Ждать/закрыть?"

    prompt += """
Дай 1 лучший сигнал (действие: LONG / SHORT / CLOSE / HOLD). Ответь строго JSON:
{"symbol": "X", "action": "X", "entry_price": X, "take_profit": X, "stop_loss": X, "reason": "X"}"""

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
    # Проверяем, не прошло ли меньше 2 часов с последнего запуска (чтобы не спамить)
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE, 'r') as f:
                last_run = int(f.read().strip())
            if time.time() - last_run < 7200: # 7200 секунд = 2 часа
                print("⏳ Прошло меньше 2 часов. Пропускаю цикл.")
                return
        except:
            pass
    
    print("⏰ Начинаю 2-часовой анализ...")
    if not is_working_hours():
        print("⏳ Вне рабочего времени (14:00-24:00 Екб). Завершаюсь.")
        return

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

# --- ОБРАБОТЧИК ЗАПРОСОВ (ДЛЯ CRON-JOB) ---
@app.route('/')
def handler():
    # Мгновенно возвращаем ответ, чтобы крон получил 200 ОК за 1 миллисекунду.
    # Бот не будет делать никаких тяжелых вычислений здесь.
    return "OK", 200

# --- ЗАПУСК ВЕБ-СЕРВЕРА ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
