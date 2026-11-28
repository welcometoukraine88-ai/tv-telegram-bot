import base64
import hashlib
import hmac
import json
import math
import os
import re
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request

OKX_BASE_URL = "https://www.okx.com"

OKX_API_KEY = os.getenv("OKX_API_KEY", "").strip()
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "").strip()
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def get_margin_usdt() -> float:
    val = float(os.getenv("MARGIN_USDT", "1"))
    if val <= 0:
        raise ValueError("MARGIN_USDT must be > 0")
    return val


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
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign_request(ts, method, request_path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
    }


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
        return f"{c[:-4]}-USDT-SWAP"
    if c.endswith("USDC"):
        return f"{c[:-4]}-USDC-SWAP"
    return f"{c}-USDT-SWAP"


def parse_signal_payload(raw_text: str) -> dict:
    cleaned = raw_text.strip().strip('"').strip("'").strip()
    cleaned_flat = re.sub(r"[\r\n]+", " ", cleaned).strip()

    parts = [p.strip() for p in cleaned_flat.split("|") if p.strip()]
    kv: dict[str, str] = {}
    for part in parts:
        if "=" in part:
            k, v = part.split("=", 1)
            kv[k.strip().upper()] = v.strip().strip('"').strip("'")
        elif ":" in part:
            k, v = part.split(":", 1)
            kv[k.strip().upper()] = v.strip().strip('"').strip("'")

    coin = kv.get("COIN") or kv.get("SYMBOL") or kv.get("TICKER")
    side_raw = (kv.get("SIDE") or kv.get("DIRECTION") or "").upper()
    tp_raw = kv.get("TP")
    sl_raw = kv.get("SL")

    if not (coin and side_raw and tp_raw and sl_raw) and cleaned.startswith("{"):
        try:
            data = json.loads(cleaned)
            coin = str(data.get("coin") or data.get("symbol") or data.get("ticker") or "").strip()
            side_raw = str(data.get("side") or data.get("direction") or "").strip().upper()
            tp_raw = str(data.get("tp", ""))
            sl_raw = str(data.get("sl", ""))
        except Exception:
            pass

    if side_raw in ("LONG", "BUY"):
        side = "LONG"
    elif side_raw in ("SHORT", "SELL"):
        side = "SHORT"
    else:
        raise ValueError(f"Invalid or missing SIDE in payload: {raw_text!r}")

    if not coin:
        raise ValueError(f"Missing COIN in payload: {raw_text!r}")

    try:
        tp_val = float(re.sub(r"[^0-9.]", "", str(tp_raw)))
        sl_val = float(re.sub(r"[^0-9.]", "", str(sl_raw)))
    except Exception as exc:
        raise ValueError(f"Invalid TP/SL in payload: {raw_text!r}") from exc

    return {
        "coin": coin,
        "side": side,
        "tp": tp_val,
        "sl": sl_val,
    }


def decimals_from_step(step_str: str) -> int:
    s = str(step_str).strip()
    if "." not in s:
        return 0
    return max(0, len(s.split(".")[1].rstrip("0")))


def round_step_down(value: float, step: float, decimals: int) -> float:
    if step <= 0:
        return value
    steps = math.floor((value + 1e-12) / step)
    return round(steps * step, decimals)


def format_by_tick(price: float, tick_str: str) -> str:
    tick = float(tick_str)
    if tick <= 0:
        return str(price)
    decimals = decimals_from_step(tick_str)
    val = round(price / tick) * tick
    return f"{val:.{decimals}f}"


async def send_tg_message(client: httpx.AsyncClient, text: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10.0)
        if r.status_code >= 400:
            print(f"[TG ERROR] {r.status_code} {r.text}")
    except Exception as exc:
        print(f"[TG EXCEPTION] {exc}")


async def get_instrument_info(client: httpx.AsyncClient, inst_id: str) -> dict:
    path = f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}"
    r = await client.get(f"{OKX_BASE_URL}{path}", timeout=10.0)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0" or not data.get("data"):
        raise HTTPException(status_code=400, detail=f"Instrument not found: {inst_id} ({data})")
    return data["data"][0]


