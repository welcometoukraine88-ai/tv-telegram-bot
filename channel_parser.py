import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from flask import Flask
import requests
from telethon import events, TelegramClient
from telethon.sessions import StringSession

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# Telegram & Bot Env
API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_STRING = os.environ.get("TG_SESSION_STRING", "")

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TARGET_CHAT_ID = int(os.environ.get("TG_CHAT_ID", "0"))

# OKX API Env
OKX_API_KEY = os.environ.get("OKX_API_KEY", "").strip()
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "").strip()
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "").strip()
OKX_BASE_URL = "https://www.okx.com"

BE_CHECK_INTERVAL_SEC = 900
TRADES_STATE_FILE = "tracked_trades.json"
DEFAULT_STOP_LOSS_PCT = 10.0


# ==========================================
# НОРМАЛИЗАЦИЯ МОНЕТ И ГИБКИЙ ПАРСЕР
# ==========================================

NON_TICKER_WORDS = {
    "LONG",
    "SHORT",
    "ЛОНГ",
    "ШОРТ",
    "BUY",
    "SELL",
    "TP",
    "TP1",
    "TP2",
    "TP3",
    "TP4",
    "TP5",
    "SL",
    "STOP",
    "СТОП",
    "ЦЕЛЬ",
    "ЦЕЛИ",
    "ТЕЙК",
    "ТЕЙКИ",
    "ВХОД",
    "ТВХ",
    "РЫНОК",
    "MARKET",
    "LIMIT",
    "ЛИМИТ",
    "КРОСС",
    "CROSS",
    "ISOLATED",
    "ИЗОЛЯ",
    "ПЛЕЧО",
    "РИСК",
    "БАНК",
    "USDT",
    "USDC",
    "USD",
    "SWAP",
    "PERP",
    "SPOT",
    "СПОТ",
    "ПО",
    "НА",
    "ОТ",
    "ДО",
    "И",
    "В",
    "ЗА",
    "Х",
}

NUM_RE = r"[0-9]+(?:[.,][0-9]+)?"


def parse_num(raw: str) -> float:
  return float(raw.strip().replace(",", "."))


def normalize_symbol(raw_symbol: str) -> str:
  cleaned = (
      re.sub(r"[^A-Za-z0-9]", "", (raw_symbol or "").upper())
      .replace("USDT", "")
      .replace("USDC", "")
      .replace("SWAP", "")
      .replace("PERP", "")
  )
  if not cleaned or cleaned in NON_TICKER_WORDS:
    return ""
  return f"{cleaned}USDT"


def detect_side(text: str) -> str:
  m = re.search(
      r"(?:^|[\s\(\[\|/#*•\-–—])(LONG|SHORT|ЛОНГ|ШОРТ|BUY|SELL)(?:$|[\s\)\]\.,!?:;*•\-–—])",
      text,
      re.IGNORECASE | re.MULTILINE,
  )
  if m:
    tok = m.group(1).upper()
    if tok in ("LONG", "ЛОНГ", "BUY"):
      return "LONG"
    if tok in ("SHORT", "ШОРТ", "SELL"):
      return "SHORT"

  if re.search(r"🟢|📈|🚀|лонг\w*|покуп\w*", text, re.IGNORECASE):
    return "LONG"
  if re.search(r"🔴|📉|🔻|шорт\w*|продаж\w*", text, re.IGNORECASE):
    return "SHORT"
  return ""


def detect_symbol(text: str) -> str:
  pair_patterns = [
      r"[#\$]?\b([A-Za-z0-9]{2,15})\s*[/\-_]\s*(?:USDT|USDC|USD|PERP|SWAP)\b",
      r"[#\$]?\b([A-Za-z0-9]{2,15})(?:USDT|USDC)\b",
  ]
  for pat in pair_patterns:
    for m in re.finditer(pat, text, re.IGNORECASE):
      sym = normalize_symbol(m.group(1))
      if sym:
        return sym

  for m in re.finditer(r"[#\$]([A-Za-z0-9]{2,15})\b", text):
    sym = normalize_symbol(m.group(1))
    if sym:
      return sym

  for m in re.finditer(
      r"\b([A-Za-z0-9]{2,15})\b\s*[\|\-–—:]?\s*\b(?:LONG|SHORT|ЛОНГ|ШОРТ)\b",
      text,
      re.IGNORECASE,
  ):
    sym = normalize_symbol(m.group(1))
    if sym:
      return sym

  for m in re.finditer(
      r"\b(?:LONG|SHORT|ЛОНГ|ШОРТ)\b\s*[\|\-–—:]?\s*\b([A-Za-z0-9]{2,15})\b",
      text,
      re.IGNORECASE,
  ):
    sym = normalize_symbol(m.group(1))
    if sym:
      return sym

  head = "\n".join(text.splitlines()[:4])
  for m in re.finditer(r"\b([A-Z0-9]{2,12})\b", head):
    cand = m.group(1).upper()
    if cand.isdigit():
      continue
    sym = normalize_symbol(cand)
    if sym:
      return sym

  return ""


