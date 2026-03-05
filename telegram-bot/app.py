from __future__ import annotations

import asyncio
import json
import logging
import os
import time
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
KEEPALIVE_INTERVAL = 8.0  # seconds between "still working" edits when no new tokens
TELEGRAM_MSG_LIMIT = 4096
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
STREAM_TIMEOUT = int(os.getenv("STREAM_TIMEOUT", "600"))  # 10 min for tool-heavy calls

# ── State ───────────────────────────────────────────────────────────
histories: dict[int, list[dict]] = defaultdict(list)
_processing: set[int] = set()  # user IDs currently being processed

# ── FastAPI (webhook receiver) ──────────────────────────────────────
web = FastAPI(title="Telegram Bot", version="1.1.0")
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


# ── Streaming call to Open WebUI ────────────────────────────────────

async def _stream_openwebui(messages: list[dict], chat_id: int, bot: Bot) -> str:
    """Call Open WebUI with streaming. Progressively edit a Telegram message.

    Handles long tool-use responses by showing keepalive updates so the user
    knows the bot is still working even when no new tokens are arriving.
    """
    headers = {
        "Authorization": f"Bearer {OPENWEBUI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENWEBUI_MODEL,
        "messages": messages,
        "stream": True,
    }

    placeholder = await bot.send_message(chat_id=chat_id, text="Thinking...")

    for attempt in range(MAX_RETRIES):
        full_text = ""
        last_edit = asyncio.get_event_loop().time()
        last_edit_text = ""
        last_token_time = asyncio.get_event_loop().time()
        keepalive_dots = 0

        timeout_config = httpx.Timeout(
            connect=30.0,
            read=STREAM_TIMEOUT,  # long read timeout for tool calls
            write=30.0,
            pool=30.0,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                async with client.stream(
                    "POST",
                    f"{OPENWEBUI_API_URL}/api/chat/completions",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("retry-after", 2 ** attempt * 3))
                        log.warning("Rate limited (429), retrying in %ss (attempt %s/%s)", retry_after, attempt + 1, MAX_RETRIES)
                        await _safe_edit(placeholder, f"Rate limited, retrying in {retry_after}s...")
                        await asyncio.sleep(retry_after)
                        continue

                    if resp.status_code >= 400:
                        body = await resp.aread()
                        log.error("Open WebUI error %s: %s", resp.status_code, body[:500])
                        error_msg = f"Error from AI backend ({resp.status_code}). Please try again."
                        await _safe_edit(placeholder, error_msg)
                        return error_msg

                    # Use aiter_lines with a keepalive wrapper so we can
                    # update the placeholder even when the AI is using tools
                    # and no tokens are streaming yet.
                    async for line in _iter_lines_with_keepalive(
                        resp, placeholder, full_text, last_edit, last_edit_text
                    ):
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
                                last_token_time = asyncio.get_event_loop().time()
                        except (json.JSONDecodeError, IndexError, KeyError):
                            continue

                        # Progressively update the message
                        now = asyncio.get_event_loop().time()
                        preview = full_text[:TELEGRAM_MSG_LIMIT - 4]
                        if now - last_edit >= EDIT_INTERVAL and preview != last_edit_text:
                            await _safe_edit(placeholder, preview + " ...")
                            last_edit = now
                            last_edit_text = preview

        except httpx.ReadTimeout:
            log.error("Stream read timeout after %ss", STREAM_TIMEOUT)
            if full_text.strip():
                # We got partial content — use it
                break
            error_msg = "The AI is taking too long. Please try a simpler question or /clear and retry."
            await _safe_edit(placeholder, error_msg)
            return error_msg
        except Exception as e:
            log.exception("Stream error: %s", e)
            if full_text.strip():
                break
            error_msg = f"Connection error: {type(e).__name__}. Please try again."
            await _safe_edit(placeholder, error_msg)
            return error_msg

        # Final edit with complete text
        final = full_text.strip() or "No response from the model."
        await _send_final(placeholder, final, chat_id, bot)
        return final

    error_msg = "The AI backend is busy right now. Please try again in a minute."
    await _safe_edit(placeholder, error_msg)
    return error_msg


async def _iter_lines_with_keepalive(resp, placeholder, full_text_ref, last_edit_ref, last_edit_text_ref):
    """Wrap aiter_lines and periodically update placeholder during long pauses.

    When the AI is calling tools (code search, Linear, etc.), there can be
    long gaps with no SSE data. This sends keepalive edits so the user knows
    the bot is still working.
    """
    phases = ["Thinking", "Researching", "Analyzing", "Processing"]
    phase_idx = 0
    last_keepalive = asyncio.get_event_loop().time()

    buffer = b""
    async for raw_bytes in resp.aiter_bytes():
        buffer += raw_bytes
        # Check for keepalive update
        now = asyncio.get_event_loop().time()
        if now - last_keepalive >= KEEPALIVE_INTERVAL:
            phase_label = phases[phase_idx % len(phases)]
            dots = "." * ((phase_idx // len(phases)) % 4 + 1)
            try:
                await placeholder.edit_text(f"{phase_label}{dots}")
            except Exception:
                pass
            last_keepalive = now
            phase_idx += 1

        # Yield complete lines
        while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line:
                yield line

    # Yield remaining
    if buffer.strip():
        yield buffer.decode("utf-8", errors="replace").strip()


async def _safe_edit(message, text: str) -> None:
    """Edit message text, silently ignoring Telegram errors."""
    try:
        await message.edit_text(text[:TELEGRAM_MSG_LIMIT])
    except Exception:
        pass


async def _send_final(placeholder, final: str, chat_id: int, bot: Bot) -> None:
    """Send the final complete response, splitting if needed."""
    if len(final) <= TELEGRAM_MSG_LIMIT:
        await _safe_edit(placeholder, final)
    else:
        await _safe_edit(placeholder, final[:TELEGRAM_MSG_LIMIT])
        for i in range(TELEGRAM_MSG_LIMIT, len(final), TELEGRAM_MSG_LIMIT):
            await bot.send_message(chat_id=chat_id, text=final[i : i + TELEGRAM_MSG_LIMIT])


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
    _processing.discard(user_id)
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
            processing = "yes (working on it)" if user_id in _processing else "no"
            await update.message.reply_text(
                f"Connected to Open WebUI\n"
                f"Model: {OPENWEBUI_MODEL}\n"
                f"History: {len(histories[user_id])} messages\n"
                f"Processing: {processing}"
            )
        else:
            await update.message.reply_text(f"Open WebUI returned status {resp.status_code}")
    except Exception as e:
        await update.message.reply_text(f"Cannot reach Open WebUI: {e}")


async def _typing_loop(chat_id: int, bot: Bot, stop_event: asyncio.Event) -> None:
    """Send 'typing...' indicator every 4s until stop_event is set."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            break  # event was set
        except asyncio.TimeoutError:
            pass  # send another typing action


async def _process_message(user_id: int, chat_id: int, bot: Bot, messages: list[dict]) -> None:
    """Background task: call Open WebUI and update history."""
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(chat_id, bot, stop_typing))

    try:
        _processing.add(user_id)
        reply = await _stream_openwebui(messages, chat_id, bot)
    except Exception as e:
        log.exception("Error calling Open WebUI")
        reply = f"Error: {e}"
        try:
            await bot.send_message(chat_id=chat_id, text=reply)
        except Exception:
            pass
    finally:
        stop_typing.set()
        typing_task.cancel()
        _processing.discard(user_id)

    histories[user_id].append({"role": "assistant", "content": reply})


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

    # If already processing a message for this user, queue acknowledgement
    if user_id in _processing:
        await update.message.reply_text(
            "I'm still working on your previous request. Please wait for it to finish, or /clear to cancel."
        )
        return

    # Add user message to history
    histories[user_id].append({"role": "user", "content": user_text})

    # Trim history
    if len(histories[user_id]) > MAX_HISTORY:
        histories[user_id] = histories[user_id][-MAX_HISTORY:]

    # Process in background so webhook returns immediately
    asyncio.create_task(
        _process_message(user_id, update.message.chat_id, tg_app.bot, list(histories[user_id]))
    )


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
    """Receive Telegram updates. Returns immediately; processing happens in background."""
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
        "active_tasks": len(_processing),
    }
