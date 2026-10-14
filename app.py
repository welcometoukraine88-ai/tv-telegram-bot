import os
import re
import json
import time
import math
import base64
import hmac
import hashlib
from datetime import datetime
from flask import Flask, request
import requests

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

OKX_BASE_URL = "https://www.okx.com"

RSI_TIMEFRAME = "15m"
RSI_EXIT_LONG = 70.0
RSI_EXIT_SHORT = 30.0

BOT_SETTINGS = {
    "default_margin": float(os.environ.get("DEFAULT_MARGIN", "1.0"))
}

def get_okx_signature(timestamp, method, request_path, body=""):
    message = timestamp + method + request_path + body
    mac = hmac.new(OKX_SECRET_KEY.encode('utf-8'), message.encode('utf-8'), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode('utf-8')

def okx_request(method, request_path, body_data=None):
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
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

def set_okx_leverage(inst_id, leverage):
    body = {
        "instId": inst_id,
        "lever": str(leverage),
        "mgnMode": "cross"
    }
    return okx_request("POST", "/api/v5/account/set-leverage", body)

def get_okx_all_positions():
    res = okx_request("GET", "/api/v5/account/positions?instType=SWAP")
    positions = []
    if res.get("code") == "0" and res.get("data"):
        for pos in res["data"]:
            pos_qty = float(pos.get("pos", 0))
            if pos_qty != 0:
                positions.append({
                    "instId": pos.get("instId"),
                    "posSide": pos.get("posSide"),
                    "upl": float(pos.get("upl", 0.0)),
                    "uplRatio": float(pos.get("uplRatio", 0.0)) * 100,
                    "avgPx": float(pos.get("avgPx", 0.0)),
                    "sz": pos.get("pos")
                })
    return positions

def close_okx_position(inst_id, pos_side):
    body = {
        "instId": inst_id,
        "mgnMode": "cross",
        "posSide": pos_side
    }
    return okx_request("POST", "/api/v5/trade/close-position", body)

def execute_okx_trade(symbol, side_type, margin_usdt):
    clean_symbol = symbol.replace(".P", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"
    okx_side = "sell" if side_type == "SHORT" else "buy"
    pos_side = "short" if side_type == "SHORT" else "long"

    max_lev = 20
    last_price = 0.0
    ct_val = 1.0
    lot_sz = 1.0

    try:
        inst_res = okx_request("GET", f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}")
        if inst_res.get("code") == "0" and inst_res.get("data"):
            inst_data = inst_res["data"][0]
            max_lev = int(inst_data.get("lever", 20))
            ct_val = float(inst_data.get("ctVal", 1.0))
            lot_sz = float(inst_data.get("lotSz", 1.0))

        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={inst_id}")
        if ticker_res.get("code") == "0" and ticker_res.get("data"):
            last_price = float(ticker_res["data"][0].get("last", 0.0))
    except Exception as e:
        print(f"Ошибка данных {inst_id}: {e}")

    if last_price <= 0:
        return False, f"Не удалось получить цену для `{inst_id}`."

    set_okx_leverage(inst_id, max_lev)

    min_contract_notional = ct_val * lot_sz * last_price
    min_margin_required = min_contract_notional / max_lev

    if margin_usdt < min_margin_required:
        suggested_margin = round(min_margin_required + 0.01, 2)
        return False, f"❌ Маржа ${margin_usdt} мала для `{inst_id}`. Мин. маржа: `${suggested_margin} USDT`"

    target_notional_usdt = margin_usdt * max_lev
    raw_contracts = target_notional_usdt / (ct_val * last_price)
    contracts_qty = math.floor(raw_contracts / lot_sz) * lot_sz

    if contracts_qty < lot_sz:
        return False, f"👉 Объем слишком мал."

    formatted_sz = str(int(contracts_qty)) if lot_sz.is_integer() else f"{contracts_qty:.4f}".rstrip('0').rstrip('.')

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
        return True, "Success"
    else:
        msg = res.get("data", [{}])[0].get("sMsg") or res.get("msg")
        return False, f"Ошибка OKX: {msg}"

def parse_alert(text):
    symbol = "UNKNOWN"
    signal_type = "UNKNOWN"
    pattern_name = "SIGNAL"

    text_str = text if isinstance(text, str) else json.dumps(text)

    # 1. Пытаемся вытащить через JSON
    try:
        data = json.loads(text_str)
        if isinstance(data, dict):
            raw_sym = data.get("instrument") or data.get("symbol") or data.get("ticker") or ""
            if raw_sym:
                symbol = raw_sym.split(":")[-1].strip() if ":" in raw_sym else raw_sym.strip()
    except Exception:
        pass

    # 2. Если в JSON пусто, ищем тикер в тексте (например, MSFTUSDT.P, SOONUSDT.P)
    if symbol == "UNKNOWN":
        match = re.search(r'([A-Z0-9]+(?:USDT|\.P))', text_str)
        if match:
            symbol = match.group(1)
            if ":" in symbol:
                symbol = symbol.split(":")[-1]

    text_lower = text_str.lower()
    
    # 3. Точные триггеры входа и стоп-лосса
    if "buy level retest triggered" in text_lower:
        signal_type = "ENTRY_LONG"
        pattern_name = "BUY_RETEST"
    elif "sell level retest triggered" in text_lower:
        signal_type = "ENTRY_SHORT"
        pattern_name = "SELL_RETEST"
    elif "range breakout down" in text_lower:
        signal_type = "BREAKOUT_DOWN"
        pattern_name = "BREAKOUT_DOWN"
    elif "range breakout up" in text_lower:
        signal_type = "BREAKOUT_UP"
        pattern_name = "BREAKOUT_UP"

    return symbol, signal_type, pattern_name

@app.route('/webhook', methods=['POST'])
def webhook():
    raw_data = request.get_data(as_text=True)
    symbol, signal_type, pattern_name = parse_alert(raw_data)

    if symbol == "UNKNOWN" or signal_type == "UNKNOWN":
        return "Ignored", 200

    clean_symbol = symbol.replace(".P", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"
    current_margin = float(BOT_SETTINGS["default_margin"])

    positions = get_okx_all_positions()
    pos_info = next((p for p in positions if p["instId"] == inst_id), None)
    current_pos = pos_info["posSide"] if pos_info else None
    upl = pos_info.get("upl", 0.0) if pos_info else 0.0
    upl_ratio = pos_info.get("uplRatio", 0.0) if pos_info else 0.0
    pnl_sign = "+" if upl >= 0 else ""
    pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"

    # Стоп-лосс по Range Breakout
    if signal_type == "BREAKOUT_DOWN" and current_pos == "long":
        close_res = close_okx_position(inst_id, "long")
        if close_res.get("code") == "0":
            send_telegram_msg(f"🛑 **СТОП-ЛОСС (Breakout DOWN) LONG по `{inst_id}`**\n💰 **PnL:** `{pnl_str}`")
        return "OK", 200

    if signal_type == "BREAKOUT_UP" and current_pos == "short":
        close_res = close_okx_position(inst_id, "short")
        if close_res.get("code") == "0":
            send_telegram_msg(f"🛑 **СТОП-ЛОСС (Breakout UP) SHORT по `{inst_id}`**\n💰 **PnL:** `{pnl_str}`")
        return "OK", 200

    # Вход по Buy level retest triggered (Лонг)
    if signal_type == "ENTRY_LONG":
        if current_pos == "short":
            close_res = close_okx_position(inst_id, "short")
            if close_res.get("code") == "0":
                send_telegram_msg(f"🔄 **Закрыт SHORT по `{inst_id}` (Разворот в Лонг)**\n💰 **PnL:** `{pnl_str}`")

        success, err_msg = execute_okx_trade(symbol, "LONG", current_margin)
        if success:
            send_telegram_msg(f"🚀 **LONG открыт (OKX)**\n• Ticker: `{inst_id}`\n• Маржа: `${current_margin} USDT`")
        else:
            send_telegram_msg(f"⚠️ **Ошибка открытия LONG `{inst_id}`**: {err_msg}")

    # Вход по Sell level retest triggered (Шорт)
    elif signal_type == "ENTRY_SHORT":
        if current_pos == "long":
            close_res = close_okx_position(inst_id, "long")
            if close_res.get("code") == "0":
                send_telegram_msg(f"🔄 **Закрыт LONG по `{inst_id}` (Разворот в Шорт)**\n💰 **PnL:** `{pnl_str}`")

        success, err_msg = execute_okx_trade(symbol, "SHORT", current_margin)
        if success:
            send_telegram_msg(f"🔻 **SHORT открыт (OKX)**\n• Ticker: `{inst_id}`\n• Маржа: `${current_margin} USDT`")
        else:
            send_telegram_msg(f"⚠️ **Ошибка открытия SHORT `{inst_id}`**: {err_msg}")

    return "OK", 200

@app.route('/telegram-callback', methods=['POST'])
def telegram_callback():
    data = request.get_json(silent=True)
    if not data:
        return "OK", 200

    if "message" in data and "text" in data["message"]:
        user_text = data["message"]["text"].strip()

        if user_text.startswith("/margin") or user_text.startswith("/setmargin"):
            parts = user_text.split()
            if len(parts) > 1:
                try:
                    new_margin = float(parts[1].replace(",", "."))
                    if new_margin > 0:
                        BOT_SETTINGS["default_margin"] = new_margin
                        send_telegram_msg(f"✅ Маржа на сделку успешно изменена на: **${new_margin} USDT**")
                    else:
                        send_telegram_msg("⚠️ Сумма должна быть больше нуля.")
                except ValueError:
                    send_telegram_msg("⚠️ Неверный формат. Пример: `/margin 1`")
            else:
                send_telegram_msg(f"ℹ️ Текущая маржа на сделку: **${BOT_SETTINGS['default_margin']} USDT**\nДля изменения введите: `/margin <сумма>`")
        
        elif user_text.startswith("/settings") or user_text.startswith("/status"):
            send_telegram_msg(f"⚙️ **Текущие настройки бота:**\n• Маржа на сделку: **${BOT_SETTINGS['default_margin']} USDT**\n• Плечо: **Максимальное (авто)**")

    return "OK", 200

@app.route('/', methods=['GET'])
def index():
    return "OKX Smart Bot is Running!", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
