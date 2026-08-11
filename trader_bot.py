import requests
import json
import datetime
import time
import re
import os
import sys

# ==========================================================
# НАСТРОЙКИ (ВСТАВЬ СВОИ ДАННЫЕ)
# ==========================================================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")  # Твой ключ от OpenRouter
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN") # Твой токен ТГ
TG_CHAT_ID = os.getenv("TG_CHAT_ID")  # Узнай через @userinfobot

MODEL = "deepseek/deepseek-v4-flash-0423"  # Быстрая, дешевая, знает русский
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"] # ТОП-5
STATE_FILE = "trade_state.json"
# ==========================================================

# --- ПРОВЕРКА ВРЕМЕНИ (ЕКАТЕРИНБУРГ UTC+5) ---
def is_working_hours():
    now_utc = datetime.datetime.utcnow()
    hour_ekb = (now_utc.hour + 5) % 24
    return 14 <= hour_ekb < 24

# --- ЗАПРОС ЦЕН С BYBIT ---
def get_prices():
    prices = {}
    for sym in SYMBOLS:
        try:
            url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={sym}"
            resp = requests.get(url).json()
            if resp['retCode'] == 0:
                tick = resp['result']['list'][0]
                prices[sym] = {
                    'price': float(tick['lastPrice']),
                    'change': float(tick['change24h']) if tick['change24h'] else 0.0,
                    'volume': float(tick['volume24h'])
                }
        except:
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
        print(f"❌ Ошибка сети или парсинга: {e}")
        return None

# --- ОТПРАВКА В TELEGRAM ---
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text})
    except:
        pass

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

# --- ГЛАВНЫЙ ЦИКЛ ---
def main():
    print("⏰ Бот проснулся.")
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

    # Формируем сообщение
    msg = f"📊 {signal.get('action')} {signal.get('symbol')}\n"
    if signal.get('entry_price'): msg += f"🟢 Вход: {signal['entry_price']}\n"
    if signal.get('take_profit'): msg += f"🎯 Тейк: {signal['take_profit']}\n"
    if signal.get('stop_loss'): msg += f"⛔ Стоп: {signal['stop_loss']}\n"
    msg += f"💬 Причина: {signal.get('reason')}"

    send_telegram(msg)
    print(f"✅ Отправлено: {msg}")

    # Обновляем состояние
    if signal['action'] in ["LONG", "SHORT"]:
        save_state({"symbol": signal['symbol'], "entry_price": signal['entry_price']})
    elif signal['action'] == "CLOSE":
        clear_state()

if __name__ == "__main__":
    main()