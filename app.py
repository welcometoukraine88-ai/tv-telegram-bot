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

app = Flask(__name__)

# Токены и ключи из Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

OKX_BASE_URL = "https://www.okx.com"

# НАСТРОЙКИ ТЕЙК-ПРОФИТА ПО RSI(14)
RSI_TIMEFRAME = "15m"       # Таймфрейм свечей
RSI_LEVEL_HIGH = 70.0      # Верхний уровень RSI (TP LONG)
RSI_LEVEL_LOW = 30.0       # Нижний уровень RSI (TP SHORT)

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

# ================= РАСЧЕТ RSI(14) ПО ЗАКРЫТЫМ СВЕЧАМ =================

def calculate_rsi14_history(inst_id, timeframe=RSI_TIMEFRAME):
    """
    Точный расчет RSI(14) по методу RMA (Wilder's Smoothing), 
    в точности повторяющий стандартный индикатор TradingView.
    """
    # Запрашиваем 200 свечей для правильной «раскачки» сглаживания RMA
    res = okx_request("GET", f"/api/v5/market/candles?instType=SWAP&instId={inst_id}&bar={timeframe}&limit=200")
    if res.get("code") != "0" or not res.get("data"):
        return None

    raw_candles = res["data"]
    # Берем только закрытые свечи (индекс 8 равен "1" в OKX API)
    closed_candles = [c for c in raw_candles if c[8] == "1"]

    if len(closed_candles) < 30:
        closed_candles = raw_candles[1:]

    if len(closed_candles) < 50:
        return None

    # OKX отдает свечи от новых к старым, разворачиваем от старых к новым
    candles = list(reversed(closed_candles))
    closes = [float(c[4]) for c in candles]

    # Считаем изменения цены (ta.change)
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    
    gains = [max(c, 0.0) for c in changes]
    losses = [abs(min(c, 0.0)) for c in changes]

    length = 14
    if len(gains) < length:
        return None

    rsi_values = []

    # 1. Первый шаг: классическое SMA для инициализации RMA (как в TradingView)
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length

    # Расчет RSI для первой точки
    if avg_loss == 0:
        rsi = 100.0
    elif avg_gain == 0:
        rsi = 0.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi_values.append(round(rsi, 2))

    # 2. Дальнейшие шаги: рекурсивная формула RMA (Wilder's Smoothing)
    # Формула TV: avg = (previous_avg * (length - 1) + current_value) / length
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
    """Проверка ТЕЙК-ПРОФИТА (Crossover / Crossunder) по RSI 15m"""
    try:
        positions = get_okx_all_positions()
        if not positions:
            return

        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Проверка тейк-профитов RSI по закрытию 15м свечи...")

        for pos in positions:
            inst_id = pos["instId"]
            pos_side = pos["posSide"]
            rsi_history = calculate_rsi14_history(inst_id)

            if not rsi_history or len(rsi_history) < 2:
                continue

            prev_rsi = rsi_history[-2]  # Предпоследняя закрытая свеча
            curr_rsi = rsi_history[-1]  # Последняя закрытая свеча

            upl = pos.get("upl", 0.0)
            upl_ratio = pos.get("uplRatio", 0.0)
            pnl_sign = "+" if upl >= 0 else ""
            pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"

            # 1. ТЕЙК-ПРОФИТ LONG: Пробитие 70 СНИЗУ ВВЕРХ
            if pos_side == "long":
                if prev_rsi < RSI_LEVEL_HIGH and curr_rsi >= RSI_LEVEL_HIGH:
                    close_res = close_okx_position(inst_id, "long")
                    if close_res.get("code") == "0":
                        msg = (
                            f"🎯 **Тейк-профит LONG по `{inst_id}`**\n"
                            f"• RSI(14) пробил 70 снизу вверх: **{prev_rsi} ➔ {curr_rsi}**\n"
                            f"💰 **PnL:** `{pnl_str}`"
                        )
                        send_telegram_msg(msg)

            # 2. ТЕЙК-ПРОФИТ SHORT: Пробитие 30 СВЕРХУ ВНИЗ
            elif pos_side == "short":
                if prev_rsi > RSI_LEVEL_LOW and curr_rsi <= RSI_LEVEL_LOW:
                    close_res = close_okx_position(inst_id, "short")
                    if close_res.get("code") == "0":
                        msg = (
                            f"🎯 **Тейк-профит SHORT по `{inst_id}`**\n"
                            f"• RSI(14) пробил 30 сверху вниз: **{prev_rsi} ➔ {curr_rsi}**\n"
                            f"💰 **PnL:** `{pnl_str}`"
                        )
                        send_telegram_msg(msg)

    except Exception as e:
        print(f"Ошибка проверки RSI: {e}")

