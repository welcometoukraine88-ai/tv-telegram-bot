import os
import re
import json
import time
import math
import base64
import hmac
import hashlib
from flask import Flask, request
import requests

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
    try:
        if method == "GET":
            res = requests.get(url, headers=headers, timeout=10)
        else:
            res = requests.post(url, data=body_str, headers=headers, timeout=10)
        return res.json()
    except Exception as e:
        print(f"Ошибка запроса к OKX ({request_path}): {e}")
        return {"code": "-1", "msg": str(e)}

def send_telegram_msg(text):
    """Вспомогательная функция отправки сообщения в Telegram"""
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

def set_okx_leverage(inst_id, leverage):
    """Устанавливает плечо на OKX"""
    body = {
        "instId": inst_id,
        "lever": str(leverage),
        "mgnMode": "cross"
    }
    return okx_request("POST", "/api/v5/account/set-leverage", body)

def get_okx_position_side(inst_id):
    """
    Проверяет текущую открытую позицию по инструменту на OKX.
    Возвращает 'long', 'short' или None (если позиции нет).
    """
    res = okx_request("GET", f"/api/v5/account/positions?instType=SWAP&instId={inst_id}")
    if res.get("code") == "0" and res.get("data"):
        for pos in res["data"]:
            pos_qty = float(pos.get("pos", 0))
            if pos_qty != 0:
                return pos.get("posSide")  # 'long' или 'short'
    return None

def close_okx_position(inst_id, pos_side):
    """Полностью закрывает позицию по рынку (Market Close)"""
    body = {
        "instId": inst_id,
        "mgnMode": "cross",
        "posSide": pos_side
    }
    return okx_request("POST", "/api/v5/trade/close-position", body)

# ================= РАСЧЕТ И ИСПОЛНЕНИЕ ОРДЕРА В КОНТРАКТАХ =================

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
    lot_sz = 1.0   # Минимальный шаг лота

    try:
        # 1. Запрос параметров инструмента
        inst_res = okx_request("GET", f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}")
        if inst_res.get("code") == "0" and inst_res.get("data"):
            inst_data = inst_res["data"][0]
            max_lev = int(inst_data.get("lever", 20))
            ct_val = float(inst_data.get("ctVal", 1.0))
            lot_sz = float(inst_data.get("lotSz", 1.0))

        # 2. Получение текущей цены
        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={inst_id}")
        if ticker_res.get("code") == "0" and ticker_res.get("data"):
            last_price = float(ticker_res["data"][0].get("last", 0.0))

    except Exception as e:
        print(f"Ошибка получения данных по инструменту {inst_id}: {e}")

    if last_price <= 0:
        return False, f"Не удалось получить текущую цену для `{inst_id}` с OKX."

    # 3. Расчет стоимости 1 минимального контракта в USDT
    min_contract_notional = ct_val * lot_sz * last_price
    min_margin_required = min_contract_notional / max_lev

    # 4. Проверка маржи
    if margin_usdt < min_margin_required:
        suggested_margin = round(min_margin_required + 0.01, 2)
        return False, (
            f"❌ **Суммы ${margin_usdt} недостаточно для входа в {inst_id}!**\n\n"
            f"• Текущая цена: **${last_price:,.2f}**\n"
            f"• Макс. плечо: **{max_lev}x**\n"
            f"• Стоимость 1 контракта: **${min_contract_notional:.2f}**\n\n"
            f"👉 **Минимальная маржа для этой монеты: `${suggested_margin} USDT`**"
        )

    # 5. Установка плеча
    set_okx_leverage(inst_id, max_lev)

    # 6. Расчет целевого объема
    target_notional_usdt = margin_usdt * max_lev
    raw_contracts = target_notional_usdt / (ct_val * last_price)
    contracts_qty = math.floor(raw_contracts / lot_sz) * lot_sz

    if contracts_qty < lot_sz:
        suggested_margin = round(min_margin_required + 0.01, 2)
        return False, f"👉 Попробуйте ввести маржу от **${suggested_margin} USDT**."

    formatted_sz = str(int(contracts_qty)) if lot_sz.is_integer() else f"{contracts_qty:.4f}".rstrip('0').rstrip('.')

    actual_notional_usdt = contracts_qty * ct_val * last_price
    actual_margin_used = actual_notional_usdt / max_lev

    # 7. Отправка ордера
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

# ================= TELEGRAM & WEBHOOKS =================

