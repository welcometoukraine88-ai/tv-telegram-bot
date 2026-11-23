import asyncio
import os
import re
import threading
import time
import requests
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_STRING = os.environ.get("TG_SESSION_STRING", "")

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TARGET_CHAT_ID = int(os.environ.get("TG_CHAT_ID", "0"))
RAW_SOURCE = os.environ.get("SOURCE_CHANNEL", "").strip().lstrip("@")


def resolve_source_channel():
  if not RAW_SOURCE:
    return None
  if re.fullmatch(r"-?\d+", RAW_SOURCE):
    return int(RAW_SOURCE)
  return RAW_SOURCE


SOURCE_CHANNEL = resolve_source_channel()
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, loop=loop)

pending_signals = {}
last_update_id = 0
web_app = Flask(__name__)


@web_app.route("/")
def health():
  return "OK", 200


def send_bot_msg(chat_id: int, text: str):
  try:
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )
  except Exception as e:
    print(f"❌ Bot send error: {e}", flush=True)


def parse_signal_post(text: str):
  if not text:
    return None
  header_match = re.search(
      r"\b([A-Z0-9_]+USDT)\s*\|\s*(LONG|SHORT)\b", text, re.IGNORECASE
  )
  if not header_match:
    return None
  symbol = header_match.group(1).upper()
  side = header_match.group(2).upper()

  tbx_match = re.search(r"ТВХ\s*:\s*([0-9]+(?:[.,][0-9]+)?)", text, re.IGNORECASE)
  if not tbx_match:
    return None
  entry_price = float(tbx_match.group(1).replace(",", "."))

  tp_section_match = re.search(
      r"Тейк-профит\s*:\s*(.*?)(?=\n\s*Стоп\s*:|\Z)",
      text,
      re.IGNORECASE | re.DOTALL,
  )
  tps = []
  if tp_section_match:
    for m in re.finditer(
        r"([0-9]+(?:[.,][0-9]+)?)\s*\(\s*([0-9]+(?:[.,][0-9]+)?)\s*%\s*\)",
        tp_section_match.group(1),
    ):
      tps.append({
          "price": float(m.group(1).replace(",", ".")),
          "share_pct": float(m.group(2).replace(",", ".")),
      })

  sl_match = re.search(r"Стоп\s*:\s*([0-9]+(?:[.,][0-9]+)?)", text, re.IGNORECASE)
  stop_price = float(sl_match.group(1).replace(",", ".")) if sl_match else None

  if not tps or stop_price is None:
    return None

  return {
      "symbol": symbol,
      "side": side,
      "entry_price": entry_price,
      "tps": tps,
      "stop_price": stop_price,
  }


def format_signal_card(sig: dict) -> str:
  tp_lines = "\n".join(
      f"  • TP{i+1}: `{tp['price']}` ({tp['share_pct']}%)"
      for i, tp in enumerate(sig["tps"])
  )
  emoji = "🟢" if sig["side"] == "LONG" else "🔴"
  return (
      f"{emoji} *Новый сигнал: `{sig['symbol']}` — {sig['side']}*\n"
      f"• Плечо OKX: `MAX` (авто)\n"
      f"• ТВХ (лимит): `{sig['entry_price']}`\n"
      f"• Тейки лесенкой:\n{tp_lines}\n"
      f"• Стоп-лосс: `{sig['stop_price']}`\n\n"
      "✍️ Введите сумму маржи в *USDT* (например, `15`) или напишите `skip` для"
      " отмены:"
  )


def poll_bot_replies():
  global last_update_id
  while True:
    try:
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
            send_bot_msg(chat_id, f"⏭ Сигнал по `{skipped['symbol']}` пропущен.")
            continue
          try:
            margin = float(text.replace(",", "."))
            if margin <= 0:
              raise ValueError()
          except ValueError:
            send_bot_msg(
                chat_id, "⚠️ Введите число (например, `15`) или `skip`."
            )
            continue
          sig = pending_signals.pop(user_id)
          print(f"✅ ПОДТВЕРЖДЁН: {sig} | Маржа: {margin}", flush=True)
          send_bot_msg(
              chat_id,
              "🧪 *[ТЕСТ]* Принято:\n"
              f"• `{sig['symbol']}` ({sig['side']})\n"
              f"• Маржа: `{margin} USDT` | Плечо: `MAX`\n"
              f"• ТВХ: `{sig['entry_price']}` | СЛ: `{sig['stop_price']}`",
          )
    except Exception as e:
      print(f"⚠️ Polling error: {e}", flush=True)
      time.sleep(2)


def run_web():
  port = int(os.environ.get("PORT", "10000"))
  web_app.run(host="0.0.0.0", port=port, use_reloader=False)


@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def on_channel_post(event):
  sig = parse_signal_post(event.raw_text or "")
  if not sig:
    return
  print(f"📩 Сигнал: {sig}", flush=True)
  pending_signals[TARGET_CHAT_ID] = sig
  send_bot_msg(TARGET_CHAT_ID, format_signal_card(sig))


async def main_async():
  print("1. Вызов client.connect()...", flush=True)
  await asyncio.wait_for(client.connect(), timeout=15)
  print("2. connect() завершён, проверка авторизации...", flush=True)
  auth = await asyncio.wait_for(client.is_user_authorized(), timeout=10)
  print(f"3. is_user_authorized = {auth}", flush=True)
  if not auth:
    print("❌ Сессия не авторизована!", flush=True)
    return
  me = await client.get_me()
  print(f"4. Аккаунт: {me.first_name} ({me.id})", flush=True)
  print("5. Диалоги:", flush=True)
  async for d in client.iter_dialogs(limit=30):
    if d.is_channel or d.is_group:
      print(f"   - {d.name} | ID: {d.id}", flush=True)
  print(f"6. Слушаем {SOURCE_CHANNEL}...", flush=True)
  await client.run_until_disconnected()


if __name__ == "__main__":
  if not all([
      API_ID,
      API_HASH,
      SESSION_STRING,
      BOT_TOKEN,
      TARGET_CHAT_ID,
      SOURCE_CHANNEL,
  ]):
    raise SystemExit("❌ Не заданы переменные окружения!")
  threading.Thread(target=run_web, daemon=True).start()
  threading.Thread(target=poll_bot_replies, daemon=True).start()
  print("0. Старт главного цикла...", flush=True)
  try:
    loop.run_until_complete(main_async())
  except Exception as e:
    print(f"💥 Ошибка: {type(e).__name__}: {e}", flush=True)
