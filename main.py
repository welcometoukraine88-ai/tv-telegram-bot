import hashlib
import hmac
import base64
import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request

app = FastAPI(title="OKX Turtle Soup Webhook")

OKX_BASE_URL = "https://www.okx.com"

OKX_API_KEY = os.getenv("OKX_API_KEY", "").strip()
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "").strip()
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # опциональный токен проверки

# Размер маржи в USDT на одну сделку (меняешь в Environment Variables на Render)
MARGIN_USDT = float(os.getenv("MARGIN_USDT", "50"))

# Опционально: верхний потолок плеча, если не хочешь упираться в биржевой максимум
MAX_LEVERAGE_CAP = os.getenv("MAX_LEVERAGE_CAP", "").strip()

# Режим торговли на OKX: "0" = обычный аккаунт, "1" = демо-торговля
OKX_SIMULATED = os.getenv("OKX_SIMULATED", "0").strip()


def okx_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sign_request(timestamp: str, method: str, request_path: str, body: str) -> str:
    message = f"{timestamp}{method.upper()}{request_path}{body}"
    mac = hmac.new(
        OKX_SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


def okx_headers(method: str, request_path: str, body: str = "") -> dict[str, str]:
    ts = okx_timestamp()
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign_request(ts, method, request_path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
    }
    if OKX_SIMULATED == "1":
        headers["x-simulated-trading"] = "1"
    return headers


def normalize_inst_id(raw_coin: str) -> str:
    c = raw_coin.strip().upper()
    if c.endswith("-SWAP"):
        return c
    if c.endswith(".P"):
        c = c[:-2]
    if "-" in c:
        parts = c.split("-")
        return f"{parts[0]}-{parts[1]}-SWAP"
    if c.endswith("USDT"):
        base = c[:-4]
        return f"{base}-USDT-SWAP"
    if c.endswith("USDC"):
        base = c[:-4]
        return f"{base}-USDC-SWAP"
    return f"{c}-USDT-SWAP"


def parse_signal_payload(raw_text: str) -> dict:
    """
    Поддерживает формат:
    Coin: EWZUSDT.P | Side: LONG | TP: 34.07 | SL: 32.74
    или JSON с полями coin/symbol, side/direction, tp, sl.
    """
    text = raw_text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            coin = str(data.get("coin") or data.get("symbol") or data.get("ticker") or "").strip()
            side = str(data.get("side") or data.get("direction") or "").strip().upper()
            tp = float(data["tp"])
            sl = float(data["sl"])
            if coin and side in ("LONG", "SHORT", "BUY", "SELL"):
                norm_side = "LONG" if side in ("LONG", "BUY") else "SHORT"
                return {"coin": coin, "side": norm_side, "tp": tp, "sl": sl}
        except Exception:
            pass

    coin_match = re.search(r"Coin\s*:\s*([A-Za-z0-9._-]+)", text, re.IGNORECASE)
    side_match = re.search(r"Side\s*:\s*(LONG|SHORT|BUY|SELL)", text, re.IGNORECASE)
    tp_match = re.search(r"TP\s*:\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    sl_match = re.search(r"SL\s*:\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)

    if not (coin_match and side_match and tp_match and sl_match):
        raise ValueError(f"Cannot parse signal: {text}")

    raw_side = side_match.group(1).upper()
    side = "LONG" if raw_side in ("LONG", "BUY") else "SHORT"

    return {
        "coin": coin_match.group(1).strip(),
        "side": side,
        "tp": float(tp_match.group(1)),
        "sl": float(sl_match.group(1)),
    }


def round_step_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    steps = math.floor((value + 1e-12) / step)
    rounded = steps * step
    # Убираем артефакты float вроде 1.2000000000000002
    decimals = max(0, len(str(step).split(".")[1].rstrip("0")) if "." in str(step) else 0)
    return round(rounded, decimals)


def format_by_tick(price: float, tick_str: str) -> str:
    tick = float(tick_str)
    if tick <= 0:
        return str(price)
    steps = round(price / tick)
    val = steps * tick
    decimals = max(0, len(tick_str.split(".")[1].rstrip("0")) if "." in tick_str else 0)
    return f"{val:.{decimals}f}"


async def get_instrument_info(client: httpx.AsyncClient, inst_id: str) -> dict:
    path = f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}"
    r = await client.get(f"{OKX_BASE_URL}{path}", timeout=10.0)
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != "0" or not payload.get("data"):
        raise HTTPException(status_code=400, detail=f"Instrument not found on OKX: {inst_id} ({payload})")
    return payload["data"][0]


async def get_last_price(client: httpx.AsyncClient, inst_id: str) -> float:
    path = f"/api/v5/market/ticker?instId={inst_id}"
    r = await client.get(f"{OKX_BASE_URL}{path}", timeout=10.0)
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != "0" or not payload.get("data"):
        raise HTTPException(status_code=400, detail=f"Ticker error for {inst_id}: {payload}")
    return float(payload["data"][0]["last"])


