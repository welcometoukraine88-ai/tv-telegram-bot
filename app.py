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
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

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
    return okx_request("POST", "/api/v5/trade/close-position", body)

def calculate_rsi_history(inst_id, timeframe=RSI_TIMEFRAME, limit=30):
    res = okx_request("GET", f"/api/v5/market/candles?instType=SWAP&instId={inst_id}&bar={timeframe}&limit={limit}")
    if res.get("code") != "0" or not res.get("data"):
        return []

    raw_candles = res["data"]
    if len(raw_candles) < 20:
        return []

    candles = list(reversed(raw_candles))
    closes = [float(c[4]) for c in candles]

    rsi_list = []
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [abs(min(c, 0.0)) for c in changes]

    length = 14
    if len(gains) < length:
        return []

    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length

    if avg_loss == 0:
        rsi_list.append(100.0)
    elif avg_gain == 0:
        rsi_list.append(0.0)
    else:
        rs = avg_gain / avg_loss
        rsi_list.append(round(100.0 - (100.0 / (1.0 + rs)), 2))

    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        if avg_loss == 0:
            rsi_list.append(100.0)
        elif avg_gain == 0:
            rsi_list.append(0.0)
        else:
            rs = avg_gain / avg_loss
            rsi_list.append(round(100.0 - (100.0 / (1.0 + rs)), 2))

    return rsi_list

def execute_okx_trade(symbol, side_type, margin_usdt):
    clean_symbol = symbol.replace(".P", "").replace(".p", "").replace("USDT", "")
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
        potential_prefix = parts[0].strip().replace('"', '').replace("'", "")
        
        clean_prefix_match = re.search(r'^([A-Za-z0-9_\-\.]+)(?:\s+\d+)?$', potential_prefix)
        if clean_prefix_match:
            candidate_sym = clean_prefix_match.group(1).upper()
            if len(candidate_sym) > 2:
                symbol = candidate_sym

        text_str = parts[1]

    if symbol == "UNKNOWN":
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
    elif "upthrust" in text_lower or "utad" in text_lower:
        signal_type = "ENTRY_SHORT"
        pattern_name = "Upthrust / UTAD"
    
    elif "buying climax" in text_lower:
        signal_type = "EXIT_LONG"
        pattern_name = "Buying Climax"
    elif "sign of strength" in text_lower:
        signal_type = "EXIT_LONG"
        pattern_name = "Sign of Strength"
    elif "distribution entering phase b" in text_lower:
        signal_type = "EXIT_LONG"
        pattern_name = "Distribution entering Phase B"
    elif "markup" in text_lower:
        signal_type = "EXIT_LONG"
        pattern_name = "Markup"

    elif "selling climax" in text_lower:
        signal_type = "EXIT_SHORT"
        pattern_name = "Selling Climax"
    elif "sign of weakness" in text_lower:
        signal_type = "EXIT_SHORT"
        pattern_name = "Sign of Weakness"
    elif "accumulation entering phase b" in text_lower:
        signal_type = "EXIT_SHORT"
        pattern_name = "Accumulation entering Phase B"
    elif "markdown" in text_lower:
        signal_type = "EXIT_SHORT"
        pattern_name = "Markdown"

    return symbol, signal_type, pattern_name

