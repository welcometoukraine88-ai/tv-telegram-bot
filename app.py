import os
import re
import json
import time
import math
import base64
import hmac
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request
import requests
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

OKX_BASE_URL = "https://www.okx.com"

RSI_TIMEFRAME = "15m"
RSI_TRAIL_LONG = 65.0
RSI_TRAIL_SHORT = 35.0
RSI_SL_LONG = 30.0
RSI_SL_SHORT = 70.0

# Сумма маржи по умолчанию
BOT_SETTINGS = {
    "default_margin": 1.0
}

STATS_DATA = {
    "tp_closed": 0,
    "sl_closed": 0,
    "total_pnl": 0.0
}

# Очередь отложенных входов (когда ждем пересечения RSI 30 / 70)
PENDING_ENTRY_SIGNALS = []

# Хранилище времени открытия сделок для защиты от СЛ в первые 4 бара (1 час)
ACTIVE_TRADES_META = {}

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
        # Фиксируем время входа для защиты СЛ на 4 бара (1 час)
        ACTIVE_TRADES_META[inst_id] = {
            "entry_time": time.time(),
            "bars_passed": 0
        }
        return True, (
            f"🚀 **Авто-ордер открыт!**\n\n"
            f"• Инструмент: `{inst_id}`\n"
            f"• Направление: **{side_type}**\n"
            f"• Плечо: **~{max_lev}x**\n"
            f"• Маржа: **~${round(actual_margin_used, 2)} USDT**"
        )
    else:
        msg = res.get("data", [{}])[0].get("sMsg") or res.get("msg")
        return False, f"Ошибка OKX: {msg}"