async def set_max_cross_leverage(client: httpx.AsyncClient, inst_id: str, max_inst_lever: int) -> int:
    target_lever = max_inst_lever
    if MAX_LEVERAGE_CAP:
        target_lever = min(target_lever, int(float(MAX_LEVERAGE_CAP)))

    # Пробуем установить максимальное кросс-плечо; если биржа вернёт ошибку лимита позиции —
    # уменьшаем плечо шагом вниз до рабочего значения.
    for lev in range(target_lever, 0, -1):
        req_path = "/api/v5/account/set-leverage"
        body_obj = {
            "instId": inst_id,
            "lever": str(lev),
            "mgnMode": "cross",
        }
        body_str = json.dumps(body_obj, separators=(",", ":"))
        headers = okx_headers("POST", req_path, body_str)
        r = await client.post(f"{OKX_BASE_URL}{req_path}", content=body_str, headers=headers, timeout=10.0)
        r.raise_for_status()
        res = r.json()
        if res.get("code") == "0":
            return lev
        msg = str(res.get("msg", "")).lower()
        if "leverage" not in msg and "exceed" not in msg and "max" not in msg:
            raise HTTPException(status_code=400, detail=f"set-leverage failed: {res}")

    raise HTTPException(status_code=400, detail=f"Could not set cross leverage for {inst_id}")


async def place_cross_order_with_tpsl(
    client: httpx.AsyncClient,
    inst_id: str,
    side: str,
    sz_str: str,
    tp_str: str,
    sl_str: str,
) -> dict:
    order_side = "buy" if side == "LONG" else "sell"
    req_path = "/api/v5/trade/order"
    body_obj = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": order_side,
        "ordType": "market",
        "sz": sz_str,
        "attachAlgoOrds": [
            {
                "tpTriggerPx": tp_str,
                "tpOrdPx": "-1",
                "slTriggerPx": sl_str,
                "slOrdPx": "-1",
                "tpTriggerPxType": "last",
                "slTriggerPxType": "last",
            }
        ],
    }
    body_str = json.dumps(body_obj, separators=(",", ":"))
    headers = okx_headers("POST", req_path, body_str)
    r = await client.post(f"{OKX_BASE_URL}{req_path}", content=body_str, headers=headers, timeout=10.0)
    r.raise_for_status()
    res = r.json()
    if res.get("code") != "0":
        raise HTTPException(status_code=400, detail=f"Order failed: {res}")
    return res


@app.get("/health")
async def health():
    return {"status": "ok", "margin_usdt": MARGIN_USDT}


@app.post("/webhook")
async def webhook(request: Request, token: Optional[str] = None):
    if WEBHOOK_SECRET and token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    if not (OKX_API_KEY and OKX_SECRET_KEY and OKX_PASSPHRASE):
        raise HTTPException(status_code=500, detail="OKX API credentials are not configured")

    raw_bytes = await request.body()
    raw_text = raw_bytes.decode("utf-8", errors="replace").strip()

    try:
        sig = parse_signal_payload(raw_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    inst_id = normalize_inst_id(sig["coin"])
    margin = float(os.getenv("MARGIN_USDT", str(MARGIN_USDT)))

    async with httpx.AsyncClient() as client:
        inst = await get_instrument_info(client, inst_id)
        price = await get_last_price(client, inst_id)

        max_lev = int(float(inst.get("lever", "20")))
        applied_lev = await set_max_cross_leverage(client, inst_id, max_lev)

        ct_val = float(inst.get("ctVal", "1"))
        lot_sz = float(inst.get("lotSz", "1"))
        min_sz = float(inst.get("minSz", str(lot_sz)))
        tick_sz = inst.get("tickSz", "0.01")

        notional_usdt = margin * applied_lev
        raw_contracts = notional_usdt / (price * ct_val)
        contracts = round_step_down(raw_contracts, lot_sz)

        if contracts < min_sz:
            raise HTTPException(
                status_code=400,
                detail=f"Calculated size {contracts} < minSz {min_sz} for {inst_id} (margin={margin}, lev={applied_lev}, price={price})",
            )

        # Форматируем размер без научной нотации
        lot_decimals = max(0, len(str(lot_sz).split(".")[1].rstrip("0")) if "." in str(lot_sz) else 0)
        sz_str = f"{contracts:.{lot_decimals}f}" if lot_decimals > 0 else str(int(contracts))

        tp_str = format_by_tick(sig["tp"], tick_sz)
        sl_str = format_by_tick(sig["sl"], tick_sz)

        order_res = await place_cross_order_with_tpsl(
            client=client,
            inst_id=inst_id,
            side=sig["side"],
            sz_str=sz_str,
            tp_str=tp_str,
            sl_str=sl_str,
        )

    return {
        "status": "filled_request_sent",
        "instId": inst_id,
        "side": sig["side"],
        "margin_usdt": margin,
        "leverage": applied_lev,
        "contracts": sz_str,
        "tp": tp_str,
        "sl": sl_str,
        "okx": order_res,
    }