# --- ФОНОВАЯ ПРОВЕРКА (ЖЕСТКИЙ СТОП 10% + ТЕЙК-ПРОФИТЫ RSI) КАЖДЫЕ 15 МИНУТ ---
def scheduled_rsi_check():
    print("⏰ Запуск плановой проверки позиций (Жесткий стоп 10% + Тейк-профиты RSI)...")
    positions = get_okx_all_positions()
    if not positions:
        return

    for pos in positions:
        inst_id = pos["instId"]
        current_pos = pos["posSide"]
        avg_px = pos["avgPx"]
        upl = pos.get("upl", 0.0)
        upl_ratio = pos.get("uplRatio", 0.0)
        pnl_sign = "+" if upl >= 0 else ""
        pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"
        short_coin = inst_id.replace("-USDT-SWAP", "USDT.P")
        icon = "✅" if upl >= 0 else "❌"

        # 1. Проверка жесткого стоп-лосса в 10% движения цены против позиции
        current_price = 0.0
        try:
            ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={inst_id}")
            if ticker_res.get("code") == "0" and ticker_res.get("data"):
                current_price = float(ticker_res["data"][0].get("last", 0.0))
        except Exception as e:
            print(f"Ошибка получения цены для стопа {inst_id}: {e}")

        if avg_px > 0 and current_price > 0:
            price_diff_pct = 0.0
            if current_pos == "long":
                price_diff_pct = ((current_price - avg_px) / avg_px) * 100
                if price_diff_pct <= -10.0:
                    close_res = close_okx_position(inst_id, "long")
                    if close_res.get("code") == "0":
                        send_telegram_msg(
                            f"❌ *Жесткий стоп-лосс (10% цены)*\n"
                            f"• Монета: `{short_coin}`\n"
                            f"• Убыток по цене: `{price_diff_pct:.2f}%`\n"
                            f"• PnL: `{pnl_str}`"
                        )
                        continue
            elif current_pos == "short":
                price_diff_pct = ((avg_px - current_price) / avg_px) * 100
                if price_diff_pct <= -10.0:
                    close_res = close_okx_position(inst_id, "short")
                    if close_res.get("code") == "0":
                        send_telegram_msg(
                            f"❌ *Жесткий стоп-лосс (10% цены)*\n"
                            f"• Монета: `{short_coin}`\n"
                            f"• Рост против шорта: `{abs(price_diff_pct):.2f}%`\n"
                            f"• PnL: `{pnl_str}`"
                        )
                        continue

        # 2. Проверка Тейк-профитов по RSI
        rsi_history = calculate_rsi_history(inst_id, RSI_TIMEFRAME, limit=30)
        if len(rsi_history) < 2:
            continue

        prev_rsi = rsi_history[-2]
        curr_rsi = rsi_history[-1]

        print(f"📊 {inst_id} ({current_pos.upper()}): prev_RSI = {prev_rsi}, curr_RSI = {curr_rsi}")

        if current_pos == "long" and curr_rsi >= RSI_EXIT_LONG:
            close_res = close_okx_position(inst_id, "long")
            if close_res.get("code") == "0":
                send_telegram_msg(
                    f"{icon} *Тейк-профит (RSI)*\n"
                    f"• Монета: `{short_coin}`\n"
                    f"• PnL: `{pnl_str}`"
                )
                continue

        elif current_pos == "short" and curr_rsi <= RSI_EXIT_SHORT:
            close_res = close_okx_position(inst_id, "short")
            if close_res.get("code") == "0":
                send_telegram_msg(
                    f"{icon} *Тейк-профит (RSI)*\n"
                    f"• Монета: `{short_coin}`\n"
                    f"• PnL: `{pnl_str}`"
                )
                continue

scheduler = BackgroundScheduler()
# Запуск строго в 0, 15, 30 и 45 минут каждого часа на 5-й секунде
scheduler.add_job(
    func=scheduled_rsi_check, 
    trigger=CronTrigger(minute="0,15,30,45", second="5")
)
scheduler.start()

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

    # 2. Обработка алертов от TradingView
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
    exit_icon = "✅" if upl >= 0 else "❌"

    # Обработка парсинга выхода из лонга (Вайкоф)
    if signal_type == "EXIT_LONG" and current_pos == "long":
        close_res = close_okx_position(inst_id, "long")
        if close_res.get("code") == "0":
            send_telegram_msg(
                f"{exit_icon} *Выход ({pattern_name})*\n"
                f"• Монета: `{short_coin}`\n"
                f"• PnL: `{pnl_str}`"
            )
        return "OK", 200

    # Обработка парсинга выхода из шорта (Вайкоф)
    if signal_type == "EXIT_SHORT" and current_pos == "short":
        close_res = close_okx_position(inst_id, "short")
        if close_res.get("code") == "0":
            send_telegram_msg(
                f"{exit_icon} *Выход ({pattern_name})*\n"
                f"• Монета: `{short_coin}`\n"
                f"• PnL: `{pnl_str}`"
            )
        return "OK", 200

    if signal_type in ["EXIT_LONG", "EXIT_SHORT"]:
        return "OK", 200

    # Автоматический вход по сигналу на DEFAULT_MARGIN (1 USDT) с разворотом
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
            send_telegram_msg(f"⚠️ *Ошибка открытия `{short_coin}`*\n• Причина: `{err_msg}`")

    return "OK", 200

@app.route('/', methods=['POST', 'GET'])
def index():
    if request.method == 'GET':
        return "OKX Smart Bot is Running!", 200
    return webhook()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
