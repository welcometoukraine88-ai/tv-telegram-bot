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

# НАСТРОЙКИ ВЫХОДА ПО RSI(14)
RSI_TIMEFRAME = "15m"      # Таймфрейм свечей
RSI_LONG_EXIT = 70.0       # Закрывать LONG, если RSI >= 70
RSI_SHORT_EXIT = 30.0      # Закрывать SHORT, если RSI <= 30

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

def calculate_rsi14(inst_id, timeframe=RSI_TIMEFRAME):
    res = okx_request("GET", f"/api/v5/market/candles?instType=SWAP&instId={inst_id}&bar={timeframe}&limit=50")
    if res.get("code") != "0" or not res.get("data"):
        return None

    raw_candles = res["data"]
    closed_candles = [c for c in raw_candles if c[8] == "1"]
    
    if len(closed_candles) < 15:
        closed_candles = raw_candles[1:]

    candles = list(reversed(closed_candles))
    closes = [float(c[4]) for c in candles]

    if len(closes) < 15:
        return None

    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(abs(diff))
            losses.append(abs(diff))

    avg_gain = sum(gains[:14]) / 14.0
    avg_loss = sum(losses[:14]) / 14.0

    for i in range(14, len(gains)):
        avg_gain = (avg_gain * 13.0 + gains[i]) / 14.0
        avg_loss = (avg_loss * 13.0 + losses[i]) / 14.0

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)

def check_and_close_positions_by_rsi():
    try:
        positions = get_okx_all_positions()
        if not positions:
            return

        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Проверка RSI по закрытию 15м свечи...")

        for pos in positions:
            inst_id = pos["instId"]
            pos_side = pos["posSide"]
            rsi = calculate_rsi14(inst_id)

            if rsi is None:
                continue

            upl = pos.get("upl", 0.0)
            upl_ratio = pos.get("uplRatio", 0.0)
            pnl_sign = "+" if upl >= 0 else ""
            pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"

            if pos_side == "long" and rsi >= RSI_LONG_EXIT:
                close_res = close_okx_position(inst_id, "long")
                if close_res.get("code") == "0":
                    msg = (
                        f"🎯 **Авто-закрытие LONG по `{inst_id}`**\n"
                        f"• Закрытая свеча 15m RSI(14): **{rsi}** (порог >= {RSI_LONG_EXIT})\n"
                        f"💰 **PnL:** `{pnl_str}`"
                    )
                    send_telegram_msg(msg)

            elif pos_side == "short" and rsi <= RSI_SHORT_EXIT:
                close_res = close_okx_position(inst_id, "short")
                if close_res.get("code") == "0":
                    msg = (
                        f"🎯 **Авто-закрытие SHORT по `{inst_id}`**\n"
                        f"• Закрытая свеча 15m RSI(14): **{rsi}** (порог <= {RSI_SHORT_EXIT})\n"
                        f"💰 **PnL:** `{pnl_str}`"
                    )
                    send_telegram_msg(msg)

    except Exception as e:
        print(f"Ошибка проверки RSI: {e}")

# ================= РАСЧЕТ И ИСПОЛНЕНИЕ ОРДЕРА =================

def execute_okx_trade(symbol, side_type, margin_usdt):
    """Открывает сделку с максимальным плечом и 5% стоп-лоссом"""
    clean_symbol = symbol.replace(".P", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"
    okx_side = "sell" if side_type == "SHORT" else "buy"
    pos_side = "short" if side_type == "SHORT" else "long"

    max_lev = 20
    last_price = 0.0
    ct_val = 1.0
    lot_sz = 1.0
    tick_sz_str = "0.01"

    try:
        inst_res = okx_request("GET", f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}")
        if inst_res.get("code") == "0" and inst_res.get("data"):
            inst_data = inst_res["data"][0]
            max_lev = int(inst_data.get("lever", 20))
            ct_val = float(inst_data.get("ctVal", 1.0))
            lot_sz = float(inst_data.get("lotSz", 1.0))
            tick_sz_str = str(inst_data.get("tickSz", "0.01"))

        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={inst_id}")
        if ticker_res.get("code") == "0" and ticker_res.get("data"):
            last_price = float(ticker_res["data"][0].get("last", 0.0))

    except Exception as e:
        print(f"Ошибка информации об инструменте {inst_id}: {e}")

    if last_price <= 0:
        return False, f"Не удалось получить цену для `{inst_id}`."

    # Устанавливаем МАКСИМАЛЬНОЕ плечо монеты на OKX перед ордером
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

    # РАСЧЕТ ТОЧНОСТИ ЦЕНЫ ДЛЯ СТОП-ЛОССА С УЧЕТОМ TICK_SZ ОКХ
    tick_sz_float = float(tick_sz_str)
    if '.' in tick_sz_str:
        precision = len(tick_sz_str.split('.')[1].rstrip('0'))
    else:
        precision = 0

    if side_type == "LONG":
        raw_sl = last_price * 0.95
    else:
        raw_sl = last_price * 1.05

    # Округление цены до кратности tickSz
    sl_price = math.floor(raw_sl / tick_sz_float) * tick_sz_float if side_type == "LONG" else math.ceil(raw_sl / tick_sz_float) * tick_sz_float

    if precision > 0:
        sl_price_str = f"{sl_price:.{precision}f}"
    else:
        sl_price_str = str(int(sl_price))

    order_body = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": okx_side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": formatted_sz,
        "attachAlgoOrds": [
            {
                "slTriggerPx": sl_price_str,
                "slOrdPx": "-1",
                "slTriggerPxType": "last"
            }
        ]
    }

    res = okx_request("POST", "/api/v5/trade/order", order_body)

    if res.get("code") == "0":
        return True, (
            f"✅ **Ордер открыт!**\n\n"
            f"• Инструмент: `{inst_id}`\n"
            f"• Направление: **{side_type}**\n"
            f"• Плечо: **{max_lev}x**\n"
            f"• Маржа: **~${round(actual_margin_used, 2)} USDT**\n"
            f"• Объем: **~${round(actual_notional_usdt, 2)} USDT**\n"
            f"• 🛑 **Стоп-лосс (5%): `${sl_price_str}`**"
        )
    else:
        msg = res.get("data", [{}])[0].get("sMsg") or res.get("msg")
        return False, f"Ошибка OKX: {msg}"