def extract_entry(text: str):
  entry_patterns = [
      r"(?:ТВХ|Точка\s+входа|Вход\s*(?:в\s+сделку|в\s+позицию)?|Entry)\s*[:\-–—]?\s*([^\n]+)",
  ]
  for pat in entry_patterns:
    m = re.search(pat, text, re.IGNORECASE)
    if m:
      line = m.group(1).strip()
      if re.search(
          r"по\s+рынку|рынок|с\s+рынка|market|mkt|по\s+тек\w*|current",
          line,
          re.IGNORECASE,
      ):
        return None, [], True

      nums = [
          parse_num(x)
          for x in re.findall(NUM_RE, line)
          if parse_num(x) > 0
      ]
      if nums:
        if len(nums) >= 2 and abs(nums[1] - nums[0]) / max(nums[0], 1e-9) < 0.25:
          return nums[0], nums[:2], False
        return nums[0], nums[:1], False

  return None, [], True


def default_shares_for_count(n: int, custom_shares=None) -> list:
  if n <= 0:
    return []
  if custom_shares and len(custom_shares) >= n:
    return [float(x) for x in custom_shares[:n]]
  if n == 1:
    return [100.0]
  if n == 3:
    return [50.0, 25.0, 25.0]
  equal = round(100.0 / n, 2)
  return [equal for _ in range(n)]


def extract_tps(text: str, custom_shares=None) -> list:
  tps = []

  block_match = re.search(
      r"(?:Тейк-профит\w*|Тейк\w*|Цел[ьяи]|TPs?|Таргет\w*)\s*[:\-–—]?\s*(.*?)(?=\n\s*(?:Стоп\w*|СЛ|SL\b|Риск\b|Банк\b|Плеч\w*)|\Z)",
      text,
      re.IGNORECASE | re.DOTALL,
  )
  if block_match:
    block = block_match.group(1)

    pct_items = []
    for m in re.finditer(
        rf"({NUM_RE})\s*\(\s*({NUM_RE})\s*%\s*\)",
        block,
    ):
      px = parse_num(m.group(1))
      sh = parse_num(m.group(2))
      if px > 0:
        pct_items.append({"price": px, "share_pct": sh})
    if pct_items:
      return pct_items

    prices = []
    for line in block.splitlines():
      s = line.strip()
      if not s:
        continue
      s_clean = re.sub(
          r"^\s*(?:TP\s*\d+|Т\s*\d+|Цель\s*\d+|\d+)\s*[\)\.\-:]\s*",
          "",
          s,
          flags=re.IGNORECASE,
      )
      s_clean = re.sub(r"\(\s*[0-9]+(?:[.,][0-9]+)?\s*%\s*\)", "", s_clean)
      for num_str in re.findall(NUM_RE, s_clean):
        val = parse_num(num_str)
        if val > 0:
          prices.append(val)

    if prices:
      shares = default_shares_for_count(len(prices), custom_shares)
      for i, p in enumerate(prices):
        tps.append({"price": p, "share_pct": shares[i]})
      return tps

  line_tps = []
  for m in re.finditer(
      rf"(?:TP\s*\d+|Т\s*\d+|Цель\s*\d+|Тейк\s*\d+)\s*[:\-–—]?\s*({NUM_RE})",
      text,
      re.IGNORECASE,
  ):
    val = parse_num(m.group(1))
    if val > 0:
      line_tps.append(val)

  if line_tps:
    shares = default_shares_for_count(len(line_tps), custom_shares)
    for i, p in enumerate(line_tps):
      tps.append({"price": p, "share_pct": shares[i]})

  return tps


def extract_stop(text: str):
  m = re.search(
      rf"(?:Стоп(?:-лосс)?|СЛ|SL|Stop(?:-loss)?)\s*[:\-–—]?\s*({NUM_RE})",
      text,
      re.IGNORECASE,
  )
  if m:
    val = parse_num(m.group(1))
    if val > 0:
      return val
  return None


def parse_flexible_signal(text: str, cfg: dict = None):
  if not text or not text.strip():
    return None

  clean_text = text.strip()
  symbol = detect_symbol(clean_text)
  side = detect_side(clean_text)

  if not symbol or not side:
    return None

  entry_price, entry_candidates, _ = extract_entry(clean_text)
  custom_shares = (cfg or {}).get("tp_shares")
  tps = extract_tps(clean_text, custom_shares=custom_shares)
  stop_price = extract_stop(clean_text)

  if not tps and stop_price is None and entry_price is None:
    return None

  return {
      "symbol": symbol,
      "side": side,
      "entry_price": entry_price,
      "entry_candidates": entry_candidates,
      "tps": tps,
      "stop_price": stop_price,
  }


def to_telethon_channel_id(raw_id: int) -> int:
  if raw_id < 0:
    return raw_id
  s = str(raw_id)
  if s.startswith("100") and len(s) > 10:
    return int(f"-{s}")
  return int(f"-100{s}")


