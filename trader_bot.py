import requests
import json
import datetime
import time
import re
import os
import threading
from flask import Flask

# ==========================================================
# НАСТРОЙКИ
# ==========================================================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

# МОДЕЛЬ (DeepSeek V4 Pro)
MODEL = "deepseek/deepseek-v4-pro"

# Проценты для базового режима
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 4.0))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 2.0))

# ФИКСИРОВАННЫЙ СПИСОК ТОП-25 МОНЕТ
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "DOTUSDT",
    "AVAXUSDT", "MATICUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT",
    "BCHUSDT", "XLMUSDT", "PAXGUSDT", "FILUSDT", "TONUSDT",
    "SHIBUSDT", "NEARUSDT", "APTUSDT", "ZECUSDT", "GRTUSDT",
    # НОВЫЕ 25 (из ТОП-100 + твои запрошенные)
    "WLDUSDT",      # Запрошено
    "FARTCOINUSDT", # Запрошено
    "GUNUSDT",      # Запрошено
    "SUIUSDT", "SEIUSDT", "INJUSDT", "RNDRUSDT", "FETUSDT",
    "TAOUSDT", "AAVEUSDT", "MKRUSDT", "CRVUSDT", "ARBUSDT",
    "OPUSDT", "STXUSDT", "ALGOUSDT", "HBARUSDT", "KASUSDT",
    "ICPUSDT", "VETUSDT", "EGLDUSDT", "RUNEUSDT", "ENSUSDT",
    "LDOUSDT", "QNTUSDT"
]

STATE_FILE = "trade_state.json"
LAST_RUN_FILE = "last_run.txt"
DUMP_STATE_FILE = "dump_state.json"
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
    return (hour_ekb >= 14) or (hour_ekb < 3)

# ==========================================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С ДАННЫМИ MEXC (СПОТ)
# ==========================================================

# ЗАПРОС ЦЕН ПО КОНКРЕТНОЙ МОНЕТЕ
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

# ЗАПРОС 15-МИНУТНЫХ СВЕЧЕЙ (30 ШТУК = 7.5 ЧАСОВ)
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

# --- ФИЛЬТР EMA/ATR (ОТСЕКАЕМ БОКОВИК) ---
def calculate_ema(candles, period=20):
    if len(candles) < period:
        return None
    k = 2 / (period + 1)
    ema = candles[0]['close']
    for i in range(1, len(candles)):
        ema = (candles[i]['close'] - ema) * k + ema
    return ema

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

def is_choppy_market(candles):
    if len(candles) < 30:
        return True
    ema = calculate_ema(candles)
    atr = calculate_atr(candles)
    if ema is None or atr is None:
        return True
    last_close = candles[-1]['close']
    # Если цена в коридоре 1.5 ATR от EMA20 — это боковик
    if abs(last_close - ema) < (atr * 1.0):
        return True
    return False

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

    # Собираем цены по нашим монетам
    prices = {}
    for sym in SYMBOLS:
        ticker = get_ticker(sym)
        if ticker:
            prices[sym] = ticker
    if not prices:
        return

    trade = load_state(STATE_FILE)

    # Формируем промпт (без новостей)
    prompt = f"Ты трейдер. Вот ТОП-25 монет за 24ч (выбери строго ОДНУ для входа):\n"
    for sym, d in list(prices.items())[:5]:  # Для базы берем только первые 5 (экономия токенов)
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
# ЛОВЕЦ ДАМПОВ 3.0 (ДВА СЦЕНАРИЯ + EMA/ATR)
# ==========================================================
def check_pump_dump_ai():
    if not is_working_hours():
        return

    print("🎯 Сканер (15 мин): ищу пампы по 2-м сценариям в ТОП-25...")

    # Собираем цены по всем монетам списка
    prices = {}
    for sym in SYMBOLS:
        ticker = get_ticker(sym)
        if ticker:
            prices[sym] = ticker
    if not prices:
        return

    dump_state = load_state(DUMP_STATE_FILE)
    new_dump_state = {}

    for sym in prices.keys():
        candles = get_15m_candles(sym)
        if not candles or len(candles) < 30:
            continue

        # --- ФИЛЬТР EMA/ATR ---
        if is_choppy_market(candles):
            print(f"⚠️ Монета {sym} в боковике (EMA/ATR). Пропускаю.")
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

        # --- ЭТАП 1: ОБНАРУЖЕНИЕ ПАМПА (Два сценария) ---
        if sym not in dump_state:
            is_strong = (change_3h >= 0.02 and vol_ratio >= 2.2)
            is_moderate = (change_3h >= 0.01 and vol_ratio >= 1.5)

            if is_strong or is_moderate:
                print(f"⚡ Обнаружен памп по {sym}! Тип: {'СИЛЬНЫЙ' if is_strong else 'УМЕРЕННЫЙ'}")
                new_dump_state[sym] = {
                    'detected': True,
                    'peak_price': current_price,
                    'pump_start_price': price_3h_ago,
                    'type': 'strong' if is_strong else 'moderate'
                }

        # --- ЭТАП 2: ОБНАРУЖЕНИЕ РАЗВОРОТА ---
        elif sym in dump_state and dump_state[sym].get('detected'):
            entry = dump_state[sym]
            peak = entry['peak_price']
            drop_percent = (current_price - peak) / peak
            is_red_candle = candles[-1]['close'] < candles[-1]['open']

            target_drop = 0.005 if entry.get('type') == 'strong' else 0.0025  # 0.5% или 0.25%

            if drop_percent <= -target_drop and is_red_candle:
                print(f"🎯 Разворот по {sym}! Падение {abs(drop_percent)*100:.2f}% от пика. Бужу DeepSeek...")

                prompt = f"""
Ты трейдер. За 3 часа по {sym}:
- Цена выросла на {change_3h*100:.1f}%
- Объем вырос в {vol_ratio:.1f} раз.
Цена достигла пика {peak}, упала на {abs(drop_percent)*100:.2f}% и свеча красная.

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

                        tp_percent = max(-12.0, min(-2.0, tp_percent))
                        sl_percent = max(1.0, min(6.0, sl_percent))

                        msg = f"🎯 ЛОВЕЦ ДАМПОВ (ИИ): SHORT {sym}\n"
                        msg += f"🟢 Вход (пик): {entry_price:.4f}\n"
                        msg += f"🎯 Тейк ({tp_percent:.1f}%): {entry_price * (1 + tp_percent/100):.4f}\n"
                        msg += f"⛔ Стоп (+{sl_percent:.1f}%): {entry_price * (1 + sl_percent/100):.4f}\n"
                        msg += f"💬 Причина: {decision.get('reason')}"
                        send_telegram(msg)
                        print(f"✅ DeepSeek подтвердила дамп по {sym}. Сигнал отправлен!")
                        continue
                except Exception as e:
                    print(f"❌ Ошибка DeepSeek по {sym}: {e}")
            else:
                new_dump_state[sym] = entry

    save_state(DUMP_STATE_FILE, new_dump_state)

# ==========================================================
# ФОНОВЫЙ ПОТОК
# ==========================================================
def bg_alarm():
    last_dump_check = 0
    last_main_check = 0

    while True:
        try:
            now = time.time()

            # Базовый цикл (каждые 30 минут)
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