# ================= TELEGRAM & WEBHOOKS =================

def send_telegram_signal(symbol, signal_type, is_averaging=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    emoji = "🔴" if signal_type == "SHORT" else "🟢"
    action_title = "УСРЕДНЕНИЕ (ДОБОР)" if is_averaging else "СИГНАЛ (RETEST)"

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
    symbol = "UNKNOWN"
    signal_type = "UNKNOWN"

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            symbol = data.get("instrument") or data.get("symbol") or data.get("ticker") or "UNKNOWN"
            action = str(data.get("action") or data.get("signal") or "").lower()
            if "buy level retest" in action:
                signal_type = "ENTRY_LONG"
            elif "sell level retest" in action:
                signal_type = "ENTRY_SHORT"
    except Exception:
        pass

    if symbol == "UNKNOWN":
        match = re.search(r'([A-Z0-9]+USDT(?:\.P)?)', text)
        if match:
            symbol = match.group(1)

    text_lower = text.lower()
    if signal_type == "UNKNOWN":
        if "buy level retest" in text_lower:
            signal_type = "ENTRY_LONG"
        elif "sell level retest" in text_lower:
            signal_type = "ENTRY_SHORT"

    return symbol, signal_type

@app.route('/webhook', methods=['POST'])
def webhook():
    raw_data = request.get_data(as_text=True)
    symbol, signal_type = parse_alert(raw_data)

    if symbol == "UNKNOWN" or signal_type == "UNKNOWN":
        return "Ignored", 200

    clean_symbol = symbol.replace(".P", "").replace("USDT", "")
    inst_id = f"{clean_symbol}-USDT-SWAP"

    positions = get_okx_all_positions()
    pos_info = next((p for p in positions if p["instId"] == inst_id), None)
    current_pos = pos_info["posSide"] if pos_info else None

    if signal_type == "ENTRY_LONG":
        if current_pos == "short":
            upl = pos_info.get("upl", 0.0)
            upl_ratio = pos_info.get("uplRatio", 0.0)
            close_res = close_okx_position(inst_id, "short")
            if close_res.get("code") == "0":
                pnl_sign = "+" if upl >= 0 else ""
                pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"
                msg = (
                    f"🔄 **Закрыт SHORT по `{inst_id}`** (получен противоположный Buy level retest).\n"
                    f"💰 **PnL:** `{pnl_str}`"
                )
                send_telegram_msg(msg)

        is_avg = (current_pos == "long")
        send_telegram_signal(symbol, "LONG", is_averaging=is_avg)

    elif signal_type == "ENTRY_SHORT":
        if current_pos == "long":
            upl = pos_info.get("upl", 0.0)
            upl_ratio = pos_info.get("uplRatio", 0.0)
            close_res = close_okx_position(inst_id, "long")
            if close_res.get("code") == "0":
                pnl_sign = "+" if upl >= 0 else ""
                pnl_str = f"{pnl_sign}${upl:,.2f} ({pnl_sign}{upl_ratio:.2f}%)"
                msg = (
                    f"🔄 **Закрыт LONG по `{inst_id}`** (получен противоположный Sell level retest).\n"
                    f"💰 **PnL:** `{pnl_str}`"
                )
                send_telegram_msg(msg)

        is_avg = (current_pos == "short")
        send_telegram_signal(symbol, "SHORT", is_averaging=is_avg)

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
    return "OKX Signal Bot is Running!", 200

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