# ================= РАСЧЕТ И ИСПОЛНЕНИЕ ОРДЕРА =================

def execute_okx_trade(symbol, side_type, margin_usdt):
    """Открывает сделку с максимальным плечом (без фиксированного стоп-лосса)"""
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
        print(f"Ошибка информации об инструменте {inst_id}: {e}")

    if last_price <= 0:
        return False, f"Не удалось получить цену для `{inst_id}`."

    set_okx_leverage(inst_id, max_lev)

    min_contract_notional = ct_val * lot_sz * last_price
    min_margin_required = min_contract_notional / max_lev

    if margin_usdt < min_margin_required:
        suggested_margin = round(min_margin_required + 0.01, 2)
        return False, (
            f"❌ **Недостаточно маржи для {inst_id}!**\n\n"
            f"• Минимальная маржа: `${suggested_margin} USDT`"
        )

    target_notional_usdt = margin_usdt * max_lev
    raw_contracts = target_notional_usdt / (ct_val * last_price)
    contracts_qty = math.floor(raw_contracts / lot_sz) * lot_sz

    if contracts_qty < lot_sz:
        suggested_margin = round(min_margin_required + 0.01, 2)
        return False, f"👉 Введите маржу от **${suggested_margin} USDT**."

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
            f"✅ **Ордер открыт!**\n\n"
            f"• Инструмент: `{inst_id}`\n"
            f"• Направление: **{side_type}**\n"
            f"• Плечо: **{max_lev}x**\n"
            f"• Маржа: **~${round(actual_margin_used, 2)} USDT**\n"
            f"• Объем: **~${round(actual_notional_usdt, 2)} USDT**"
        )
    else:
        msg = res.get("data", [{}])[0].get("sMsg") or res.get("msg")
        return False, f"Ошибка OKX: {msg}"

# ================= TELEGRAM & WEBHOOKS =================

def send_telegram_signal(symbol, signal_type, is_averaging=False, pattern_name="SPRING"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    emoji = "🔴" if signal_type == "SHORT" else "🟢"
    action_title = "УСРЕДНЕНИЕ (ДОБОР)" if is_averaging else f"СИГНАЛ ({pattern_name})"

    text = (
        f"{emoji} **{action_title}**\n\n"
        f"Монета: `{symbol}`\n"
        f"Направление: **{signal_type}**"
    )

    reply_markup = {
        "inline_keyboard": [
            [{"text": f"🚀 Войти ({signal_type})", "callback_data": f"INIT_{signal_type}_{symbol}"}],
            [{"text": "❌ Пропустить", "callback_data": "CANCEL"}]
        ]
    }

    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown", "reply_markup": reply_markup}, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки сигнала: {e}")

