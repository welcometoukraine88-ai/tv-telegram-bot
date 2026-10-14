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
RSI_EXIT_LONG = 65.0
RSI_EXIT_SHORT = 35.0

# Словарь для ожидания ввода маржи: {chat_id: {"symbol": ..., "side": ..., "inst_id": ..., "message_id": ...}}
PENDING_MARGIN_INPUT = {}

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

def send_telegram_msg(text, reply_markup=None):
    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
            
        res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=5)
        data = res.json()
        if data.get("ok"):
            return data["result"]["message_id"]
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")
    return None

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

def calculate_rsi14(inst_id, timeframe=RSI_TIMEFRAME):
    res = okx_request("GET", f"/api/v5/market/candles?instType=SWAP&instId={inst_id}&bar={timeframe}&limit=100")
    if res.get("code") != "0" or not res.get("data"):
        return None

    raw_candles = res["data"]
    if len(raw_candles) < 30:
        return None

    candles = list(reversed(raw_candles))
    closes = [float(c[4]) for c in candles]

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [abs(min(c, 0.0)) for c in changes]

    length = 14
    if len(gains) < length:
        return None

    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length

    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length

    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)

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
        return False, f"❌ Маржа ${margin_usdt} мала. Мин. маржа: `${suggested_margin} USDT`"

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

    if ":" in text_str:
        parts = text_str.split(":", 1)
        potential_symbol = parts[0].strip().replace('"', '').replace("'", "")
        if len(potential_symbol) > 2 and ("USDT" in potential_symbol.upper() or ".P" in potential_symbol.upper() or len(potential_symbol) <= 12):
            symbol = potential_symbol.upper()
            text_str = parts[1]

    if symbol == "UNKNOWN":
        try:
            data = json.loads(text_str)
            if isinstance(data, dict):
                raw_sym = data.get("instrument") or data.get("symbol") or data.get("ticker") or ""
                if raw_sym:
                    symbol = raw_sym.split(":")[-1].strip() if ":" in raw_sym else raw_sym.strip()
        except Exception:
            pass

    if symbol == "UNKNOWN":
        match = re.search(r'([A-Za-z0-9_]+(?:USDT|\.p|\.P))', text_str)
        if match:
            symbol = match.group(1).upper()
            if ":" in symbol:
                symbol = symbol.split(":")[-1]

    text_lower = text_str.lower()
    
    if "buy level retest" in text_lower:
        signal_type = "ENTRY_LONG"
        pattern_name = "BUY_RETEST"
    elif "sell level retest" in text_lower:
        signal_type = "ENTRY_SHORT"
        pattern_name = "SELL_RETEST"
    elif "range breakout down" in text_lower:
        signal_type = "BREAKOUT_DOWN"
        pattern_name = "BREAKOUT_DOWN"
    elif "range breakout up" in text_lower:
        signal_type = "BREAKOUT_UP"
        pattern_name = "BREAKOUT_UP"

    return symbol, signal_type, pattern_name

