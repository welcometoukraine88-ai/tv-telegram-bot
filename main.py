import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request

OKX_BASE_URL = "https://www.okx.com"

OKX_API_KEY = os.getenv("OKX_API_KEY", "").strip()
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "").strip()
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_INTERVAL_SECONDS = 300  # каждые 5 минут

# Состояние активных позиций, открытых через вебхук:
# inst_id -> {side, tp, sl, entry_px, margin_usdt, leverage, opened_at_ms}
tracked_positions: dict[str, dict] = {}
state_lock = asyncio.Lock()


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
    text = raw_text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            coin = str(data.get("coin") or data.get("symbol") or data.get("ticker") or "").strip()
            side_raw = str(data.get("side") or data.get("direction") or "").strip().upper()
            tp = float(data["tp"])
            sl = float(data["sl"])
            if coin and side_raw in ("LONG", "SHORT", "BUY", "SELL"):
                side = "LONG" if side_raw in ("LONG", "BUY") else "SHORT"
                return {"coin": coin, "side": side, "tp": tp, "sl": sl}
        except Exception:
            pass

    coin_match = re.search(r"Coin\s*:\s*([A-Za-z0-9._-]+)", text, re.IGNORECASE)
    side_match = re.search(r"Side\s*:\s*(LONG|SHORT|BUY|SELL)", text, re.IGNORECASE)
    tp_match = re.search(r"TP\s*:\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    sl_match = re.search(r"SL\s*:\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)

    if not (coin_match and side_match and tp_match and sl_match):
        raise ValueError(f"Cannot parse signal: {text}")

    side_raw = side_match.group(1).upper()
    side = "LONG" if side_raw in ("LONG", "BUY") else "SHORT"

    return {
        "coin": coin_match.group(1).strip(),
        "side": side,
        "tp": float(tp_match.group(1)),
        "sl": float(sl_match.group(1)),
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
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
    }
    try:
        r = await client.post(url, json=payload, timeout=10.0)
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


async def fetch_open_swap_positions(client: httpx.AsyncClient) -> dict[str, dict]:
    req_path = "/api/v5/account/positions?instType=SWAP"
    r = await client.get(
        f"{OKX_BASE_URL}{req_path}",
        headers=okx_headers("GET", req_path, ""),
        timeout=10.0,
    )
    r.raise_for_status()
    res = r.json()
    if res.get("code") != "0":
        raise RuntimeError(f"positions error: {res}")

    open_map: dict[str, dict] = {}
    for pos in res.get("data", []):
        pos_sz = abs(float(pos.get("pos", "0") or "0"))
        if pos_sz > 0:
            open_map[pos["instId"]] = pos
    return open_map


async def fetch_closed_position_record(
    client: httpx.AsyncClient,
    inst_id: str,
    opened_at_ms: int,
) -> Optional[dict]:
    req_path = f"/api/v5/account/positions-history?instType=SWAP&instId={inst_id}&limit=20"
    r = await client.get(
        f"{OKX_BASE_URL}{req_path}",
        headers=okx_headers("GET", req_path, ""),
        timeout=10.0,
    )
    r.raise_for_status()
    res = r.json()
    if res.get("code") != "0":
        return None

    for item in res.get("data", []):
        u_time = int(item.get("uTime") or item.get("cTime") or "0")
        if u_time >= opened_at_ms - 5000:
            return item
    return res["data"][0] if res.get("data") else None


def classify_exit_and_metrics(pos_meta: dict, closed_rec: Optional[dict], fallback_px: float) -> tuple[str, float, float]:
    side = pos_meta["side"]
    entry_px = pos_meta["entry_px"]
    tp_px = pos_meta["tp"]
    sl_px = pos_meta["sl"]

    exit_px = fallback_px
    realized_pnl = 0.0

    if closed_rec:
        raw_exit = closed_rec.get("closeAvgPx") or closed_rec.get("avgPx")
        if raw_exit:
            try:
                val = float(raw_exit)
                if val > 0:
                    exit_px = val
            except ValueError:
                pass

        for field in ("realizedPnl", "pnl", "settledPnl"):
            if closed_rec.get(field) not in (None, ""):
                try:
                    realized_pnl = float(closed_rec[field])
                    break
                except ValueError:
                    pass

    if side == "LONG":
        price_move_pct = ((exit_px - entry_px) / entry_px) * 100.0 if entry_px > 0 else 0.0
    else:
        price_move_pct = ((entry_px - exit_px) / entry_px) * 100.0 if entry_px > 0 else
