import os
import re
import json
import time
import base64
import hmac
import hashlib
from flask import Flask, request
import requests
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Токены и ключи из Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

OKX_BASE_URL = "https://www.okx.com"

# Временное хранилище ожидания ввода маржи (в памяти)
PENDING_TRADES = {}

def get_okx_signature(timestamp, method, request_path, body=""):
    """Формирует подпись HMAC SHA256 для V5 API OKX"""
    message = timestamp + method + request_path + body
    mac = hmac.new(OKX_SECRET_KEY.encode('utf-8'), message.encode('utf-8'), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode('utf-8')

def okx_request(method, request_path, body_data=None):
    """Универсальная функция отправки запросов к OKX API"""
    body_str = json.dumps(body_data) if body_data else ""
    timestamp = time.strftime('%Y-%m-%dT%H:%M:%S.', time.gmtime()) + f"{int(time.time() * 1000) % 1000:03d}Z"
    signature = get_okx_signature(timestamp, method, request_path, body_str)

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }

    url = OKX_BASE_URL + request_path
    if method == "GET":
        res = requests.get(url, headers=headers, timeout=10)
    else:
        res = requests.post(url, data=body_str, headers=headers, timeout=10)
    return res.json()

def send_telegram_msg(text):
    """Вспомогательная функция отправки сообщения"""
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    })

def get_max_leverage_and_ticker(inst_id):
    """Получает максимально допустимое плечо инструмента и текущую рыночную цену"""
    max_lev = 20
    last_price = 0.0

    try:
        # 1. Запрос параметров инструмента (макс. плечо)
        inst_res = okx_request("GET", f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}")
        if inst_res.get("code") == "0" and inst_res.get("data"):
            max_lev = int(inst_res["data"][0].get("lever", 20))

        # 2. Если не нашли, ищем через leverage-lanes
        if max_lev == 20:
            lev_res = okx_request("GET", f"/api/v5/public/leverage-lanes?instId={inst_id}&mgnMode=cross")
            if lev_res.get("code") == "0" and lev_res.get("data"):
                levers = [int(item.get("maxLever", 20)) for item in lev_res["data"] if "maxLever" in item]
                if levers:
                    max_lev = max(levers)

        # 3. Запрос цены
        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={inst_id}")
        if ticker_res.get("code") == "0" and ticker_res.get("data"):
            last_price = float(ticker_res["data"][0].get("last", 0.0))

        return max_lev, last_price

    except Exception as e:
        print(f"Ошибка получения данных тикера/плеча: {e}")
        return 20, last_price

def set_okx_leverage(inst_id, leverage):
    """Устанавливает максимальное плечо на OKX"""
    body = {
        "instId": inst_id,
        "lever": str(leverage),
        "mgnMode": "cross"
    }
    return okx_request("POST", "/api/v5/account/set-leverage", body)

import math