async def get_last_price(client: httpx.AsyncClient, inst_id: str) -> float:
    path = f"/api/v5/market/ticker?instId={inst_id}"
    r = await client.get(f"{OKX_BASE_URL}{path}", timeout=10.0)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0" or not data.get("data"):
        raise HTTPException(status_code=400, detail=f"Ticker error: {inst_id} ({data})")
    return float(data["data"][0]["last"])


async def set_max_cross_leverage(client: httpx.AsyncClient, inst_id: str, max_inst_lever: int) -> int:
    for lev in range(max_inst_lever, 0, -1):
        req_path = "/api/v5/account/set-leverage"
        body_str = json.dumps(
            {"instId": inst_id, "lever": str(lev), "mgnMode": "cross"},
            separators=(",", ":"),
        )
        r = await client.post(
            f"{OKX_BASE_URL}{req_path}",
            content=body_str,
            headers=okx_headers("POST", req_path, body_str),
            timeout=10.0,
        )
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
    req_path = "/api/v5/trade/order"
    body_obj = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": "buy" if side == "LONG" else "sell",
        "posSide": "long" if side == "LONG" else "short",
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
    r = await client.post(
        f"{OKX_BASE_URL}{req_path}",
        content=body_str,
        headers=okx_headers("POST", req_path, body_str),
        timeout=10.0,
    )
    r.raise_for_status()
    res = r.json()
    if res.get("code") != "0":
        raise HTTPException(status_code=400, detail=f"Order failed: {res}")
    return res


app = FastAPI(title="OKX Cross Webhook")


@app.post("/webhook")
async def webhook(request: Request):
    if not (OKX_API_KEY and OKX_SECRET_KEY and OKX_PASSPHRASE):
        raise HTTPException(status_code=500, detail="OKX credentials are not configured")

    raw_bytes = await request.body()
    raw_text = raw_bytes.decode("utf-8", errors="replace").strip()
    print(f"[WEBHOOK RAW] {raw_text!r}")

    try:
        sig = parse_signal_payload(raw_text)
        margin = get_margin_usdt()
        inst_id = normalize_inst_id(sig["coin"])

        async with httpx.AsyncClient() as client:
            inst = await get_instrument_info(client, inst_id)
            price = await get_last_price(client, inst_id)
            tick_sz = inst.get("tickSz", "0.01")
            entry_str = format_by_tick(price, tick_sz)

            max_lev = int(float(inst.get("lever", "20")))
            applied_lev = await set_max_cross_leverage(client, inst_id, max_lev)

            ct_val = float(inst.get("ctVal", "1"))
            lot_sz = float(inst.get("lotSz", "1"))
            min_sz = float(inst.get("minSz", str(lot_sz)))

            lot_decimals = decimals_from_step(str(lot_sz))
            notional_usdt = margin * applied_lev
            raw_contracts = notional_usdt / (price * ct_val)
            contracts = round_step_down(raw_contracts, lot_sz, lot_decimals)

            if contracts < min_sz:
                raise HTTPException(
                    status_code=400,
                    detail=f"Size {contracts} < minSz {min_sz} for {inst_id}",
                )

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

            dot = "🟢" if sig["side"] == "LONG" else "🔴"
            open_msg = (
                f"{dot} Вход: {sig['side']}\n"
                f"Монета: {inst_id}\n"
                f"Маржа: {margin:.2f} USDT | Плечо: {applied_lev}x\n"
                f"Вход: {entry_str} | TP: {tp_str} | SL: {sl_str}"
            )
            await send_tg_message(client, open_msg)

        return {
            "status": "ok",
            "instId": inst_id,
            "side": sig["side"],
            "entry": entry_str,
            "margin_usdt": margin,
            "leverage": applied_lev,
            "contracts": sz_str,
            "tp": tp_str,
            "sl": sl_str,
            "okx": order_res,
        }
    except HTTPException as exc:
        print(f"[WEBHOOK ERROR] {exc.status_code} {exc.detail}")
        raise
    except Exception as exc:
        print(f"[WEBHOOK ERROR] {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=400, detail=str(exc))
