import os
import re
import json
import time
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
    if method == "GET":
        res = requests.get(url, headers=headers, timeout=10)
    else:
        res = requests.post(url, data=body_str, headers=headers, timeout=10)
    return res.json()

def get_max_leverage_and_ticker(inst_id):
    """Получает максимальное доступное плечо и текущую цену инструмента"""
    try:
        # 1. Запрос максимального плеча
        lev_res = okx_request("GET", f"/api/v5/public/leverage-lanes?instId={inst_id}&mgnMode=cross")
        max_lev = "20" # Дефолтное плечо
        if lev_res.get("code") == "0" and lev_res.get("data"):
            max_lev = str(lev_res["data"][0].get("maxLever", "20"))

        # 2. Запрос текущей цены и размера контракта (ctVal)
        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={inst_id}")
        price = float(ticker_res["data"][0]["last"]) if ticker_res.get("code") == "0" else 0.0

        instr_res = okx_request("GET", f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}")
        ct_val = float(instr_res["data"][0]["ctVal"]) if instr_res.get("code") == "0" else 1.0

        return int(max_lev), price, ct_val
    except Exception as e:
        print(f"Ошибка получения данных тикера: {e}")
        return 20, 0.0, 1.0

def set_okx_leverage(inst_id, leverage):
    """Устанавливает максимальное плечо на OKX"""
    body = {
        "instId": inst_id,
        "lever": str(leverage),
        "mgnMode": "cross"
    }
    return okx_request("POST", "/api/v5/account/set-leverage", body)

def execute_okx_trade(symbol, side_type, margin_usdt):
    """
    Рассчитывает размер позиции с максимальным плечом и открывает ордер
    """
    clean_symbol = symbol.replace(".P", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"
    okx_side = "sell" if side_type == "SHORT" else "buy"

    # 1. Получаем макс. плечо и рыночную цену
    max_lev, last_price, ct_val = get_max_leverage_and_ticker(inst_id)
    if last_price <= 0:
        return False, "Не удалось получить текущую цену монеты с OKX."

    # 2. Выставляем максимальное плечо
    set_okx_leverage(inst_id, max_lev)

    # 3. Расчет позиционного объема (Номинал позиции = Маржа * Плечо)
    notional_usdt = margin_usdt * max_lev
    
    # Расчет количества контрактов sz = Номинал / (Цена * Размер_1_контракта)
    sz_contracts = int(notional_usdt / (last_price * ct_val))
    if sz_contracts < 1:
        sz_contracts = 1  # Минимальный размер — 1 контракт

    # 4. Отправка рыночного ордера
    order_body = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": okx_side,
        "ordType": "market",
        "sz": str(sz_contracts)
    }

    res = okx_request("POST", "/api/v5/trade/order", order_body)

    if res.get("code") == "0":
        total_pos_val = round(sz_contracts * last_price * ct_val, 2)
        return True, (
            f"Плечо: **{max_lev}x** (Максимальное)\n"
            f"Введенная маржа: **${margin_usdt}**\n"
            f"Общий объем позиции: **~${total_pos_val}** ({sz_contracts} контр.)\n"
            f"ID ордера: `{res['data'][0]['ordId']}`"
        )
    else:
        msg = res.get("data", [{}])[0].get("sMsg") or res.get("msg")
        return False, f"Ошибка OKX: {msg}"

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

def send_telegram_msg(text):
    """Вспомогательная функция отправки сообщения"""
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    })

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

    # 1. Обработка текстовых сообщений (когда пользователь вводит сумму USDT)
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

    # 2. Обработка нажатий на inline-кнопки
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

            # Запоминаем, что ждем ввод суммы от этого чата
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