# ==========================================
# РЕЕСТР КАНАЛОВ И ИХ ПРАВИЛ ТОРГОВЛИ
# ==========================================

CHANNEL_CONFIGS = {
    to_telethon_channel_id(3845170751): {
        "name": "Нестеров",
        "parser": parse_flexible_signal,
        "order_type": "limit",
        "default_sl_pct": DEFAULT_STOP_LOSS_PCT,
        "tp_shares": None,
    },
    to_telethon_channel_id(2254922481): {
        "name": "Роман Робертович",
        "parser": parse_flexible_signal,
        "order_type": "market",
        "default_sl_pct": DEFAULT_STOP_LOSS_PCT,
        "tp_shares": [100.0],
    },
    to_telethon_channel_id(1574940403): {
        "name": "AltCentr",
        "parser": parse_flexible_signal,
        "order_type": "limit",
        "default_sl_pct": DEFAULT_STOP_LOSS_PCT,
        "tp_shares": [50.0, 25.0, 25.0],
    },
    to_telethon_channel_id(1195973120): {
        "name": "Crypto Alliance (Мартин)",
        "parser": parse_flexible_signal,
        "order_type": "market",
        "default_sl_pct": DEFAULT_STOP_LOSS_PCT,
        "tp_shares": [50.0, 25.0, 25.0],
    },
    to_telethon_channel_id(1698940666): {
        "name": "Роман Златов",
        "parser": parse_flexible_signal,
        "order_type": "market",
        "default_sl_pct": DEFAULT_STOP_LOSS_PCT,
        "tp_shares": [50.0, 25.0, 25.0],
    },
    to_telethon_channel_id(2224581920): {
        "name": "GOAT TRADING",
        "parser": parse_flexible_signal,
        "order_type": "market",
        "default_sl_pct": DEFAULT_STOP_LOSS_PCT,
        "tp_shares": [100.0],
    },
}


# ==========================================
# СЕРВИСНЫЕ ФУНКЦИИ И OKX API
# ==========================================

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, loop=loop)

pending_signals = {}
tracked_trades = {}
last_be_check_ts = 0.0
last_update_id = 0
web_app = Flask(__name__)


def load_tracked_trades():
  global tracked_trades
  try:
    if os.path.exists(TRADES_STATE_FILE):
      with open(TRADES_STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        if isinstance(data, dict):
          tracked_trades = data
  except Exception as e:
    print(f"⚠️ Не удалось загрузить {TRADES_STATE_FILE}: {e}", flush=True)


def save_tracked_trades():
  try:
    with open(TRADES_STATE_FILE, "w", encoding="utf-8") as f:
      json.dump(tracked_trades, f, ensure_ascii=False, indent=2)
  except Exception as e:
    print(f"⚠️ Не удалось сохранить {TRADES_STATE_FILE}: {e}", flush=True)


@web_app.route("/")
def health():
  return "Telegram OKX Multi-Channel Executor is running", 200


def send_bot_msg(chat_id: int, text: str):
  try:
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )
    return r.json()
  except Exception as e:
    print(f"❌ Ошибка отправки в бот: {e}", flush=True)
    return None


def symbol_to_inst_id(symbol: str) -> str:
  norm = normalize_symbol(symbol)
  if not norm:
    raise RuntimeError(f"Некорректный символ монеты: {symbol}")
  base = norm[:-4]
  return f"{base}-USDT-SWAP"


def round_step(value: float, step_str: str, mode=ROUND_DOWN) -> Decimal:
  step = Decimal(str(step_str))
  if step <= 0:
    return Decimal(str(value))
  val = Decimal(str(value))
  units = (val / step).to_integral_value(rounding=mode)
  return (units * step).normalize()


def fmt_dec(d: Decimal) -> str:
  s = format(d, "f")
  if "." in s:
    s = s.rstrip("0").rstrip(".")
  return s or "0"


def okx_sign(timestamp: str, method: str, request_path: str, body: str) -> str:
  payload = f"{timestamp}{method.upper()}{request_path}{body}"
  mac = hmac.new(
      OKX_SECRET_KEY.encode("utf-8"),
      payload.encode("utf-8"),
      digestmod=hashlib.sha256,
  )
  return base64.b64encode(mac.digest()).decode("utf-8")


def okx_request(method: str, path: str, params: dict = None, data: dict = None):
  query_str = ""
  if params:
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    if qs:
      query_str = f"?{qs}"
  request_path = f"{path}{query_str}"
  body = json.dumps(data, separators=(",", ":")) if data else ""
  ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
  sign = okx_sign(ts, method, request_path, body)

  headers = {
      "OK-ACCESS-KEY": OKX_API_KEY,
      "OK-ACCESS-SIGN": sign,
      "OK-ACCESS-TIMESTAMP": ts,
      "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
      "Content-Type": "application/json",
  }

  url = f"{OKX_BASE_URL}{request_path}"
  if method.upper() == "GET":
    resp = requests.get(url, headers=headers, timeout=15)
  else:
    resp = requests.post(url, headers=headers, data=body, timeout=15)

  parsed = resp.json()
  if parsed.get("code") != "0":
    raise RuntimeError(
        f"OKX API error on {method} {path}: code={parsed.get('code')},"
        f" msg={parsed.get('msg')}, data={parsed.get('data')}"
    )
  return parsed.get("data", [])