def process_signal_execution(symbol, side_type):
    """Непосредственный вход с проверкой RSI"""
    clean_symbol = symbol.replace(".P", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"
    rsi_history = calculate_rsi14_history(inst_id)

    if not rsi_history or len(rsi_history) < 1:
        return False, "Нет данных RSI"

    curr_rsi = rsi_history[-1]
    current_margin = float(BOT_SETTINGS["default_margin"])

    positions = get_okx_all_positions()
    pos_info = next((p for p in positions if p["instId"] == inst_id), None)
    current_pos = pos_info["posSide"] if pos_info else None

    if side_type == "LONG":
        if curr_rsi < 30:
            return False, f"RSI = {curr_rsi} (<30). Ждем поднятия выше 30."
        
        if current_pos == "short":
            upl = pos_info.get("upl", 0.0)
            upl_ratio = pos_info.get("uplRatio", 0.0)
            close_res = close_okx_position(inst_id, "short")
            if close_res.get("code") == "0":
                STATS_DATA["total_pnl"] += upl
                pnl_sign = "+" if upl >= 0 else ""
                send_telegram_msg(f"🔄 **Закрыт SHORT по `{inst_id}`** (разворот Spring).\n💰 **PnL:** `{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)`")

        success, res_msg = execute_okx_trade(symbol, "LONG", current_margin)
        send_telegram_msg(res_msg)
        return True, "Opened"

    elif side_type == "SHORT":
        if curr_rsi > 70:
            return False, f"RSI = {curr_rsi} (>70). Ждем опускания ниже 70."

        if current_pos == "long":
            upl = pos_info.get("upl", 0.0)
            upl_ratio = pos_info.get("uplRatio", 0.0)
            close_res = close_okx_position(inst_id, "long")
            if close_res.get("code") == "0":
                STATS_DATA["total_pnl"] += upl
                pnl_sign = "+" if upl >= 0 else ""
                send_telegram_msg(f"🔄 **Закрыт LONG по `{inst_id}`** (разворот Upthrust).\n💰 **PnL:** `{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)`")

        success, res_msg = execute_okx_trade(symbol, "SHORT", current_margin)
        send_telegram_msg(res_msg)
        return True, "Opened"

    return False, "Unknown side"

def check_and_close_positions_by_rsi():
    try:
        # 1. Проверяем отложенные сигналы в очереди входа
        global PENDING_ENTRY_SIGNALS
        still_pending = []
        for sig in PENDING_ENTRY_SIGNALS:
            success, _ = process_signal_execution(sig["symbol"], sig["side_type"])
            if not success:
                still_pending.append(sig)  # Оставляем ждать дальше
        PENDING_ENTRY_SIGNALS = still_pending

        # 2. Проверяем открытые позиции на предмет ТП и СЛ
        positions = get_okx_all_positions()
        if not positions:
            ACTIVE_TRADES_META.clear()
            return

        for pos in positions:
            inst_id = pos["instId"]
            pos_side = pos["posSide"]
            
            # Обновляем счетчик баров после входа (для защиты СЛ в первые 4 бара)
            if inst_id in ACTIVE_TRADES_META:
                ACTIVE_TRADES_META[inst_id]["bars_passed"] += 1
            else:
                ACTIVE_TRADES_META[inst_id] = {"bars_passed": 4} # Если бот перезапустился, считаем защиту уже прошедшей

            bars_passed = ACTIVE_TRADES_META[inst_id]["bars_passed"]
            is_sl_protected = (bars_passed <= 4)  то есть первые 4 бара (1 час) СЛ заблокирован

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
                # СТОП-ЛОСС (защищен в течение первых 4 баров)
                if curr_rsi < RSI_SL_LONG:
                    if is_sl_protected:
                        print(f"[{inst_id}] СЛ пробит (RSI < 30), но действует защита первого часа ({bars_passed}/4 бара).")
                    else:
                        close_res = close_okx_position(inst_id, "long")
                        if close_res.get("code") == "0":
                            STATS_DATA["sl_closed"] += 1
                            STATS_DATA["total_pnl"] += upl
                            send_telegram_msg(f"🛑 **СТОП-ЛОСС LONG по `{inst_id}`**\n• RSI < 30: **{curr_rsi}**\n💰 **PnL:** `{pnl_str}`")
                            ACTIVE_TRADES_META.pop(inst_id, None)
                
                # ТЕЙК-ПРОФИТ (Трейлинг) - работает без ограничений с первой свечи
                elif curr_rsi >= RSI_TRAIL_LONG and prev_rsi > curr_rsi:
                    close_res = close_okx_position(inst_id, "long")
                    if close_res.get("code") == "0":
                        STATS_DATA["tp_closed"] += 1
                        STATS_DATA["total_pnl"] += upl
                        send_telegram_msg(f"🎯 **Трейлинг-стоп LONG по `{inst_id}`**\n• RSI разворот вниз: **{prev_rsi} ➔ {curr_rsi}**\n💰 **PnL:** `{pnl_str}`")
                        ACTIVE_TRADES_META.pop(inst_id, None)

            elif pos_side == "short":
                # СТОП-ЛОСС (защищен в течение первых 4 баров)
                if curr_rsi > RSI_SL_SHORT:
                    if is_sl_protected:
                        print(f"[{inst_id}] СЛ пробит (RSI > 70), но действует защита первого часа ({bars_passed}/4 бара).")
                    else:
                        close_res = close_okx_position(inst_id, "short")
                        if close_res.get("code") == "0":
                            STATS_DATA["sl_closed"] += 1
                            STATS_DATA["total_pnl"] += upl
                            send_telegram_msg(f"🛑 **СТОП-ЛОСС SHORT по `{inst_id}`**\n• RSI > 70: **{curr_rsi}**\n💰 **PnL:** `{pnl_str}`")
                            ACTIVE_TRADES_META.pop(inst_id, None)

                # ТЕЙК-ПРОФИТ (Трейлинг) - работает без ограничений с первой свечи
                elif curr_rsi <= RSI_TRAIL_SHORT and prev_rsi < curr_rsi:
                    close_res = close_okx_position(inst_id, "short")
                    if close_res.get("code") == "0":
                        STATS_DATA["tp_closed"] += 1
                        STATS_DATA["total_pnl"] += upl
                        send_telegram_msg(f"🎯 **Трейлинг-стоп SHORT по `{inst_id}`**\n• RSI разворот вверх: **{prev_rsi} ➔ {curr_rsi}**\n💰 **PnL:** `{pnl_str}`")
                        ACTIVE_TRADES_META.pop(inst_id, None)

    except Exception as e:
        print(f"Ошибка проверки RSI: {e}")

def send_periodical_report():
    try:
        positions = get_okx_all_positions()
        open_count = len(positions)

        tp_count = STATS_DATA["tp_closed"]
        sl_count = STATS_DATA["sl_closed"]
        period_pnl = STATS_DATA["total_pnl"]

        pnl_sign = "+" if period_pnl >= 0 else ""
        pnl_str = f"{pnl_sign}${period_pnl:,.2f}"

        report_msg = (
            f"📊 **Периодический отчет бота**\n\n"
            f"• Открытых сделок сейчас: **{open_count}**\n"
            f"• Закрыто по ТП (трейлинг): **{tp_count}**\n"
            f"• Закрыто по СЛ: **{sl_count}**\n"
            f"• Общий PnL закрытых: `{pnl_str}`"
        )

        send_telegram_msg(report_msg)

        STATS_DATA["tp_closed"] = 0
        STATS_DATA["sl_closed"] = 0
        STATS_DATA["total_pnl"] = 0.0

    except Exception as e:
        print(f"Ошибка отчета: {e}")

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
    elif "upthrust" in text_lower:
        signal_type = "ENTRY_SHORT"
        pattern_name = "UPTHRUST"

    return symbol, signal_type, pattern_name

@app.route('/webhook', methods=['POST'])
def webhook():
    raw_data = request.get_data(as_text=True)
    symbol, signal_type, pattern_name = parse_alert(raw_data)

    if symbol == "UNKNOWN" or signal_type == "UNKNOWN":
        return "Ignored", 200

    side_type = "LONG" if signal_type == "ENTRY_LONG" else "SHORT"

    # Сразу пытаемся открыть сделку по условиям RSI. Если RSI не подходит, кидаем в очередь ожидания.
    success, msg = process_signal_execution(symbol, side_type)
    if not success:
        # Добавляем в очередь ожидания пересечения RSI
        PENDING_ENTRY_SIGNALS.append({"symbol": symbol, "side_type": side_type})
        send_telegram_msg(f"⏳ **Сигнал {pattern_name} ({symbol}) получен**. {msg}")

    return "OK", 200

@app.route('/telegram-callback', methods=['POST'])
def telegram_callback():
    data = request.get_json()

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
    return "OKX Smart Bot with RSI Filter & SL Lock is Running!", 200

scheduler = BackgroundScheduler(daemon=True)

scheduler.add_job(
    func=check_and_close_positions_by_rsi,
    trigger='cron',
    minute='0,15,30,45',
    second='3',
    id='rsi_checker_job'
)

scheduler.add_job(
    func=send_periodical_report,
    trigger='cron',
    hour='8,10,12,14,16,18,20,22',
    minute='0',
    second='0',
    timezone=ZoneInfo('Europe/Kyiv'),
    id='periodical_report_job'
)

scheduler.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
