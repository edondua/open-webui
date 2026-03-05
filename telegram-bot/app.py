from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict

import httpx
from fastapi import FastAPI, Request
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("telegram-bot")

# ── Config ──────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENWEBUI_API_URL = os.getenv("OPENWEBUI_API_URL", "").rstrip("/")
OPENWEBUI_API_KEY = os.getenv("OPENWEBUI_API_KEY", "")
OPENWEBUI_MODEL = os.getenv("OPENWEBUI_MODEL", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "50"))
ALLOWED_USERS_RAW = os.getenv("ALLOWED_TELEGRAM_USERS", "")
ALLOWED_USERS: set[int] = set()
if ALLOWED_USERS_RAW.strip():
    ALLOWED_USERS = {int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip()}

EDIT_INTERVAL = 1.5  # seconds between progressive message edits
TELEGRAM_MSG_LIMIT = 4096

# ── State ───────────────────────────────────────────────────────────
histories: dict[int, list[dict]] = defaultdict(list)

# ── FastAPI (webhook receiver) ──────────────────────────────────────
web = FastAPI(title="Telegram Bot", version="1.0.0")
tg_app: Application | None = None


def _check_config() -> str | None:
    if not TELEGRAM_BOT_TOKEN:
        return "TELEGRAM_BOT_TOKEN is not set"
    if not OPENWEBUI_API_URL:
        return "OPENWEBUI_API_URL is not set"
    if not OPENWEBUI_API_KEY:
        return "OPENWEBUI_API_KEY is not set"
    if not OPENWEBUI_MODEL:
        return "OPENWEBUI_MODEL is not set"
    return None


def _is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))


async def _call_openwebui(messages: list[dict]) -> str:
    """Call Open WebUI chat completions with streaming and automatic retry on 429."""
    headers = {
        "Authorization": f"Bearer {OPENWEBUI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENWEBUI_MODEL,
        "messages": messages,
        "stream": True,
    }

    for attempt in range(MAX_RETRIES):
        full_text = ""
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream(
                "POST",
                f"{OPENWEBUI_API_URL}/api/chat/completions",
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("retry-after", 2 ** attempt * 3))
                    log.warning("Rate limited (429), retrying in %ss (attempt %s/%s)", retry_after, attempt + 1, MAX_RETRIES)
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status_code >= 400:
                    body = await resp.aread()
                    log.error("Open WebUI error %s: %s", resp.status_code, body[:500])
                    return f"Error from AI backend ({resp.status_code}). Please try again."

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            full_text += content
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue

        return full_text.strip() or "No response from the model."

    return "The AI backend is busy right now. Please try again in a minute."


async def _send_long(update: Update, text: str) -> None:
    """Send a message, splitting if it exceeds Telegram's 4096 char limit."""
    for i in range(0, len(text), TELEGRAM_MSG_LIMIT):
        chunk = text[i : i + TELEGRAM_MSG_LIMIT]
        await update.message.reply_text(chunk)


# ── Telegram Handlers ───────────────────────────────────────────────
async def cmd_start(update: Update, _) -> None:
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        await update.message.reply_text("You are not authorised to use this bot.")
        return
    await update.message.reply_text(
        "Hello! I'm your AI assistant connected to Open WebUI.\n\n"
        "Just send me a message and I'll respond using all available tools.\n\n"
        "Commands:\n"
        "/clear - Reset conversation history\n"
        "/status - Check connection status"
    )


async def cmd_clear(update: Update, _) -> None:
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return
    histories[user_id] = []
    await update.message.reply_text("Conversation history cleared.")


async def cmd_status(update: Update, _) -> None:
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return
    err = _check_config()
    if err:
        await update.message.reply_text(f"Config issue: {err}")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{OPENWEBUI_API_URL}/health",
                headers={"Authorization": f"Bearer {OPENWEBUI_API_KEY}"},
            )
        if resp.status_code == 200:
            await update.message.reply_text(
                f"Connected to Open WebUI\n"
                f"Model: {OPENWEBUI_MODEL}\n"
                f"History: {len(histories[user_id])} messages"
            )
        else:
            await update.message.reply_text(f"Open WebUI returned status {resp.status_code}")
    except Exception as e:
        await update.message.reply_text(f"Cannot reach Open WebUI: {e}")


async def handle_message(update: Update, _) -> None:
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        await update.message.reply_text("You are not authorised to use this bot.")
        return

    err = _check_config()
    if err:
        await update.message.reply_text(f"Bot misconfigured: {err}")
        return

    user_text = update.message.text
    if not user_text:
        return

    # Add user message to history
    histories[user_id].append({"role": "user", "content": user_text})

    # Trim history
    if len(histories[user_id]) > MAX_HISTORY:
        histories[user_id] = histories[user_id][-MAX_HISTORY:]

    # Show typing indicator
    await update.message.chat.send_action("typing")

    # Call Open WebUI
    try:
        reply = await _call_openwebui(histories[user_id])
    except Exception as e:
        log.exception("Error calling Open WebUI")
        reply = f"Error: {e}"

    # Add assistant reply to history
    histories[user_id].append({"role": "assistant", "content": reply})

    # Send response
    await _send_long(update, reply)


# ── App Lifecycle ───────────────────────────────────────────────────
@web.on_event("startup")
async def startup() -> None:
    global tg_app
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set, bot will not start")
        return

    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    tg_app = builder.build()

    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("clear", cmd_clear))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await tg_app.initialize()

    if WEBHOOK_URL:
        webhook_path = f"{WEBHOOK_URL}/webhook"
        await tg_app.bot.set_webhook(url=webhook_path)
        log.info("Webhook set: %s", webhook_path)
    else:
        # Long-polling mode for local dev
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        log.info("Bot started in polling mode")


@web.on_event("shutdown")
async def shutdown() -> None:
    if tg_app:
        if not WEBHOOK_URL:
            await tg_app.updater.stop()
            await tg_app.stop()
        await tg_app.shutdown()


@web.post("/webhook")
async def telegram_webhook(request: Request) -> dict:
    if not tg_app:
        return {"ok": False, "error": "Bot not initialized"}
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


@web.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "bot_configured": bool(TELEGRAM_BOT_TOKEN),
        "openwebui_configured": bool(OPENWEBUI_API_URL and OPENWEBUI_API_KEY),
        "model": OPENWEBUI_MODEL,
        "webhook_mode": bool(WEBHOOK_URL),
        "allowed_users": len(ALLOWED_USERS) if ALLOWED_USERS else "all",
    }
