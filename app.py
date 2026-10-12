os
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

app = Flask(__name__)

# Токены и ключи из Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

OKX_BASE_URL = "https://www.okx.com"

# НАСТРОЙКИ ПО УМОЛЧАНИЮ
RSI_TIMEFRAME = "15m"         # Таймфрейм свечей
RSI_TRAIL_LONG = 65.0        # Трейлинг для LONG
RSI_TRAIL_SHORT = 35.0       # Трейлинг для SHORT
RSI_SL_LONG = 30.0           # Стоп-лосс для LONG
RSI_SL_SHORT = 70.0          # Стоп-лосс для SHORT

# Хранилище настроек в памяти (дефолтная маржа на сделку — 50 USDT)
BOT_SETTINGS = {
    "default_margin": 50.0
}

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

def get_okx_all_positions():
    """Получает список всех открытых позиций на OKX"""
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
    """Полностью закрывает позицию по рынку (Market Close)"""
    body = {
        "instId": inst_id,
        "mgnMode": "cross",
        "posSide": pos_side
    }
    return okx_request("POST", "/api/v5/trade/close-position", body)

# ================= ТОЧНЫЙ РАСЧЕТ RSI(14) ПО TRADINGVIEW (RMA) =================

def calculate_rsi14_history(inst_id, timeframe=RSI_TIMEFRAME):
    res = okx_request("GET", f"/api/v5/market/candles?instType=SWAP&instId={inst_id}&bar={timeframe}&limit=200")
    if res.get("code") != "0" or not res.get("data"):
        return None

    raw_candles = res["data"]
    closed_candles = [c for c in raw_candles if c[8] == "1"]

    if len(closed_candles) < 30:
        closed_candles = raw_candles[1:]

    if len(closed_candles) < 50:
        return None

    candles = list(reversed(closed_candles))
    closes = [float(c[4]) for c in candles]

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [abs(min(c, 0.0)) for c in changes]

    length = 14
    if len(gains) < length:
        return None

    rsi_values = []
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length

    if avg_loss == 0:
        rsi = 100.0
    elif avg_gain == 0:
        rsi = 0.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi_values.append(round(rsi, 2))

    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length

        if avg_loss == 0:
            rsi = 100.0
        elif avg_gain == 0:
            rsi = 0.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        rsi_values.append(round(rsi, 2))

    return rsi_values

def check_and_close_positions_by_rsi():
    try:
        positions = get_okx_all_positions()
        if not positions:
            return

        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Проверка RSI (SL & Trailing)...")

        for pos in positions:
            inst_id = pos["instId"]
            pos_side = pos["posSide"]
            rsi_history = calculate_rsi14_history(inst_id)

            if not rsi_history or len(rsi_history) < 2:
                continue

            prev_rsi = rsi_history[-2]
            curr_rsi = rsi_history[-1]

            upl = pos.get("upl", 0.0)
            upl_ratio = pos.get("uplRatio", 0.0)
            pnl_sign = "+" if upl >= 0 else ""
            pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"

            if pos_side == "long":
                if curr_rsi < RSI_SL_LONG:
                    close_res = close_okx_position(inst_id, "long")
                    if close_res.get("code") == "0":
                        send_telegram_msg(f"🛑 **СТОП-ЛОСС LONG по `{inst_id}`**\n• RSI < 30: **{curr_rsi}**\n💰 **PnL:** `{pnl_str}`")
                elif curr_rsi >= RSI_TRAIL_LONG and prev_rsi > curr_rsi:
                    close_res = close_okx_position(inst_id, "long")
                    if close_res.get("code") == "0":
                        send_telegram_msg(f"🎯 **Трейлинг-стоп LONG по `{inst_id}`**\n• RSI разворот вниз: **{prev_rsi} ➔ {curr_rsi}**\n💰 **PnL:** `{pnl_str}`")

            elif pos_side == "short":
                if curr_rsi > RSI_SL_SHORT:
                    close_res = close_okx_position(inst_id, "short")
                    if close_res.get("code") == "0":
                        send_telegram_msg(f"🛑 **СТОП-ЛОСС SHORT по `{inst_id}`**\n• RSI > 70: **{curr_rsi}**\n💰 **PnL:** `{pnl_str}`")
                elif curr_rsi <= RSI_TRAIL_SHORT and prev_rsi < curr_rsi:
                    close_res = close_okx_position(inst_id, "short")
                    if close_res.get("code") == "0":
                        send_telegram_msg(f"🎯 **Трейлинг-стоп SHORT по `{inst_id}`**\n• RSI разворот вверх: **{prev_rsi} ➔ {curr_rsi}**\n💰 **PnL:** `{pnl_str}`")

    except Exception as e:
        print(f"Ошибка проверки RSI: {e}")