def send_telegram_signal(symbol, signal_type, raw_text, is_averaging=False):
    """Отправляет сигнал на вход или усреднение в Telegram с кнопками"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    emoji = "🔴" if signal_type == "SHORT" else "🟢"
    action_title = "УСРЕДНЕНИЕ (ДОБОР)" if is_averaging else "СИГНАЛ ОТ СТРАТЕГИИ"

    text = (
        f"{emoji} **{action_title}**\n\n"
        f"Монета: `{symbol}`\n"
        f"Направление: **{signal_type}**\n\n"
        f"📝 *Исходный текст:* `{raw_text}`"
    )

    reply_markup = {
        "inline_keyboard": [
            [{"text": f"🚀 Войти / Усреднить ({signal_type})", "callback_data": f"INIT_{signal_type}_{symbol}"}],
            [{"text": "❌ Пропустить", "callback_data": "CANCEL"}]
        ]
    }

    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown", "reply_markup": reply_markup}, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки сигнала: {e}")

def parse_alert(text):
    """Парсит алерт от стратегии TradingView (ищет тикер и buy/sell)"""
    match = re.search(r'([A-Z0-9]+USDT(?:\.P)?)', text)
    symbol = match.group(1) if match else "UNKNOWN"
    
    signal_type = "UNKNOWN"
    text_lower = text.lower()
    
    if "sell" in text_lower:
        signal_type = "SHORT"
    elif "buy" in text_lower:
        signal_type = "LONG"
        
    return symbol, signal_type

@app.route('/webhook', methods=['POST'])
def webhook():
    raw_data = request.get_data(as_text=True)
    symbol, signal_type = parse_alert(raw_data)
    
    if symbol == "UNKNOWN" or signal_type == "UNKNOWN":
        return "Ignored", 200

    clean_symbol = symbol.replace(".P", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"

    # Проверяем текущее состояние позиции на OKX
    current_pos = get_okx_position_side(inst_id)

    # ЛОГИКА ОБРАБОТКИ СИГНАЛА BUY
    if signal_type == "LONG":
        if current_pos == "short":
            # Закрываем существующий ШОРТ без открытия ЛОНГА
            close_res = close_okx_position(inst_id, "short")
            if close_res.get("code") == "0":
                send_telegram_msg(f"🔄 **Закрыт SHORT по `{inst_id}`** по сигналу BUY от стратегии.")
            else:
                msg = close_res.get("msg") or "Ошибка"
                send_telegram_msg(f"⚠️ **Ошибка закрытия SHORT по `{inst_id}`:** `{msg}`")
        else:
            # Если позиции нет или уже открыт LONG — предлагаем войти / усредниться
            is_avg = (current_pos == "long")
            send_telegram_signal(symbol, "LONG", raw_data, is_averaging=is_avg)

    # ЛОГИКА ОБРАБОТКИ СИГНАЛА SELL
    elif signal_type == "SHORT":
        if current_pos == "long":
            # Закрываем существующий ЛОНГ без открытия ШОРТА
            close_res = close_okx_position(inst_id, "long")
            if close_res.get("code") == "0":
                send_telegram_msg(f"🔄 **Закрыт LONG по `{inst_id}`** по сигналу SELL от стратегии.")
            else:
                msg = close_res.get("msg") or "Ошибка"
                send_telegram_msg(f"⚠️ **Ошибка закрытия LONG по `{inst_id}`:** `{msg}`")
        else:
            # Если позиции нет или уже открыт SHORT — предлагаем войти / усредниться
            is_avg = (current_pos == "short")
            send_telegram_signal(symbol, "SHORT", raw_data, is_averaging=is_avg)

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
                    send_telegram_msg(f"{result_msg}")
                else:
                    send_telegram_msg(f"❌ **Ошибка при открытии ордера:**\n\n{result_msg}")
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
            try:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText", json={
                    "chat_id": chat_id, "message_id": message_id, "text": "❌ **Сигнал отменён пользователем.**", "parse_mode": "Markdown"
                }, timeout=5)
            except Exception as e:
                print(f"Ошибка отмены сигнала: {e}")

        elif action.startswith("INIT_"):
            parts = action.split("_")
            side_type = parts[1]
            symbol = parts[2]

            PENDING_TRADES[chat_id] = {"symbol": symbol, "side_type": side_type}

            try:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": callback_id}, timeout=5)
            except Exception as e:
                print(f"Ошибка CallbackQuery: {e}")

            send_telegram_msg(
                f"💵 **Введи сумму маржи в USDT для входа/усреднения в {side_type} ({symbol}):**\n\n"
                f"*(Бот автоматически применит максимально возможное плечо)*"
            )

    return "OK", 200

@app.route('/', methods=['GET'])
def index():
    return "OKX Bot is Running!", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
