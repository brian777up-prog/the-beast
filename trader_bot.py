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

# МОДЕЛЬ (Llama 3.3 70B)
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

# --- ЗАПРОС 15-МИНУТНЫХ СВЕЧЕЙ (24 ШТУКИ = 6 ЧАСОВ) ---
def get_15m_candles(symbol):
    try:
        url = f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval=15m&limit=24"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            candles = []
            for candle in data:
                candles.append({
                    'open': float(candle[1]),
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
# БАЗОВЫЙ ЦИКЛ (2 ЧАСА)
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
    prices = get_top_n_prices_from_mexc(5)
    if not prices:
        return

    trade = load_state(STATE_FILE)
    
    prompt = f"Ты трейдер. ТОП-5 монет за 24ч:\n"
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
        if "error" in result: return
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
# ЛОВЕЦ ДАМПОВ 2.0 (3-ЧАСОВОЙ ПАМП + 0.5% РАЗВОРОТ + КРАСНАЯ СВЕЧА)
# ==========================================================
def check_pump_dump_ai():
    if not is_working_hours():
        return

    print("🎯 Мгновенный сканер (15 мин): ищу 3-часовой памп в ТОП-30...")
    prices = get_top_n_prices_from_mexc(30)
    if not prices:
        return

    dump_state = load_state(DUMP_STATE_FILE)
    new_dump_state = {}

    for sym in prices.keys():
        candles = get_15m_candles(sym)
        if not candles or len(candles) < 24:
            continue

        # Первые 12 свечей (3 часа) — базовый объем
        base_vol = sum(c['volume'] for c in candles[:12]) / 12
        # Последние 12 свечей (3 часа) — текущий объем
        recent_vol = sum(c['volume'] for c in candles[12:])
        vol_ratio = recent_vol / base_vol if base_vol > 0 else 0

        # Цена 3 часа назад и сейчас
        price_3h_ago = candles[12]['close']
        current_price = candles[-1]['close']
        change_3h = (current_price - price_3h_ago) / price_3h_ago

        # --- ЭТАП 1: ОБНАРУЖЕНИЕ ПАМПА (рост ≥2% и объем ≥120% за 3 часа) ---
        if sym not in dump_state:
            if change_3h >= 0.02 and vol_ratio >= 2.2:
                print(f"⚡ Обнаружен 3-часовой памп по {sym}! Ставлю метку.")
                new_dump_state[sym] = {
                    'detected': True,
                    'peak_price': current_price,
                    'pump_start_price': price_3h_ago
                }

        # --- ЭТАП 2: ОБНАРУЖЕНИЕ РАЗВОРОТА (падение 0.5% от пика + красная свеча) ---
        elif sym in dump_state and dump_state[sym].get('detected'):
            entry = dump_state[sym]
            peak = entry['peak_price']
            drop_percent = (current_price - peak) / peak
            is_red_candle = candles[-1]['close'] < candles[-1]['open']

            if drop_percent <= -0.005 and is_red_candle:
                print(f"🎯 Разворот по {sym}! Падение 0.5% от пика, свеча красная. Бужу Llama...")
                
                prompt = f"""
Ты трейдер. За 3 часа по {sym}:
- Цена выросла на {change_3h*100:.1f}%
- Объем вырос в {vol_ratio:.1f} раз.
Цена достигла пика {peak}, упала на 0.5% и свеча красная.
Это реальный дамп? Если да, подтверди SHORT.
Дай Тейк в диапазоне от -2% до -12%.
Дай Стоп в диапазоне от +1% до +6%.
Ответь строго JSON:
{{"confirm": true/false, "reason": "...", "tp_percent": float, "sl_percent": float}}
"""
                headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
                data = {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}
                
                try:
                    resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=30)
                    result = resp.json()
                    if "error" in result:
                        continue
                    raw = result['choices'][0]['message']['content']
                    match = re.search(r'\{.*\}', raw, re.DOTALL)
                    decision = json.loads(match.group()) if match else json.loads(raw)
                    
                    if decision.get('confirm') is True:
                        entry_price = peak
                        tp_percent = decision.get('tp_percent', -10.0)
                        sl_percent = decision.get('sl_percent', 5.0)
                        
                        # Жесткие ограничения
                        tp_percent = max(-12.0, min(-2.0, tp_percent))
                        sl_percent = max(1.0, min(6.0, sl_percent))

                        msg = f"🎯 ЛОВЕЦ ДАМПОВ (ИИ): SHORT {sym}\n"
                        msg += f"🟢 Вход (пик): {entry_price:.4f}\n"
                        msg += f"🎯 Тейк ({tp_percent:.1f}%): {entry_price * (1 + tp_percent/100):.4f}\n"
                        msg += f"⛔ Стоп (+{sl_percent:.1f}%): {entry_price * (1 + sl_percent/100):.4f}\n"
                        msg += f"💬 Причина: {decision.get('reason')}"
                        send_telegram(msg)
                        print(f"✅ Llama подтвердила дамп по {sym}. Сигнал отправлен!")
                        continue  # убираем метку
                except Exception as e:
                    print(f"❌ Ошибка Llama по {sym}: {e}")
            else:
                # Если разворота еще нет, переносим метку
                new_dump_state[sym] = entry

    save_state(DUMP_STATE_FILE, new_dump_state)

# --- ФОНОВЫЙ ПОТОК (УПРАВЛЕНИЕ РАСПИСАНИЕМ) ---
def bg_alarm():
    last_dump_check = 0
    last_main_check = 0

    while True:
        try:
            now = time.time()
            
            # Базовый цикл (проверка каждые 30 минут)
            if now - last_main_check >= 1800:
                main_cycle()
                last_main_check = now

            # Ловец дампов (каждые 15 минут)
            if now - last_dump_check >= 900:
                check_pump_dump_ai()
                last_dump_check = now

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
    app.run(host="0.0.0.0", port=port)