def get_account_pos_mode() -> str:
  cfg_list = okx_request("GET", "/api/v5/account/config")
  if cfg_list and isinstance(cfg_list, list):
    return cfg_list[0].get("posMode", "net_mode")
  return "net_mode"


def get_instrument_spec(inst_id: str) -> dict:
  rows = okx_request(
      "GET",
      "/api/v5/public/instruments",
      params={"instType": "SWAP", "instId": inst_id},
  )
  if not rows:
    raise RuntimeError(f"Инструмент {inst_id} не найден на OKX SWAP")
  row = rows[0]
  return {
      "ctVal": row["ctVal"],
      "lotSz": row["lotSz"],
      "minSz": row["minSz"],
      "tickSz": row["tickSz"],
  }


def get_market_price(inst_id: str) -> float:
  rows = okx_request(
      "GET",
      "/api/v5/market/ticker",
      params={"instId": inst_id},
  )
  if not rows:
    raise RuntimeError(f"Не удалось получить текущую цену для {inst_id}")
  return float(rows[0]["last"])


def get_order_details(inst_id: str, ord_id: str) -> dict:
  rows = okx_request(
      "GET",
      "/api/v5/trade/order",
      params={"instId": inst_id, "ordId": ord_id},
  )
  return rows[0] if rows else {}


def get_algo_order_status(inst_id: str, algo_id: str) -> str:
  if not algo_id or algo_id == "—":
    return ""
  rows = okx_request(
      "GET",
      "/api/v5/trade/order-algo",
      params={"instId": inst_id, "algoId": algo_id},
  )
  if rows:
    return rows[0].get("state", "")
  hist = okx_request(
      "GET",
      "/api/v5/trade/orders-algo-history",
      params={"instType": "SWAP", "instId": inst_id, "algoId": algo_id},
  )
  if hist:
    return hist[0].get("state", "")
  return ""


def get_all_open_swap_positions() -> list:
  rows = okx_request(
      "GET",
      "/api/v5/account/positions",
      params={"instType": "SWAP"},
  )
  open_pos = []
  for r in rows or []:
    pos_val = Decimal(str(r.get("pos", "0") or "0"))
    if pos_val != 0:
      open_pos.append(r)
  return open_pos


def get_all_pending_algo_orders() -> list:
  return okx_request(
      "GET",
      "/api/v5/trade/orders-algo-pending",
      params={"instType": "SWAP"},
  )


def cancel_algo_orders(inst_id: str, algo_ids: list):
  valid_ids = [aid for aid in algo_ids if aid and aid != "—"]
  if not valid_ids:
    return
  payload = [{"instId": inst_id, "algoId": aid} for aid in valid_ids]
  okx_request("POST", "/api/v5/trade/cancel-algos", data=payload)


def place_stop_loss_algo(
    inst_id: str,
    close_side: str,
    sz: Decimal,
    sl_px: Decimal,
    is_hedge: bool,
    pos_side: str,
) -> str:
  sl_payload = {
      "instId": inst_id,
      "tdMode": "cross",
      "side": close_side,
      "ordType": "conditional",
      "sz": fmt_dec(sz),
      "slTriggerPx": fmt_dec(sl_px),
      "slOrdPx": "-1",
  }
  if is_hedge:
    sl_payload["posSide"] = pos_side
  else:
    sl_payload["reduceOnly"] = True

  sl_res = okx_request("POST", "/api/v5/trade/order-algo", data=sl_payload)
  return sl_res[0].get("algoId") if sl_res else "?"


def get_max_cross_leverage(inst_id: str, pos_mode: str, side: str) -> str:
  pos_side = (
      ("long" if side == "LONG" else "short")
      if pos_mode == "long_short_mode"
      else None
  )
  rows = okx_request(
      "GET",
      "/api/v5/account/leverage-info",
      params={"instId": inst_id, "mgnMode": "cross"},
  )
  max_lev = None
  for r in rows:
    r_pos_side = r.get("posSide") or ""
    if pos_side and r_pos_side and r_pos_side != pos_side:
      continue
    lev_str = r.get("lever")
    if lev_str:
      lev_val = float(lev_str)
      if max_lev is None or lev_val > max_lev:
        max_lev = lev_val
  if max_lev is None:
    return "20"
  return fmt_dec(Decimal(str(max_lev)).to_integral_value(rounding=ROUND_DOWN))


def set_cross_leverage(inst_id: str, lever: str, pos_mode: str, side: str):
  payload = {
      "instId": inst_id,
      "lever": str(lever),
      "mgnMode": "cross",
  }
  if pos_mode == "long_short_mode":
    payload["posSide"] = "long" if side == "LONG" else "short"
  return okx_request("POST", "/api/v5/account/set-leverage", data=payload)