# ================= АВТОМАТИЧЕСКОЕ И СПОЛНЕНИЕ ОРДЕРА =================

def execute_okx_trade(symbol, side_type, margin_usdt):
    """Открывает сделку с максимальным плечом автоматически"""
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
        print(f"Ошибка получения данных для {inst_id}: {e}")

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
        suggested_margin = round(min_margin_required + 0.01, 2)
        return False, f"👉 Увеличьте маржу от **${suggested_margin} USDT**."

    formatted_sz = str(int(contracts_qty)) if lot_sz.is_integer() else f"{contracts_qty:.4f}".rstrip('0').rstrip('.')
    actual_notional_usdt = contracts_qty * ct_val * last_price
    actual_margin_used = actual_notional_usdt / max_lev

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
            f"🚀 **Авто-ордер открыт!**\n\n"
            f"• Инструмент: `{inst_id}`\n"
            f"• Направление: **{side_type}**\n"
            f"• Плечо: **{max_lev}x**\n"
            f"• Маржа: **~${round(actual_margin_used, 2)} USDT**\n"
            f"• Объем: **~${round(actual_notional_usdt, 2)} USDT**"
        )
    else:
        msg = res.get("data", [{}])[0].get("sMsg") or res.get("msg")
        return False, f"Ошибка OKX: {msg}"

# ================= ПАРСЕР АЛЕРТОВ =================

def parse_alert(text):
    symbol = "UNKNOWN"
    signal_type = "UNKNOWN"
    pattern_name = "SIGNAL"

    text_str = text if isinstance(text, str) else json.dumps(text)

    try:
        data = json.loads(text_str)
        if isinstance(data, dict):
            raw_sym = data.get("instrument") or data.get("symbol") or data.get("ticker") or ""
            if raw_sym:
                symbol = raw_sym.split(":")[-1].strip() if ":" in raw_sym else raw_sym.strip()
    except Exception:
        pass

    if symbol == "UNKNOWN":
        match = re.search(r'(?:[A-Z0-9]+:)?([A-Z0-9]+\.P|[A-Z0-9]+USDT)', text_str)
        if match:
            symbol = match.group(1)
            if ":" in symbol:
                symbol = symbol.split(":")[-1]

    text_lower = text_str.lower()
    
    if "spring" in text_lower:
        signal_type = "ENTRY_LONG"
        pattern_name = "SPRING"
    elif "ut" in text_lower or re.search(r'\but\b', text_lower):
        signal_type = "ENTRY_SHORT"
        pattern_name = "UT"

    return symbol, signal_type, pattern_name

# ================= WEBHOOKS И TELEGRAM КОМАНДЫ =================

