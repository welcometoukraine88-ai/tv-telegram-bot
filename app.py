import os
import re
from flask import Flask, request
import requests

app = Flask(__name__)

# Токены берем из настроек сервера
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")


def send_telegram_signal(symbol, signal_type, raw_text):
    """Формирует и отправляет сообщение с кнопкой в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Оформляем направление
    if signal_type == "SHORT":
        side_emoji = "🔴"
        action_title = "SHORT (Upthrust)"
        side_val = "sell"
    else:
        side_emoji = "🟢"
        action_title = "LONG (Spring)"
        side_val = "buy"

    text = (
        f"{side_emoji} <b>СИГНАЛ WYCKOFF ОТ ИНДИКАТОРА</b>\n\n"
        f"📌 <b>Монета:</b> {symbol}\n"
        f"📊 <b>Направление:</b> {action_title}\n\n"
        f"<i>Текст алерта:</i> {raw_text}"
    )

    cb_data = f"trade:{symbol}:{side_val}"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": f"🚀 Открыть {action_title} на OKX",
                        "callback_data": cb_data,
                    },
                    {"text": "❌ Пропустить", "callback_data": "cancel"},
                ]
            ]
        },
    }
    requests.post(url, json=payload)


@app.route("/webhook", methods=["POST"])
def webhook():
    """Принимает сырой текст от TradingView"""
    raw_data = request.get_data(as_text=True)

    # 1. Извлекаем тикер (например, ATHUSDT, WDCUSDT, SOLUSDT)
    ticker_match = re.search(r"([A-Z0-9]+)(?:USDT|\.P)", raw_data)
    if ticker_match:
        # Приводим к чистому тикеру (например, ATHUSDT)
        base_symbol = ticker_match.group(1)
        symbol = (
            f"{base_symbol}USDT"
            if not base_symbol.endswith("USDT")
            else base_symbol
        )
    else:
        symbol = "BTCUSDT"

    # 2. Парсим ключевые слова Upthrust (Short) и Spring (Long)
    raw_lower = raw_data.lower()
    if "upthrust" in raw_lower:
        signal_type = "SHORT"
    elif "spring" in raw_lower:
        signal_type = "LONG"
    else:
        # Если ни одно ключевое слово не найдено
        return "No Wyckoff pattern found", 200

    # 3. Отправляем кнопку в Telegram
    send_telegram_signal(symbol, signal_type, raw_data)
    return "OK", 200


@app.route("/telegram-callback", methods=["POST"])
def telegram_callback():
    """Обрабатывает нажатие на кнопки в Telegram"""
    update = request.json
    if "callback_query" in update:
        query = update["callback_query"]
        cb_data = query["data"]
        chat_id = query["message"]["chat"]["id"]
        message_id = query["message"]["message_id"]

        if cb_data == "cancel":
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": "❌ Сигнал проигнорирован.",
                },
            )
        elif cb_data.startswith("trade:"):
            _, symbol, side = cb_data.split(":")

            # Здесь будет вызываться ордер на OKX
            # (вызываем функцию отправки)
            status_text = f"✅ <b>Сигнал принят!</b>\nОтправляем ордер {side.upper()} по монете {symbol} на OKX."

            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": status_text,
                    "parse_mode": "HTML",
                },
            )

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
