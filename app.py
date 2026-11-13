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

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

OKX_BASE_URL = "https://www.okx.com"

# Автоматическая маржа по умолчанию 1.0 USDT (можно переопределить через env DEFAULT_MARGIN)
DEFAULT_MARGIN = float(os.environ.get("DEFAULT_MARGIN", "1.0"))

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
        print(f"❌ Ошибка запроса к OKX ({request_path}): {e}")
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
        print(f"❌ Ошибка отправки в Telegram: {e}")
    return None

def set_okx_leverage(inst_id, leverage):
    body = {
        "instId": inst_id,
        "lever": str(leverage),
        "mgnMode": "cross"
    }
    return okx_request("POST", "/api/v5/account/set-leverage", body)

def get_okx_account_balance():
    """Получает общий баланс аккаунта (Total Equity в USDT)"""
    res = okx_request("GET", "/api/v5/account/balance")
    if res.get("code") == "0" and res.get("data"):
        details = res["data"][0].get("details", [])
        for det in details:
            if det.get("ccy") == "USDT":
                return float(det.get("eq", 0.0))
    return 0.0

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
                    "lever": pos.get("lever", "20"),
                    "sz": pos.get("pos")
                })
    return positions

def close_okx_position(inst_id, pos_side):
    body = {
        "instId": inst_id,
        "mgnMode": "cross",
        "posSide": pos_side
    }
    res = okx_request("POST", "/api/v5/trade/close-position", body)
    print(f"🔄 Ответ закрытия позиции {inst_id} ({pos_side}): {res}")
    return res

def check_drawdown_limit():
    """Проверяет общую просадку: если суммарный убыток > 50% от депозита, возвращает False"""
    try:
        total_equity = get_okx_account_balance()
        if total_equity <= 0:
            return True 

        positions = get_okx_all_positions()
        total_upl = sum(p["upl"] for p in positions)

        # Изменили 0.40 на 0.50
        if total_upl < 0 and abs(total_upl) >= (total_equity * 0.50):
            print(f"🛑 ЗАЩИТА СРАБОТАЛА: Общий убыток ${total_upl:.2f} превышает 50% депозита (${total_equity:.2f})!")
            return False
    except Exception as e:
        print(f"⚠️ Ошибка проверки просадки: {e}")
    
    return True