def parse_alert(text):
    """Парсинг названий инструментов (включая формат METAUSDT.P) и паттернов Вайкоффа"""
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
    
    if "spring confirmed" in text_lower:
        signal_type = "ENTRY_LONG"
        pattern_name = "SPRING CONFIRMED"
    elif "spring" in text_lower:
        signal_type = "ENTRY_LONG"
        pattern_name = "SPRING"
    elif "utad confirmed" in text_lower:
        signal_type = "ENTRY_SHORT"
        pattern_name = "UTAD CONFIRMED"
    elif "upthrust" in text_lower:
        signal_type = "ENTRY_SHORT"
        pattern_name = "UPTHRUST"
    elif "markup" in text_lower:
        signal_type = "EXIT_LONG"
        pattern_name = "MARKUP"
    elif "markdown" in text_lower:
        signal_type = "EXIT_SHORT"
        pattern_name = "MARKDOWN"

    return symbol, signal_type, pattern_name

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

    # --- 1. ВХОД В LONG (SPRING / SPRING CONFIRMED) ---
    if signal_type == "ENTRY_LONG":
        if current_pos == "short":
            upl = pos_info.get("upl", 0.0)
            upl_ratio = pos_info.get("uplRatio", 0.0)
            close_res = close_okx_position(inst_id, "short")
            if close_res.get("code") == "0":
                pnl_sign = "+" if upl >= 0 else ""
                pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"
                msg = (
                    f"🔄 **Закрыт SHORT по `{inst_id}`** (получен разворотный сигнал {pattern_name}).\n"
                    f"💰 **PnL:** `{pnl_str}`"
                )
                send_telegram_msg(msg)

        is_avg = (current_pos == "long")
        send_telegram_signal(symbol, "LONG", is_averaging=is_avg, pattern_name=pattern_name)

    # --- 2. ВХОД В SHORT (UPTHRUST / UTAD CONFIRMED) ---
    elif signal_type == "ENTRY_SHORT":
        if current_pos == "long":
            upl = pos_info.get("upl", 0.0)
            upl_ratio = pos_info.get("uplRatio", 0.0)
            close_res = close_okx_position(inst_id, "long")
            if close_res.get("code") == "0":
                pnl_sign = "+" if upl >= 0 else ""
                pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"
                msg = (
                    f"🔄 **Закрыт LONG по `{inst_id}`** (получен разворотный сигнал {pattern_name}).\n"
                    f"💰 **PnL:** `{pnl_str}`"
                )
                send_telegram_msg(msg)

        is_avg = (current_pos == "short")
        send_telegram_signal(symbol, "SHORT", is_averaging=is_avg, pattern_name=pattern_name)

    # --- 3. ВЫХОД ИЗ LONG (MARKUP) ---
    elif signal_type == "EXIT_LONG":
        if current_pos == "long":
            upl = pos_info.get("upl", 0.0)
            upl_ratio = pos_info.get("uplRatio", 0.0)
            close_res = close_okx_position(inst_id, "long")
            if close_res.get("code") == "0":
                pnl_sign = "+" if upl >= 0 else ""
                pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"
                msg = (
                    f"🎯 **Закрыт LONG по `{inst_id}`** (получен сигнал Markup).\n"
                    f"💰 **PnL:** `{pnl_str}`"
                )
                send_telegram_msg(msg)

    # --- 4. ВЫХОД ИЗ SHORT (MARKDOWN) ---
    elif signal_type == "EXIT_SHORT":
        if current_pos == "short":
            upl = pos_info.get("upl", 0.0)
            upl_ratio = pos_info.get("uplRatio", 0.0)
            close_res = close_okx_position(inst_id, "short")
            if close_res.get("code") == "0":
                pnl_sign = "+" if upl >= 0 else ""
                pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"
                msg = (
                    f"🎯 **Закрыт SHORT по `{inst_id}`** (получен сигнал Markdown).\n"
                    f"💰 **PnL:** `{pnl_str}`"
                )
                send_telegram_msg(msg)

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

                send_telegram_msg(f"⏳ Открываем {side_type} по {symbol} на **${margin_usdt}**...")
                success, result_msg = execute_okx_trade(symbol, side_type, margin_usdt)
                send_telegram_msg(result_msg)

            except ValueError:
                send_telegram_msg("⚠️ Введите число (например: `50` или `100`).")

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
                    "chat_id": chat_id, "message_id": message_id, "text": "❌ **Сигнал отменён.**", "parse_mode": "Markdown"
                }, timeout=5)
            except Exception as e:
                print(f"Ошибка отмены: {e}")

        elif action.startswith("INIT_"):
            parts = action.split("_")
            side_type = parts[1]
            symbol = parts[2]

            PENDING_TRADES[chat_id] = {"symbol": symbol, "side_type": side_type}

            try:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": callback_id}, timeout=5)
            except Exception as e:
                print(f"Ошибка Callback: {e}")

            send_telegram_msg(f"💵 **Введи сумму маржи в USDT для входа в {side_type} ({symbol}):**")

    return "OK", 200

@app.route('/', methods=['GET'])
def index():
    return "OKX Signal Bot (Multi-Signal & Auto-Flip) is Running!", 200

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