def resolve_limit_entry_price(sig: dict, inst_id: str) -> float:
  candidates = sig.get("entry_candidates") or []
  if len(candidates) >= 2:
    entry_1, entry_2 = float(candidates[0]), float(candidates[1])
    mkt_px = get_market_price(inst_id)
    if sig["side"] == "LONG" and mkt_px < entry_1:
      return entry_2
    if sig["side"] == "SHORT" and mkt_px > entry_1:
      return entry_2
    return entry_1
  if candidates:
    return float(candidates[0])
  return float(sig["entry_price"])


def execute_okx_signal(sig: dict, margin_usdt: float) -> dict:
  inst_id = symbol_to_inst_id(sig["symbol"])
  side = sig["side"]
  is_long = side == "LONG"
  order_type = sig.get("order_type", "limit")

  pos_mode = get_account_pos_mode()
  is_hedge = pos_mode == "long_short_mode"
  pos_side = ("long" if is_long else "short") if is_hedge else None

  spec = get_instrument_spec(inst_id)
  max_lev = get_max_cross_leverage(inst_id, pos_mode, side)
  set_cross_leverage(inst_id, max_lev, pos_mode, side)

  lever_dec = Decimal(str(max_lev))
  margin_dec = Decimal(str(margin_usdt))

  if order_type == "market" or sig.get("entry_price") is None:
    ref_price = get_market_price(inst_id)
    entry_px_dec = round_step(ref_price, spec["tickSz"], ROUND_HALF_UP)
  else:
    chosen_entry = resolve_limit_entry_price(sig, inst_id)
    entry_px_dec = round_step(chosen_entry, spec["tickSz"], ROUND_HALF_UP)

  ct_val_dec = Decimal(str(spec["ctVal"]))
  min_sz_dec = Decimal(str(spec["minSz"]))

  if entry_px_dec <= 0 or ct_val_dec <= 0:
    raise RuntimeError("Некорректная цена входа или размер контракта")

  notional_usdt = margin_dec * lever_dec
  raw_contracts = notional_usdt / (entry_px_dec * ct_val_dec)
  total_sz = round_step(float(raw_contracts), spec["lotSz"], ROUND_DOWN)

  if total_sz < min_sz_dec:
    raise RuntimeError(
        f"Рассчитанный объём {fmt_dec(total_sz)} меньше минимума биржи"
        f" {fmt_dec(min_sz_dec)} для {inst_id}"
    )

  entry_side = "buy" if is_long else "sell"
  close_side = "sell" if is_long else "buy"

  # 1. Входной ордер (limit или market)
  entry_payload = {
      "instId": inst_id,
      "tdMode": "cross",
      "side": entry_side,
      "ordType": order_type,
      "sz": fmt_dec(total_sz),
  }
  if order_type == "limit":
    entry_payload["px"] = fmt_dec(entry_px_dec)
  if is_hedge:
    entry_payload["posSide"] = pos_side

  entry_res = okx_request("POST", "/api/v5/trade/order", data=entry_payload)
  entry_ord_id = entry_res[0].get("ordId") if entry_res else "?"

  actual_entry_px_dec = entry_px_dec
  if order_type == "market" and entry_ord_id and entry_ord_id != "?":
    try:
      time.sleep(0.4)
      ord_info = get_order_details(inst_id, entry_ord_id)
      avg_px = ord_info.get("avgPx")
      if avg_px and float(avg_px) > 0:
        actual_entry_px_dec = round_step(
            float(avg_px), spec["tickSz"], ROUND_HALF_UP
        )
    except Exception:
      pass

  # 2. Тейк-профит (один или лесенка), если заданы в сигнале
  tp_results = []
  tps = sig.get("tps") or []
  if tps:
    allocated_sz = Decimal("0")
    total_share = sum(Decimal(str(t["share_pct"])) for t in tps)

    for idx, tp in enumerate(tps):
      tp_px_dec = round_step(tp["price"], spec["tickSz"], ROUND_HALF_UP)
      if idx < len(tps) - 1:
        if total_share > 0:
          share_ratio = Decimal(str(tp["share_pct"])) / total_share
        else:
          share_ratio = Decimal(str(tp["share_pct"])) / Decimal("100")
        tp_sz = round_step(
            float(total_sz * share_ratio), spec["lotSz"], ROUND_DOWN
        )
        if tp_sz > total_sz - allocated_sz:
          tp_sz = round_step(
              float(total_sz - allocated_sz), spec["lotSz"], ROUND_DOWN
          )
        allocated_sz += tp_sz
      else:
        tp_sz = round_step(
            float(total_sz - allocated_sz), spec["lotSz"], ROUND_DOWN
        )

      if tp_sz <= 0:
        continue

      tp_payload = {
          "instId": inst_id,
          "tdMode": "cross",
          "side": close_side,
          "ordType": "conditional",
          "sz": fmt_dec(tp_sz),
          "tpTriggerPx": fmt_dec(tp_px_dec),
          "tpOrdPx": "-1",
      }
      if is_hedge:
        tp_payload["posSide"] = pos_side
      else:
        tp_payload["reduceOnly"] = True

      tp_res = okx_request("POST", "/api/v5/trade/order-algo", data=tp_payload)
      algo_id = tp_res[0].get("algoId") if tp_res else "?"
      tp_results.append({
          "tp": idx + 1,
          "px": fmt_dec(tp_px_dec),
          "sz": fmt_dec(tp_sz),
          "algoId": algo_id,
      })

  # 3. Стоп-лосс (SL): из сигнала или расчётный от default_sl_pct (по умолчанию 10%)
  raw_stop = sig.get("stop_price")
  if raw_stop is None:
    sl_pct = float(sig.get("default_sl_pct") or DEFAULT_STOP_LOSS_PCT)
    sl_mult = (1.0 - sl_pct / 100.0) if is_long else (1.0 + sl_pct / 100.0)
    raw_stop = float(actual_entry_px_dec) * sl_mult

  sl_px_str = "—"
  sl_algo_id = "—"
  if raw_stop is not None and raw_stop > 0:
    sl_px_dec = round_step(raw_stop, spec["tickSz"], ROUND_HALF_UP)
    sl_algo_id = place_stop_loss_algo(
        inst_id=inst_id,
        close_side=close_side,
        sz=total_sz,
        sl_px=sl_px_dec,
        is_hedge=is_hedge,
        pos_side=pos_side,
    )
    sl_px_str = fmt_dec(sl_px_dec)

  if tp_results:
    trade_key = f"{inst_id}:{pos_side or side}"
    tracked_trades[trade_key] = {
        "inst_id": inst_id,
        "symbol": sig["symbol"],
        "source_name": sig.get("source_name", ""),
        "side": side,
        "entry_px": fmt_dec(actual_entry_px_dec),
        "tp1_px": tp_results[0]["px"],
        "tp1_algo_id": tp_results[0]["algoId"],
        "sl_algo_id": sl_algo_id,
        "be_moved": False,
    }
    save_tracked_trades()

  return {
      "inst_id": inst_id,
      "order_type": order_type,
      "max_lev": max_lev,
      "entry_px": fmt_dec(actual_entry_px_dec),
      "total_sz": fmt_dec(total_sz),
      "entry_ord_id": entry_ord_id,
      "tps": tp_results,
      "sl_px": sl_px_str,
      "sl_algo_id": sl_algo_id,
  }