def execute_okx_trade(symbol, side_type, margin_usdt):
    # Дополнительная проверка просадки перед открытием сделки
    if not check_drawdown_limit():
        return False, "Защита депозита: суммарная просадка превышает 40%!"

    clean_symbol = symbol.replace(".P", "").replace(".p", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"
    okx_side = "sell" if side_type == "SHORT" else "buy"
    pos_side = "short" if side_type == "SHORT" else "long"

    max_lev = 20
    last_price = 0.0
    ct_val = 1.0
    lot_sz = 1.0
    tick_sz = 0.1

    try:
        inst_res = okx_request("GET", f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}")
        if inst_res.get("code") == "0" and inst_res.get("data"):
            inst_data = inst_res["data"][0]
            max_lev = int(inst_data.get("lever", 20))
            ct_val = float(inst_data.get("ctVal", 1.0))
            lot_sz = float(inst_data.get("lotSz", 1.0))
            tick_sz = float(inst_data.get("tickSz", 0.1))

        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={inst_id}")
        if ticker_res.get("code") == "0" and ticker_res.get("data"):
            last_price = float(ticker_res["data"][0].get("last", 0.0))
    except Exception as e:
        print(f"❌ Ошибка данных {inst_id}: {e}")

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
        # Если это шорт, выставляем усреднение (+5%) и стоп-лосс (+10%)
        if side_type == "SHORT":
            try:
                def round_to_tick(val):
                    return round(round(val / tick_sz) * tick_sz, 6)

                avg_price = last_price * 1.05   # Усреднение на 5% выше цены входа
                stop_price = last_price * 1.10  # Стоп-лосс на 10% выше цены входа

                avg_px_str = f"{round_to_tick(avg_price):f}"
                stop_px_str = f"{round_to_tick(stop_price):f}"

                dca_body = {
                    "instId": inst_id,
                    "tdMode": "cross",
                    "side": "sell",
                    "posSide": "short",
                    "ordType": "limit",
                    "px": avg_px_str,
                    "sz": formatted_sz
                }
                okx_request("POST", "/api/v5/trade/order", dca_body)

                stop_body = {
                    "instId": inst_id,
                    "tdMode": "cross",
                    "side": "buy",
                    "posSide": "short",
                    "ordType": "stop_market",
                    "sz": formatted_sz,
                    "tpTriggerPx": stop_px_str,
                    "tpOrdPx": "-1",
                    "slTriggerPx": stop_px_str,
                    "slOrdPx": "-1"
                }
                okx_request("POST", "/api/v5/trade/order-algo", stop_body)
            except Exception as e:
                print(f"⚠️ Ошибка выставления DCA/SL ордеров для {inst_id}: {e}")

        return True, "Success"
    else:
        msg = res.get("data", [{}])[0].get("sMsg") or res.get("msg")
        return False, f"Ошибка OKX: {msg}"

def parse_alert(text):
    symbol = "UNKNOWN"
    signal_type = "UNKNOWN"
    pattern_name = "SIGNAL"

    text_str = text if isinstance(text, str) else json.dumps(text)
    text_str = text_str.strip().replace('"', '').replace("'", "")

    match_start = re.match(r'^([A-Za-z0-9_\-\.]+)\s*(?:\||:)', text_str)
    if match_start:
        candidate = match_start.group(1).upper()
        if len(candidate) > 1:
            symbol = candidate
    else:
        match = re.search(r'\b([A-Za-z0-9_]+(?:USDT|\.p|\.P|XAU))\b', text_str)
        if match:
            symbol = match.group(1).upper()
            if ":" in symbol:
                symbol = symbol.split(":")[-1]

    if symbol != "UNKNOWN":
        symbol = symbol.split()[0].upper()
        if not symbol.endswith(".P") and not symbol.endswith(".p"):
            if "USDT" in symbol and not symbol.endswith("USDT.P"):
                symbol = symbol.replace("USDT", "USDT.P")
            elif "USDT" not in symbol:
                symbol = f"{symbol}USDT.P"
        else:
            symbol = symbol.replace(".p", ".P")

    text_lower = text_str.lower()
    
    if "spring" in text_lower:
        signal_type = "ENTRY_LONG"
        pattern_name = "Spring"
    elif "selling climax" in text_lower:
        signal_type = "ENTRY_LONG"
        pattern_name = "Selling Climax"
    elif "upthrust" in text_lower or "utad" in text_lower:
        signal_type = "ENTRY_SHORT"
        pattern_name = "Upthrust / UTAD"
    elif "exit_long" in text_lower or "rsi returned below 70" in text_lower:
        signal_type = "EXIT_LONG"
        pattern_name = "RSI Exit Long (>70)"
    elif "exit_short" in text_lower or "rsi returned above 30" in text_lower:
        signal_type = "EXIT_SHORT"
        pattern_name = "RSI Exit Short (<30)"

    return symbol, signal_type, pattern_name

@app.route('/webhook', methods=['POST', 'GET'])
def webhook():
    global PENDING_MARGIN_INPUT
    
    data = request.get_json(silent=True)
    
    if data:
        print(f"📥 TELEGRAM JSON DATA: {json.dumps(data, ensure_ascii=False)}")
        
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
                clean_symbol = symbol.replace(".P", "").replace(".p", "").replace("USDT", "")
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
                        short_coin = symbol

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
                            time.sleep(0.5)
                            new_positions = get_okx_all_positions()
                            new_pos = next((p for p in new_positions if p["instId"] == inst_id), None)
                            active_lev = new_pos.get("lever", "20") if new_pos else "20"
                            send_telegram_msg(
                                f"{emoji} *Вход: {side}*\n"
                                f"• Монета: `{short_coin}`\n"
                                f"• Маржа: `${margin_usdt} USDT`\n"
                                f"• Плечо: `{active_lev}x`\n"
                                f"• Паттерн: `Manual`"
                            )
                        else:
                            send_telegram_msg(f"⚠️ *Ошибка открытия `{short_coin}`*\n• Причина: `{err_msg}`")
                        return "OK", 200
                except ValueError:
                    send_telegram_msg("⚠️ Пожалуйста, введите число для маржи (например: `10`)")
        
        return "OK", 200

    raw_data = request.get_data(as_text=True)
    print(f"📈 TRADINGVIEW ALERT: {raw_data}")
    
    symbol, signal_type, pattern_name = parse_alert(raw_data)

    if not symbol or symbol == "UNKNOWN" or "{{" in symbol or signal_type == "UNKNOWN":
        return "OK", 200

    clean_symbol = symbol.replace(".P", "").replace(".p", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"
    short_coin = symbol

    positions = get_okx_all_positions()
    pos_info = next((p for p in positions if p["instId"] == inst_id), None)
    current_pos = pos_info["posSide"] if pos_info else None
    upl = pos_info.get("upl", 0.0) if pos_info else 0.0
    upl_ratio = pos_info.get("uplRatio", 0.0) if pos_info else 0.0
    pnl_sign = "+" if upl >= 0 else ""
    pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"

    # --- ОБРАБОТКА СИГНАЛОВ ВЫХОДА (С ПРОВЕРКОЙ ПРИБЫЛИ UPL > 0) ---
    if signal_type in ["EXIT_LONG", "EXIT_SHORT"]:
        target_pos_side = "long" if signal_type == "EXIT_LONG" else "short"
        
        if not pos_info or current_pos != target_pos_side:
            print(f"🔕 Сигнал {signal_type} для {inst_id} проигнорирован: активная позиция {target_pos_side} не найдена.")
            return "OK", 200

        if upl <= 0:
            print(f"📊 {inst_id} ({target_pos_side.upper()}): сигнал выхода по RSI получен, но позиция в просадке/нуле (PnL: {pnl_str}). Выход пропущен.")
            return "OK", 200

        print(f"🔔 [RSI WEBHOOK] Закрытие прибыльного {target_pos_side.upper()} для {inst_id} | PnL: {pnl_str}")
        close_res = close_okx_position(inst_id, target_pos_side)
        
        if close_res.get("code") == "0":
            icon = "✅"
            send_telegram_msg(
                f"{icon} *Тейк-профит (RSI Выход)*\n"
                f"• Монета: `{short_coin}`\n"
                f"• Стратегия: `{pattern_name}`\n"
                f"• PnL: `{pnl_str}`"
            )
        else:
            print(f"❌ Ошибка закрытия позиции {inst_id}: {close_res}")

        return "OK", 200

    # --- ОБРАБОТКА СИГНАЛОВ ВХОДА ---
    if signal_type in ["ENTRY_LONG", "ENTRY_SHORT"]:
        side = "LONG" if signal_type == "ENTRY_LONG" else "SHORT"
        circle_emoji = "🟢" if side == "LONG" else "🔴"

        if side == "LONG" and current_pos == "short":
            close_res = close_okx_position(inst_id, "short")
            if close_res.get("code") == "0":
                send_telegram_msg(f"🔄 *Разворот позиции*\n• Закрыт SHORT по `{inst_id}`\n• PnL: `{pnl_str}`")
        elif side == "SHORT" and current_pos == "long":
            close_res = close_okx_position(inst_id, "long")
            if close_res.get("code") == "0":
                send_telegram_msg(f"🔄 *Разворот позиции*\n• Закрыт LONG по `{inst_id}`\n• PnL: `{pnl_str}`")

        success, err_msg = execute_okx_trade(symbol, side, DEFAULT_MARGIN)
        
        if success:
            time.sleep(0.5)
            new_positions = get_okx_all_positions()
            new_pos = next((p for p in new_positions if p["instId"] == inst_id), None)
            active_lev = new_pos.get("lever", "20") if new_pos else "20"
            send_telegram_msg(
                f"{circle_emoji} *Вход: {side}*\n"
                f"• Монета: `{short_coin}`\n"
                f"• Маржа: `${DEFAULT_MARGIN} USDT`\n"
                f"• Плечо: `{active_lev}x`\n"
                f"• Паттерн: `{pattern_name}`"
            )
        else:
            send_telegram_msg(f"⚠️ *Защита депозита / Ошибка открытия `{short_coin}`*\n• Причина: `{err_msg}`")

    return "OK", 200

@app.route('/', methods=['POST', 'GET'])
def index():
    if request.method == 'GET':
        return "OKX Smart Bot is Running!", 200
    return webhook()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