def execute_okx_trade(symbol, side_type, margin_usdt):
    """
    Рассчитывает объем позиции в контрактах (ct) с автоматической проверкой 
    минимально допустимой маржи в USDT для выбранной монеты.
    """
    clean_symbol = symbol.replace(".P", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"
    okx_side = "sell" if side_type == "SHORT" else "buy"
    pos_side = "short" if side_type == "SHORT" else "long"

    max_lev = 20
    last_price = 0.0
    ct_val = 1.0   # Количество монет в 1 контракте
    lot_sz = 1.0   # Минимальный шаг/количество контрактов (обычно 1)

    try:
        # 1. Получаем спецификацию инструмента из OKX
        inst_res = okx_request("GET", f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}")
        if inst_res.get("code") == "0" and inst_res.get("data"):
            inst_data = inst_res["data"][0]
            max_lev = int(inst_data.get("lever", 20))
            ct_val = float(inst_data.get("ctVal", 1.0))
            lot_sz = float(inst_data.get("lotSz", 1.0))

        # 2. Получаем текущую рыночную цену
        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={inst_id}")
        if ticker_res.get("code") == "0" and ticker_res.get("data"):
            last_price = float(ticker_res["data"][0].get("last", 0.0))

    except Exception as e:
        print(f"Ошибка получения данных по инструменту {inst_id}: {e}")

    if last_price <= 0:
        return False, f"Не удалось получить текущую цену для `{inst_id}` с OKX."

    # 3. Расчет стоимости 1 минимального лота (в USDT)
    min_contract_notional = ct_val * lot_sz * last_price  # Полная стоимость 1 лота без плеча
    min_margin_required = min_contract_notional / max_lev  # Минимальная маржа с учетом плеча

    # 4. Проверка: достаточно ли введенной маржи
    if margin_usdt < min_margin_required:
        # Округляем до 2 знаков с запасом вверх
        suggested_margin = round(min_margin_required + 0.01, 2)
        return False, (
            f"❌ **Суммы ${margin_usdt} недостаточно для входа в {inst_id}!**\n\n"
            f"• Цена монеты: **${last_price:,.2f}**\n"
            f"• Максимальное плечо: **{max_lev}x**\n"
            f"• Стоимость 1 мин. контракта: **${min_contract_notional:.2f}**\n\n"
            f"👉 **Минимальная маржа для этой монеты: `${suggested_margin} USDT`**"
        )

    # 5. Устанавливаем плечо на OKX
    set_okx_leverage(inst_id, max_lev)

    # 6. Расчет целевого объема в контрактах
    target_notional_usdt = margin_usdt * max_lev
    contracts_qty = math.floor(target_notional_usdt / (ct_val * last_price))

    # Корректируем согласно шагу lot_sz
    contracts_qty = int((contracts_qty // lot_sz) * lot_sz)

    if contracts_qty < lot_sz:
        suggested_margin = round(min_margin_required + 0.01, 2)
        return False, (
            f"❌ Не удалось рассчитать минимальный объем.\n"
            f"👉 Попробуйте ввести маржу от **${suggested_margin} USDT**."
        )

    formatted_sz = str(contracts_qty)
    actual_notional_usdt = contracts_qty * ct_val * last_price
    actual_margin_used = actual_notional_usdt / max_lev

    # 7. Отправляем ордер
    order_body = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": okx_side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": formatted_sz
    }

    res = okx_request("POST", "/api/v5/trade/order", order_body)

    if res.get("code") == "0":
        return True, (
            f"✅ **Ордер успешно открыт!**\n\n"
            f"• Инструмент: `{inst_id}`\n"
            f"• Плечо: **{max_lev}x**\n"
            f"• Контрактов: **{formatted_sz} ct**\n"
            f"• Задействовано маржи: **~${round(actual_margin_used, 2)} USDT**\n"
            f"• Общий объем позиции: **~${round(actual_notional_usdt, 2)} USDT**\n"
            f"• ID ордера: `{res['data'][0]['ordId']}`"
        )
    else:
        msg = res.get("data", [{}])[0].get("sMsg") or res.get("msg")
        return False, f"Ошибка OKX: {msg}"

# ================= РАСЧЕТ RSI И ЗАКРЫТИЕ ПОЗИЦИЙ С PNL =================

def calculate_rsi(prices, period=14):
    """Рассчитывает классический индикатор RSI(14) по массиву цен закрытия"""
    if len(prices) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(abs(change))
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)

def get_15m_rsi(inst_id):
    """Получает свечи 15m с OKX и рассчитывает RSI(14)"""
    res = okx_request("GET", f"/api/v5/market/candles?instId={inst_id}&bar=15m&limit=50")
    if res.get("code") == "0" and res.get("data"):
        candles = res["data"][::-1]
        close_prices = [float(c[4]) for c in candles]
        return calculate_rsi(close_prices, 14)
    return None

def close_okx_position(inst_id, pos_side):
    """Полностью закрывает позицию по рынку (Market Close)"""
    body = {
        "instId": inst_id,
        "mgnMode": "cross",
        "posSide": pos_side
    }
    return okx_request("POST", "/api/v5/trade/close-position", body)

def check_and_close_positions_by_rsi():
    """Фоновая задача: проверяет открытые позиции на закрытии 15m свечи и выводит PnL"""
    try:
        # Получаем открытые позиции по SWAP
        res = okx_request("GET", "/api/v5/account/positions?instType=SWAP")
        if res.get("code") != "0" or not res.get("data"):
            return

        positions = res["data"]
        for pos in positions:
            pos_qty = float(pos.get("pos", 0))
            if pos_qty == 0:
                continue

            inst_id = pos.get("instId")
            pos_side = pos.get("posSide")  # "long" или "short"

            # Считываем PnL до закрытия позиции
            pnl_usdt = float(pos.get("upl", 0.0))  # Unreleased PnL в USDT
            pnl_ratio = float(pos.get("uplRatio", 0.0)) * 100  # PnL в процентах

            # Рассчитываем RSI 15m
            rsi = get_15m_rsi(inst_id)
            if rsi is None:
                continue

            # Условия закрытия
            should_close = False
            reason = ""

            if pos_side == "long" and rsi >= 70:
                should_close = True
                reason = f"RSI(15m) = **{rsi}** (≥ 70 — Перекупленность)"
            elif pos_side == "short" and rsi <= 30:
                should_close = True
                reason = f"RSI(15m) = **{rsi}** (≤ 30 — Перепроданность)"

            if should_close:
                close_res = close_okx_position(inst_id, pos_side)
                if close_res.get("code") == "0":
                    pnl_emoji = "🟩" if pnl_usdt >= 0 else "🟥"
                    pnl_str = f"+${pnl_usdt:.2f}" if pnl_usdt >= 0 else f"-${abs(pnl_usdt):.2f}"
                    pnl_ratio_str = f"+{pnl_ratio:.2f}%" if pnl_ratio >= 0 else f"{pnl_ratio:.2f}%"

                    send_telegram_msg(
                        f"🚨 **АВТО-ЗАКРЫТИЕ ПОЗИЦИИ BY RSI**\n\n"
                        f"Монета: `{inst_id}`\n"
                        f"Позиция: **{pos_side.upper()}**\n"
                        f"Причина: {reason}\n\n"
                        f"📊 **РЕЗУЛЬТАТ СДЕЛКИ:**\n"
                        f"PnL (USDT): {pnl_emoji} **{pnl_str}**\n"
                        f"PnL (%): {pnl_emoji} **{pnl_ratio_str}**\n\n"
                        f"Статус: ✅ **Позиция закрыта**"
                    )
                else:
                    err_msg = close_res.get("msg") or "Ошибка закрытия"
                    send_telegram_msg(
                        f"⚠️ **Ошибка закрытия позиции `{inst_id}` ({pos_side}):** `{err_msg}`"
                    )
    except Exception as e:
        print(f"Ошибка при проверке RSI позиций: {e}")

# ================= ПЛАНИРОВЩИК ЗАДАЧ (15m) =================

scheduler = BackgroundScheduler()
# Запуск каждые 15 минут (в 00, 15, 30, 45 минут каждого часа)
scheduler.add_job(check_and_close_positions_by_rsi, 'cron', minute='0,15,30,45')
scheduler.start()

# ================= TELEGRAM & WEBHOOKS =================

def send_telegram_signal(symbol, signal_type, raw_text):
    """Отправляет сигнал в Telegram с кнопками"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    emoji = "🔴" if signal_type == "SHORT" else "🟢"

    text = (
        f"{emoji} **СИГНАЛ ОТ ИНДИКАТОРА**\n\n"
        f"Монета: `{symbol}`\n"
        f"Тип: **{signal_type}**\n\n"
        f"📝 *Исходный текст:* `{raw_text}`"
    )

    reply_markup = {
        "inline_keyboard": [
            [{"text": f"🚀 Войти в {signal_type}", "callback_data": f"INIT_{signal_type}_{symbol}"}],
            [{"text": "❌ Пропустить", "callback_data": "CANCEL"}]
        ]
    }

    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown", "reply_markup": reply_markup})

def parse_alert(text):
    """Парсит алерт от TradingView"""
    match = re.search(r'([A-Z0-9]+USDT(?:\.P)?)', text)
    symbol = match.group(1) if match else "UNKNOWN"
    signal_type = "UNKNOWN"
    if "upthrust" in text.lower() or "short" in text.lower():
        signal_type = "SHORT"
    elif "spring" in text.lower() or "long" in text.lower():
        signal_type = "LONG"
    return symbol, signal_type

@app.route('/webhook', methods=['POST'])
def webhook():
    raw_data = request.get_data(as_text=True)
    symbol, signal_type = parse_alert(raw_data)
    if signal_type in ["SHORT", "LONG"]:
        send_telegram_signal(symbol, signal_type, raw_data)
    return "OK", 200

@app.route('/telegram-callback', methods=['POST'])
def telegram_callback():
    data = request.get_json()

    if "message" in data and "text" in data["message"]:
        chat_id = str(data["message"]["chat"]["id"])
        user_text = data["message"]["text"].strip()

        if chat_id in PENDING_TRADES:
            try:
                margin_usdt = float(user_text.replace(",", "."))
                trade_info = PENDING_TRADES.pop(chat_id)
                symbol = trade_info["symbol"]
                side_type = trade_info["side_type"]

                send_telegram_msg(f"⏳ Выставляем макс. плечо и открываем {side_type} по {symbol} на **${margin_usdt}**...")

                success, result_msg = execute_okx_trade(symbol, side_type, margin_usdt)

                if success:
                    send_telegram_msg(f"✅ **Ордер успешно исполнен!**\n\n{result_msg}")
                else:
                    send_telegram_msg(f"❌ **Ошибка при открытии ордера:**\n`{result_msg}`")
            except ValueError:
                send_telegram_msg("⚠️ Неверный формат! Введи просто число (например: `50` или `100`).")

    elif "callback_query" in data:
        query = data["callback_query"]
        callback_id = query["id"]
        action = query["data"]
        message_id = query["message"]["message_id"]
        chat_id = str(query["message"]["chat"]["id"])

        if action == "CANCEL":
            PENDING_TRADES.pop(chat_id, None)
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText", json={
                "chat_id": chat_id, "message_id": message_id, "text": "❌ **Сигнал отменён пользователем.**", "parse_mode": "Markdown"
            })
        elif action.startswith("INIT_"):
            parts = action.split("_")
            side_type = parts[1]
            symbol = parts[2]

            PENDING_TRADES[chat_id] = {"symbol": symbol, "side_type": side_type}

            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": callback_id})
            send_telegram_msg(
                f"💵 **Введи сумму маржи в USDT для входа в {side_type} ({symbol}):**\n\n"
                f"*(Бот автоматически применит максимально возможное плечо)*"
            )

    return "OK", 200

@app.route('/', methods=['GET'])
def index():
    return "OKX Bot is Running!", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