@app.route('/webhook', methods=['POST'])
def webhook():
    raw_data = request.get_data(as_text=True)
    symbol, signal_type, pattern_name = parse_alert(raw_data)

    if symbol == "UNKNOWN" or signal_type == "UNKNOWN":
        return "Ignored", 200

    clean_symbol = symbol.replace(".P", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"

    positions = get_okx_all_positions()
    pos_info = next((p for p in positions if p["instId"] == inst_id), None)
    current_pos = pos_info["posSide"] if pos_info else None

    # Получаем текущую настроенную маржу
    current_margin = BOT_SETTINGS["default_margin"]

    # 1. СИГНАЛ LONG (SPRING)
    if signal_type == "ENTRY_LONG":
        if current_pos == "short":
            upl = pos_info.get("upl", 0.0)
            upl_ratio = pos_info.get("uplRatio", 0.0)
            close_res = close_okx_position(inst_id, "short")
            if close_res.get("code") == "0":
                pnl_sign = "+" if upl >= 0 else ""
                send_telegram_msg(f"🔄 **Закрыт SHORT по `{inst_id}`** (сигнал Spring).\n💰 **PnL:** `{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)`")

        # Открываем LONG автоматически
        send_telegram_msg(f"🟢 **Сигнал SPRING ({symbol})**. Открываем LONG на **${current_margin}**...")
        success, res_msg = execute_okx_trade(symbol, "LONG", current_margin)
        send_telegram_msg(res_msg)

    # 2. СИГНАЛ SHORT (UT)
    elif signal_type == "ENTRY_SHORT":
        if current_pos == "long":
            upl = pos_info.get("upl", 0.0)
            upl_ratio = pos_info.get("uplRatio", 0.0)
            close_res = close_okx_position(inst_id, "long")
            if close_res.get("code") == "0":
                pnl_sign = "+" if upl >= 0 else ""
                send_telegram_msg(f"🔄 **Закрыт LONG по `{inst_id}`** (сигнал UT).\n💰 **PnL:** `{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)`")

        # Открываем SHORT автоматически
        send_telegram_msg(f"🔴 **Сигнал UT ({symbol})**. Открываем SHORT на **${current_margin}**...")
        success, res_msg = execute_okx_trade(symbol, "SHORT", current_margin)
        send_telegram_msg(res_msg)

    return "OK", 200

@app.route('/telegram-callback', methods=['POST'])
def telegram_callback():
    data = request.get_json()

    if "message" in data and "text" in data["message"]:
        chat_id = str(data["message"]["chat"]["id"])
        user_text = data["message"]["text"].strip()

        # Обработка команды /margin <сумма> или просто числа
        if user_text.startswith("/margin") or user_text.startswith("/setmargin"):
            parts = user_text.split()
            if len(parts) > 1:
                try:
                    new_margin = float(parts[1].replace(",", "."))
                    if new_margin > 0:
                        BOT_SETTINGS["default_margin"] = new_margin
                        send_telegram_msg(f"✅ Маржа по умолчанию изменена на: **${new_margin} USDT**")
                    else:
                        send_telegram_msg("⚠️ Сумма должна быть больше нуля.")
                except ValueError:
                    send_telegram_msg("⚠️ Неверный формат. Пример: `/margin 100`")
            else:
                send_telegram_msg(f"ℹ️ Текущая маржа на сделку: **${BOT_SETTINGS['default_margin']} USDT**\nДля изменения введите: `/margin <сумма>`")
        
        elif user_text.startswith("/settings") or user_text.startswith("/status"):
            send_telegram_msg(f"⚙️ **Текущие настройки бота:**\n• Маржа на сделку: **${BOT_SETTINGS['default_margin']} USDT**\n• Плечо: **Максимальное (авто)**")

    return "OK", 200

@app.route('/', methods=['GET'])
def index():
    return "OKX Fully Automated Bot is Running!", 200

# ИНИЦИАЛИЗАЦИЯ ПЛАНИРОВЩИКА СТРОГО ПО 15M СВЕЧАМ
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(
    func=check_and_close_positions_by_rsi,
    trigger='cron',
    minute='0,15,30,45',
    second='3',
    id='rsi_checker_job'
)
scheduler.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