def check_open_positions_breakeven():
  global last_be_check_ts
  now = time.time()
  if now - last_be_check_ts < BE_CHECK_INTERVAL_SEC:
    return
  last_be_check_ts = now

  try:
    open_positions = get_all_open_swap_positions()
    if not open_positions:
      return

    pending_algos = get_all_pending_algo_orders()

    for pos_row in open_positions:
      inst_id = pos_row.get("instId", "")
      if not inst_id:
        continue

      raw_pos = Decimal(str(pos_row.get("pos", "0") or "0"))
      if raw_pos == 0:
        continue

      pos_side_raw = (pos_row.get("posSide") or "net").lower()
      if pos_side_raw == "long":
        is_long = True
        is_hedge = True
        pos_side = "long"
      elif pos_side_raw == "short":
        is_long = False
        is_hedge = True
        pos_side = "short"
      else:
        is_long = raw_pos > 0
        is_hedge = False
        pos_side = None

      close_side = "sell" if is_long else "buy"
      abs_pos = abs(raw_pos)

      avg_px_str = pos_row.get("avgPx") or "0"
      avg_px_val = Decimal(str(avg_px_str))
      if avg_px_val <= 0:
        continue

      trade_key = f"{inst_id}:{pos_side or ('LONG' if is_long else 'SHORT')}"
      tr = tracked_trades.get(trade_key, {})
      if tr.get("be_moved"):
        continue

      spec = get_instrument_spec(inst_id)
      min_sz_dec = Decimal(str(spec["minSz"]))
      new_sl_sz = round_step(float(abs_pos), spec["lotSz"], ROUND_DOWN)
      if new_sl_sz < min_sz_dec:
        continue

      be_px_dec = round_step(float(avg_px_val), spec["tickSz"], ROUND_HALF_UP)

      tp1_reached = False
      tp1_algo_id = tr.get("tp1_algo_id")
      if tp1_algo_id and tp1_algo_id != "—":
        if get_algo_order_status(inst_id, tp1_algo_id) == "effective":
          tp1_reached = True

      if not tp1_reached and tr.get("tp1_px"):
        tp1_px_val = Decimal(str(tr["tp1_px"]))
        mkt_px = Decimal(str(get_market_price(inst_id)))
        if is_long and mkt_px >= tp1_px_val:
          tp1_reached = True
        elif (not is_long) and mkt_px <= tp1_px_val:
          tp1_reached = True

      if not tp1_reached:
        continue

      matching_sl_ids = []
      already_at_be = False
      for a in pending_algos:
        if a.get("instId") != inst_id:
          continue
        a_pos_side = (a.get("posSide") or "").lower()
        if is_hedge and a_pos_side and a_pos_side != pos_side:
          continue
        sl_trig = a.get("slTriggerPx") or ""
        if sl_trig:
          matching_sl_ids.append(a.get("algoId"))
          try:
            trig_dec = round_step(float(sl_trig), spec["tickSz"], ROUND_HALF_UP)
            if trig_dec == be_px_dec:
              already_at_be = True
          except Exception:
            pass

      if already_at_be:
        tracked_trades[trade_key] = {**tr, "be_moved": True}
        save_tracked_trades()
        continue

      cancel_algo_orders(inst_id, matching_sl_ids)
      new_sl_algo_id = place_stop_loss_algo(
          inst_id=inst_id,
          close_side=close_side,
          sz=new_sl_sz,
          sl_px=be_px_dec,
          is_hedge=is_hedge,
          pos_side=pos_side,
      )

      tracked_trades[trade_key] = {
          **tr,
          "inst_id": inst_id,
          "entry_px": fmt_dec(be_px_dec),
          "sl_algo_id": new_sl_algo_id,
          "be_moved": True,
      }
      save_tracked_trades()

      src_line = (
          f"• Источник: *{tr['source_name']}*\n" if tr.get("source_name") else ""
      )
      send_bot_msg(
          TARGET_CHAT_ID,
          f"🛡 *Стоп перенесён в безубыток (`{inst_id}`)*\n"
          f"{src_line}"
          f"• TP1 достигнут — SL перенесён на вход `{fmt_dec(be_px_dec)}`\n"
          f"• Объём SL: `{fmt_dec(new_sl_sz)}` контр. (`{new_sl_algo_id}`)",
      )
      print(
          f"🛡 [15m check] {inst_id}: TP1 достигнут, SL перенесён на"
          f" {fmt_dec(be_px_dec)}",
          flush=True,
      )
  except Exception as e:
    print(f"⚠️ Ошибка периодической проверки безубытка (15m): {e}", flush=True)


