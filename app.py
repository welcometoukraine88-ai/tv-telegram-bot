import json
import os
import re
import ccxt
from flask import Flask, jsonify, request
import requests

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")


def send_telegram_signal(symbol, side, raw_text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    text = (
        f"🚨 <b>СИГНАЛ ОТ ИНДИКАТОРА</b>\n\n"
        f"<b>Монета:</b> {symbol}\n"
        f"<b>Направление:</b> {side.upper()}\n\n"
        f"<i>Текст алерта:</i> {raw_text}"
    )

    cb_data = f"trade:{symbol}:{side}"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": f"🚀 Открыть {side.upper()} на OKX",
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
    raw_data = request.get_data(as_text=True)

    ticker_match = re.search(r"([A-Z0-9]+USDT)", raw_data)
    symbol = ticker_match.group(1) if ticker_match else "SOLUSDT"

    raw_lower = raw_data.lower()
    if "buy" in raw_lower or "long" in raw_lower:
        side = "buy"
    elif "sell" in raw_lower or "short" in raw_lower:
        side = "sell"
    else:
        side = "buy"

    send_telegram_signal(symbol, side, raw_data)
    return "OK", 200


@app.route("/telegram-callback", methods=["POST"])
def telegram_callback():
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

            success, msg = execute_okx_trade(symbol, side)

            status_text = (
                f"✅ <b>Ордер исполнен на OKX!</b>\nПара: {symbol}\nID: {msg}"
                if success
                else f"❌ <b>Ошибка OKX:</b> {msg}"
            )

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


def execute_okx_trade(symbol, side):
    if not OKX_API_KEY:
        return False, "API ключи OKX не настроены!"

    try:
        exchange = ccxt.okx(
            {
                "apiKey": OKX_API_KEY,
                "secret": OKX_SECRET_KEY,
                "password": OKX_PASSPHRASE,
                "options": {"defaultType": "swap"},
            }
        )

        pos_side = "long" if side == "buy" else "short"
        formatted_symbol = f"{symbol.replace('USDT', '')}/USDT:USDT"

        order = exchange.create_order(
            symbol=formatted_symbol,
            type="market",
            side=side,
            amount=1,
            params={"posSide": pos_side},
        )
        return True, order["id"]
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