@app.route('/webhook', methods=['POST', 'GET'])
def webhook():
    global PENDING_MARGIN_INPUT
    
    data = request.get_json(silent=True)
    
    # 1. Если запрос пришел от Telegram (нажатие кнопки или текст)
    if data and ("callback_query" in data or "message" in data):
        # Обработка нажатий на инлайн-кнопки
        if "callback_query" in data:
            query = data["callback_query"]
            query_id = query.get("id")
            callback_data = query.get("data", "")
            message = query.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            message_id = message.get("message_id")
            
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": query_id})
            
            if callback_data.startswith("skip_"):
                if chat_id and message_id:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText", json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": message.get("text", "") + "\n\n_🚫 Сигнал пропущен_",
                        "parse_mode": "Markdown"
                    })
                return "OK", 200
                
            if callback_data.startswith("enter_"):
                parts = callback_data.split("_")
                side = parts[1]
                symbol = parts[2]
                clean_symbol = symbol.replace(".P", "").replace("USDT", "")
                inst_id = f"{clean_symbol}-USDT-SWAP"

                PENDING_MARGIN_INPUT[chat_id] = {
                    "symbol": symbol,
                    "side": side,
                    "inst_id": inst_id,
                    "message_id": message_id
                }

                if chat_id and message_id:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText", json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": message.get("text", "") + f"\n\n💬 *Введите размер маржи (USDT) в чат для `{inst_id}`:*",
                        "parse_mode": "Markdown"
                    })
                return "OK", 200

        # Обработка текстового ввода суммы маржи
        if "message" in data and "text" in data["message"]:
            msg = data["message"]
            chat_id = msg["chat"]["id"]
            user_text = msg["text"].strip()

            if chat_id in PENDING_MARGIN_INPUT:
                try:
                    margin_usdt = float(user_text.replace(",", "."))
                    if margin_usdt > 0:
                        sig = PENDING_MARGIN_INPUT.pop(chat_id)
                        symbol = sig["symbol"]
                        side = sig["side"]
                        inst_id = sig["inst_id"]

                        positions = get_okx_all_positions()
                        pos_info = next((p for p in positions if p["instId"] == inst_id), None)
                        current_pos = pos_info["posSide"] if pos_info else None
                        upl = pos_info.get("upl", 0.0) if pos_info else 0.0
                        upl_ratio = pos_info.get("uplRatio", 0.0) if pos_info else 0.0
                        pnl_sign = "+" if upl >= 0 else ""
                        pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"

                        if side == "LONG" and current_pos == "short":
                            close_res = close_okx_position(inst_id, "short")
                            if close_res.get("code") == "0":
                                send_telegram_msg(f"🔄 *Разворот позиции*\n• Закрыт SHORT по `{inst_id}`\n• PnL: `{pnl_str}`")
                        elif side == "SHORT" and current_pos == "long":
                            close_res = close_okx_position(inst_id, "long")
                            if close_res.get("code") == "0":
                                send_telegram_msg(f"🔄 *Разворот позиции*\n• Закрыт LONG по `{inst_id}`\n• PnL: `{pnl_str}`")

                        success, err_msg = execute_okx_trade(symbol, side, margin_usdt)
                        emoji = "🚀" if side == "LONG" else "🔻"
                        
                        if success:
                            send_telegram_msg(
                                f"{emoji} *ОРДЕР УСПЕШНО ОТКРЫТ*\n"
                                f"━━━━━━━━━━━━━━━━━━━\n"
                                f"• *Инструмент:* `{inst_id}`\n"
                                f"• *Тип:* `{side}`\n"
                                f"• *Маржа:* `${margin_usdt} USDT`\n"
                                f"• *Плечо:* `Макс (авто)`"
                            )
                        else:
                            send_telegram_msg(f"⚠️ *Ошибка открытия `{inst_id}`*\n• Причина: `{err_msg}`")
                        return "OK", 200
                except ValueError:
                    send_telegram_msg("⚠️ Пожалуйста, введите корректное число для маржи (например: `10` или `25.5`)")
        
        return "OK", 200

    # 2. Иначе обрабатываем как обычный алерт от TradingView
    raw_data = request.get_data(as_text=True)
    print(f"DEBUG TRADINGVIEW DATA: {raw_data}")
    
    symbol, signal_type, pattern_name = parse_alert(raw_data)

    if not symbol or symbol == "UNKNOWN" or "{{" in symbol or signal_type == "UNKNOWN":
        return "OK", 200

    clean_symbol = symbol.replace(".P", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"

    positions = get_okx_all_positions()
    pos_info = next((p for p in positions if p["instId"] == inst_id), None)
    current_pos = pos_info["posSide"] if pos_info else None
    upl = pos_info.get("upl", 0.0) if pos_info else 0.0
    upl_ratio = pos_info.get("uplRatio", 0.0) if pos_info else 0.0
    pnl_sign = "+" if upl >= 0 else ""
    pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"

    # Фоновая проверка RSI для тейк-профита
    if current_pos:
        current_rsi = calculate_rsi14(inst_id)
        if current_rsi is not None:
            if current_pos == "long" and current_rsi >= RSI_EXIT_LONG:
                close_res = close_okx_position(inst_id, "long")
                if close_res.get("code") == "0":
                    send_telegram_msg(f"🎯 *ТЕЙК-ПРОФИТ (RSI {current_rsi})*\n• Инструмент: `{inst_id}`\n• Направление: `LONG`\n• PnL: `{pnl_str}`")
                return "OK", 200

            if current_pos == "short" and current_rsi <= RSI_EXIT_SHORT:
                close_res = close_okx_position(inst_id, "short")
                if close_res.get("code") == "0":
                    send_telegram_msg(f"🎯 *ТЕЙК-ПРОФИТ (RSI {current_rsi})*\n• Инструмент: `{inst_id}`\n• Направление: `SHORT`\n• PnL: `{pnl_str}`")
                return "OK", 200

    # Стоп-лосс по Range Breakout
    if signal_type == "BREAKOUT_DOWN" and current_pos == "long":
        close_res = close_okx_position(inst_id, "long")
        if close_res.get("code") == "0":
            send_telegram_msg(f"🛑 *СТОП-ЛОСС (Breakout DOWN)*\n• Инструмент: `{inst_id}`\n• PnL: `{pnl_str}`")
        return "OK", 200

    if signal_type == "BREAKOUT_UP" and current_pos == "short":
        close_res = close_okx_position(inst_id, "short")
        if close_res.get("code") == "0":
            send_telegram_msg(f"🛑 *СТОП-ЛОСС (Breakout UP)*\n• Инструмент: `{inst_id}`\n• PnL: `{pnl_str}`")
        return "OK", 200

    if signal_type in ["BREAKOUT_DOWN", "BREAKOUT_UP"]:
        return "OK", 200

    # Отправка сообщения с кнопками Вход / Пропустить
    if signal_type in ["ENTRY_LONG", "ENTRY_SHORT"]:
        is_long = signal_type == "ENTRY_LONG"
        circle_emoji = "🟢" if is_long else "🔴"
        side_code = "LONG" if is_long else "SHORT"
        
        msg_text = (
            f"{circle_emoji} *СИГНАЛ WYCKOFF: {side_code}*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"• *Монета:* `{inst_id}`\n"
            f"• *Паттерн:* `{pattern_name}`\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Вход", "callback_data": f"enter_{side_code}_{symbol}"},
                    {"text": "❌ Пропустить", "callback_data": f"skip_{symbol}"}
                ]
            ]
        }
        send_telegram_msg(msg_text, reply_markup=reply_markup)

    return "OK", 200

@app.route('/', methods=['GET'])
def index():
    return "OKX Smart Bot is Running!", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