def format_signal_card(sig: dict) -> str:
  source_name = sig.get("source_name", "Неизвестный канал")
  order_type = sig.get("order_type", "limit")
  candidates = sig.get("entry_candidates") or []

  if order_type == "market":
    entry_label = "По рынку (MARKET)"
  elif len(candidates) >= 2:
    entry_label = (
        f"лимит `{candidates[0]}` (или `{candidates[1]}` при пробое по рынку)"
    )
  elif sig.get("entry_price") is not None:
    entry_label = f"`{sig['entry_price']}` (лимит)"
  else:
    entry_label = "По рынку (MARKET)"

  tps = sig.get("tps") or []
  if len(tps) == 1:
    tp_block = f"• Тейк: `{tps[0]['price']}` ({tps[0]['share_pct']}%)"
  elif len(tps) > 1:
    tp_block = "• Тейки лесенкой:\n" + "\n".join(
        f"  • TP{i+1}: `{tp['price']}` ({tp['share_pct']}%)"
        for i, tp in enumerate(tps)
    )
  else:
    tp_block = "• Тейки: не заданы"

  if sig.get("stop_price") is not None:
    sl_label = f"`{sig['stop_price']}`"
  else:
    sl_pct = sig.get("default_sl_pct") or DEFAULT_STOP_LOSS_PCT
    sl_label = f"авто `{sl_pct}%` от входа"

  emoji = "🟢" if sig["side"] == "LONG" else "🔴"
  return (
      f"{emoji} *Новый сигнал: `{sig['symbol']}` — {sig['side']}*\n"
      f"• Источник: *{source_name}*\n"
      f"• Режим: `CROSS` | Плечо: `MAX` (авто)\n"
      f"• Вход: {entry_label}\n"
      f"{tp_block}\n"
      f"• Стоп-лосс: {sl_label}\n\n"
      "✍️ Введите сумму маржи в *USDT* (например, `15`) или напишите `skip` для"
      " отмены:"
  )


def poll_bot_replies():
  global last_update_id
  while True:
    try:
      check_open_positions_breakeven()
      r = requests.get(
          f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
          params={"offset": last_update_id + 1, "timeout": 15},
          timeout=20,
      )
      data = r.json()
      if data.get("ok"):
        for upd in data.get("result", []):
          last_update_id = upd["update_id"]
          msg = upd.get("message")
          if not msg:
            continue

          chat_id = msg["chat"]["id"]
          user_id = msg["from"]["id"]
          text = (msg.get("text") or "").strip()

          if chat_id != TARGET_CHAT_ID or user_id not in pending_signals:
            continue

          if text.lower() in ("skip", "отмена", "пропустить"):
            skipped = pending_signals.pop(user_id)
            send_bot_msg(
                chat_id,
                f"⏭ Сигнал по `{skipped['symbol']}`"
                f" ({skipped.get('source_name', '')}) пропущен.",
            )
            continue

          try:
            margin = float(text.replace(",", "."))
            if margin <= 0:
              raise ValueError()
          except ValueError:
            send_bot_msg(
                chat_id,
                "⚠️ Введите положительное число (например, `10` или `25.5`) или"
                " `skip`.",
            )
            continue

          sig = pending_signals.pop(user_id)
          print(
              f"⚡ Отправляем на OKX [{sig.get('source_name')}]: {sig} | Маржа:"
              f" {margin} USDT",
              flush=True,
          )
          try:
            res = execute_okx_signal(sig, margin)
            tp_summary = (
                "\n".join(
                    f"  • TP{t['tp']}: `{t['px']}` | `{t['sz']}` контр."
                    f" (`{t['algoId']}`)"
                    for t in res["tps"]
                )
                if res["tps"]
                else "  • без TP"
            )
            send_bot_msg(
                chat_id,
                f"✅ *Ордера выставлены на OKX (`{res['inst_id']}`)*\n"
                f"• Источник: *{sig.get('source_name')}*\n"
                f"• Сторона: `{sig['side']}` (`CROSS`, плечо `{res['max_lev']}x`)\n"
                f"• Вход ({res['order_type'].upper()}): `{res['entry_px']}` |"
                f" Объём: `{res['total_sz']}` контр. (`{res['entry_ord_id']}`)\n"
                f"• TP:\n{tp_summary}\n"
                f"• SL: `{res['sl_px']}` | `{res['total_sz']}` контр."
                f" (`{res['sl_algo_id']}`)",
            )
            print(f"✅ Успешно выставлено на OKX: {res}", flush=True)
          except Exception as okx_err:
            print(f"❌ Ошибка OKX: {okx_err}", flush=True)
            send_bot_msg(
                chat_id,
                f"❌ *Ошибка при выставлении на OKX:*\n`{okx_err}`",
            )
    except Exception as e:
      print(f"⚠️ Ошибка polling бота: {e}", flush=True)
      time.sleep(2)


def run_web():
  port = int(os.environ.get("PORT", "10000"))
  web_app.run(host="0.0.0.0", port=port, use_reloader=False)


@client.on(events.NewMessage(chats=list(CHANNEL_CONFIGS.keys())))
async def on_channel_post(event):
  cfg = CHANNEL_CONFIGS.get(event.chat_id)
  if not cfg:
    return

  raw_text = event.raw_text or ""
  parser_fn = cfg.get("parser") or parse_flexible_signal
  try:
    sig = parser_fn(raw_text, cfg)
  except TypeError:
    sig = parser_fn(raw_text)

  if not sig:
    if raw_text.strip():
      preview = raw_text.strip().replace("\n", " | ")[:220]
      print(
          f"ℹ️ Пост в [{cfg['name']}] не распознан как сигнал: {preview}",
          flush=True,
      )
    return

  sig["source_id"] = event.chat_id
  sig["source_name"] = cfg["name"]
  sig["order_type"] = (
      "market" if sig.get("entry_price") is None else cfg.get("order_type", "limit")
  )
  sig["default_sl_pct"] = cfg.get("default_sl_pct", DEFAULT_STOP_LOSS_PCT)

  print(f"📩 Спарсен сигнал [{cfg['name']}]: {sig}", flush=True)
  pending_signals[TARGET_CHAT_ID] = sig
  send_bot_msg(TARGET_CHAT_ID, format_signal_card(sig))


async def start_services():
  await client.connect()
  if not await client.is_user_authorized():
    print("❌ Сессия НЕ авторизована! Проверь TG_SESSION_STRING.", flush=True)
    return

  me = await client.get_me()
  channels_str = ", ".join(
      f"{cfg['name']} ({cid})" for cid, cfg in CHANNEL_CONFIGS.items()
  )
  print(f"✅ Авторизованы как: {me.first_name} (ID: {me.id})", flush=True)
  print(f"🚀 Слушатель запущен. Каналы: {channels_str}", flush=True)
  await client.run_until_disconnected()


if __name__ == "__main__":
  required = [
      API_ID,
      API_HASH,
      SESSION_STRING,
      BOT_TOKEN,
      TARGET_CHAT_ID,
      OKX_API_KEY,
      OKX_SECRET_KEY,
      OKX_PASSPHRASE,
  ]
  if not all(required) or not CHANNEL_CONFIGS:
    raise SystemExit(
        "❌ Заполните переменные окружения (Telegram + OKX) и CHANNEL_CONFIGS!"
    )

  load_tracked_trades()
  threading.Thread(target=run_web, daemon=True).start()
  threading.Thread(target=poll_bot_replies, daemon=True).start()

  try:
    loop.run_until_complete(start_services())
  except Exception as e:
    print(f"💥 Ошибка в start_services: {e}", flush=True)
