from __future__ import annotations

import asyncio
import logging
import os
import re
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
OPENWEBUI_TOOL_IDS_RAW = os.getenv("OPENWEBUI_TOOL_IDS", "")
OPENWEBUI_TOOL_IDS = [
    tid.strip() for tid in OPENWEBUI_TOOL_IDS_RAW.split(",") if tid.strip()
]
LINEAR_DEFAULT_TEAM_ID = os.getenv("LINEAR_DEFAULT_TEAM_ID", "").strip()
ALLOWED_USERS_RAW = os.getenv("ALLOWED_TELEGRAM_USERS", "")
ALLOWED_USERS: set[int] = set()
if ALLOWED_USERS_RAW.strip():
    ALLOWED_USERS = {int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip()}

TELEGRAM_MSG_LIMIT = 4096
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "600"))  # 10 min for tool-heavy calls
MAX_TOOL_NUDGES = int(os.getenv("MAX_TOOL_NUDGES", "2"))

# System prompt injected into every conversation so the AI gives
# direct, high-quality answers instead of filler/planning text.
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", (
    "You are SourceMind, an AI assistant with access to tools for code search, "
    "analytics (UXCam, Tableau), project management (Linear), and memory. "
    "Rules:\n"
    "- Use your tools to get real data before answering. Never say 'I will check' "
    "without actually doing it in the same response.\n"
    "- Give direct, actionable answers with concrete data and specifics.\n"
    "- Keep responses concise — this is Telegram, not a document.\n"
    "- Use short paragraphs, bullet points, and bold for key info.\n"
    "- If a tool call fails, say what went wrong clearly.\n"
    "- When creating Linear tasks, always call the Linear tools directly — "
    "never generate code snippets or API examples."
))

# ── State ───────────────────────────────────────────────────────────
histories: dict[int, list[dict]] = defaultdict(list)
_processing: set[int] = set()  # user IDs currently being processed

# ── FastAPI (webhook receiver) ──────────────────────────────────────
web = FastAPI(title="Telegram Bot", version="2.0.0")
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


def _extract_content(data: dict) -> str:
    """Extract response content from OpenAI-compatible payloads."""
    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Some backends return a structured array of content blocks.
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content or "")


_DEFERRAL_PATTERNS = (
    r"\bi('?| wi)ll (check|fetch|look|review|analy[sz]e)\b",
    r"\blet me (check|fetch|look|review)\b",
    r"\bfirst[, ]+(getting|checking|fetching)\b",
    r"\bchecking (the|for)\b",
)

DEFERRAL_FAILURE_MSG = (
    "I could not complete tool execution for this request, so I don't have reliable final numbers yet. "
    "Please retry in a moment."
)
LINEAR_TASK_FAILURE_MSG = (
    "I could not finish creating Linear tasks in this run. "
    "No confirmed issue IDs/URLs were returned. Please retry."
)


def _looks_like_deferral(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in _DEFERRAL_PATTERNS)


def _has_linear_task_artifacts(text: str) -> bool:
    """Check if the response contains concrete Linear issue output."""
    if re.search(r"https://linear\.app/[^\s)]+", text):
        return True
    if re.search(r"\b[A-Z]{2,10}-\d+\b", text):
        return True
    return False


def _looks_like_incomplete_linear_task_reply(text: str) -> bool:
    lowered = text.lower()
    soft_progress_markers = (
        "proceeding to create",
        "calling linear tools",
        "one moment",
        "i'll create",
        "i will create",
        "creating ",
    )
    if any(marker in lowered for marker in soft_progress_markers):
        return not _has_linear_task_artifacts(text)
    return False


def _is_linear_task_request(messages: list[dict]) -> bool:
    """Detect user intent to create Linear tasks/issues from context."""
    latest_user = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            latest_user = (msg.get("content") or "").lower()
            break
    if not latest_user:
        return False
    has_linear = "linear" in latest_user
    has_task_intent = any(
        keyword in latest_user
        for keyword in (
            "create task",
            "create tasks",
            "create issue",
            "create issues",
            "open task",
            "open ticket",
            "from context",
            "based on context",
        )
    )
    return has_linear and has_task_intent


def _task_execution_instruction() -> str:
    team_hint = (
        f"Use team_id '{LINEAR_DEFAULT_TEAM_ID}' unless the user explicitly asks for a different team."
        if LINEAR_DEFAULT_TEAM_ID
        else "If team_id is missing, call list_teams and choose the first team."
    )
    return (
        "Task execution mode is ON for this request.\n"
        "- Call Linear tools directly in this same response; do not output a plan without tool calls.\n"
        "- Do not ask for Linear API keys or connection setup.\n"
        f"- {team_hint}\n"
        "- Create the issues now and return identifiers + URLs."
    )


# ── Call Open WebUI (non-streaming for reliability) ──────────────────

async def _call_openwebui(messages: list[dict], chat_id: int, bot: Bot) -> str:
    """Call Open WebUI WITHOUT streaming.

    Non-streaming lets Open WebUI fully process tool calls server-side
    (code search, Tableau queries, Linear operations, etc.) and return
    the complete final answer — not just the planning text.
    """
    headers = {
        "Authorization": f"Bearer {OPENWEBUI_API_KEY}",
        "Content-Type": "application/json",
    }

    placeholder = await bot.send_message(chat_id=chat_id, text="Working on it...")

    convo_messages = list(messages)
    linear_task_mode = _is_linear_task_request(convo_messages)

    for nudge_round in range(MAX_TOOL_NUDGES + 1):
        full_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if linear_task_mode:
            full_messages.append({"role": "system", "content": _task_execution_instruction()})
        full_messages += convo_messages
        payload = {
            "model": OPENWEBUI_MODEL,
            "messages": full_messages,
            "stream": False,
        }
        if OPENWEBUI_TOOL_IDS:
            payload["tool_ids"] = OPENWEBUI_TOOL_IDS
        if linear_task_mode:
            payload["tool_choice"] = "required"

        for attempt in range(MAX_RETRIES):
            timeout_config = httpx.Timeout(
                connect=30.0,
                read=REQUEST_TIMEOUT,
                write=30.0,
                pool=30.0,
            )

            try:
                async with httpx.AsyncClient(timeout=timeout_config) as client:
                    resp = await client.post(
                        f"{OPENWEBUI_API_URL}/api/chat/completions",
                        headers=headers,
                        json=payload,
                    )

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("retry-after", 2 ** attempt * 3))
                    log.warning("Rate limited (429), attempt %s/%s", attempt + 1, MAX_RETRIES)
                    await _safe_edit(placeholder, f"Rate limited, retrying in {retry_after}s...")
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status_code >= 400:
                    log.error("Open WebUI error %s: %s", resp.status_code, resp.text[:500])
                    error_msg = f"Error from AI backend ({resp.status_code}). Please try again."
                    await _safe_edit(placeholder, error_msg)
                    return error_msg

                data = resp.json()
                final = _extract_content(data).strip() or "No response from the model."

                if linear_task_mode and _looks_like_incomplete_linear_task_reply(final):
                    if nudge_round < MAX_TOOL_NUDGES:
                        log.warning(
                            "Linear task reply incomplete (no issue artifacts), applying nudge %s",
                            nudge_round + 1,
                        )
                        await _safe_edit(placeholder, "Completing Linear task creation and collecting issue links...")
                        convo_messages.extend([
                            {"role": "assistant", "content": final},
                            {
                                "role": "user",
                                "content": (
                                    "Do the Linear tool calls now and finish in this same reply. "
                                    "Return created issue identifiers and URLs only."
                                ),
                            },
                        ])
                        break
                    log.error("Linear task reply incomplete after %s nudges", MAX_TOOL_NUDGES)
                    await _safe_edit(placeholder, LINEAR_TASK_FAILURE_MSG)
                    return LINEAR_TASK_FAILURE_MSG

                if _looks_like_deferral(final):
                    if nudge_round < MAX_TOOL_NUDGES:
                        log.warning("Model returned deferral text, applying nudge %s", nudge_round + 1)
                        await _safe_edit(placeholder, "Fetching actual data and final answer...")
                        convo_messages.extend([
                            {"role": "assistant", "content": final},
                            {
                                "role": "user",
                                "content": (
                                    "Do the required tool calls now and return the final results in this "
                                    "message with concrete numbers. Do not say you will check later."
                                ),
                            },
                        ])
                        break
                    log.error("Model still returned deferral after %s nudges", MAX_TOOL_NUDGES)
                    await _safe_edit(placeholder, DEFERRAL_FAILURE_MSG)
                    return DEFERRAL_FAILURE_MSG

                # Send the complete answer
                await _send_final(placeholder, final, chat_id, bot)
                return final

            except httpx.ReadTimeout:
                log.error("Request timeout after %ss", REQUEST_TIMEOUT)
                error_msg = "The AI is taking too long (tools may be slow). Please try again or /clear."
                await _safe_edit(placeholder, error_msg)
                return error_msg
            except Exception as e:
                log.exception("Error calling Open WebUI: %s", e)
                if attempt < MAX_RETRIES - 1:
                    await _safe_edit(placeholder, f"Error, retrying ({attempt + 1}/{MAX_RETRIES})...")
                    await asyncio.sleep(2)
                    continue
                error_msg = f"Connection error: {type(e).__name__}. Please try again."
                await _safe_edit(placeholder, error_msg)
                return error_msg
        else:
            continue
        continue

    error_msg = "The AI backend is busy. Please try again in a minute."
    await _safe_edit(placeholder, error_msg)
    return error_msg


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
        "Hello! I'm SourceMind, your AI assistant.\n\n"
        "I can search code, check analytics, manage Linear tasks, "
        "and answer product questions.\n\n"
        "Just ask me anything.\n\n"
        "Commands:\n"
        "/clear - Reset conversation\n"
        "/status - Check connection"
    )


async def cmd_clear(update: Update, _) -> None:
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return
    histories[user_id] = []
    _processing.discard(user_id)
    await update.message.reply_text("Conversation cleared.")


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
            processing = "yes" if user_id in _processing else "idle"
            await update.message.reply_text(
                f"Connected to Open WebUI\n"
                f"Model: {OPENWEBUI_MODEL}\n"
                f"History: {len(histories[user_id])} messages\n"
                f"Status: {processing}"
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
            break
        except asyncio.TimeoutError:
            pass


async def _process_message(user_id: int, chat_id: int, bot: Bot, messages: list[dict]) -> None:
    """Background task: call Open WebUI and update history."""
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(chat_id, bot, stop_typing))

    try:
        _processing.add(user_id)
        reply = await _call_openwebui(messages, chat_id, bot)
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

    if user_id in _processing:
        await update.message.reply_text(
            "Still working on your previous request. Wait for it or /clear to cancel."
        )
        return

    histories[user_id].append({"role": "user", "content": user_text})

    if len(histories[user_id]) > MAX_HISTORY:
        histories[user_id] = histories[user_id][-MAX_HISTORY:]

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

    if OPENWEBUI_TOOL_IDS:
        log.info("Open WebUI tool_ids configured: %s", ", ".join(OPENWEBUI_TOOL_IDS))
    else:
        log.warning(
            "OPENWEBUI_TOOL_IDS is empty. The model may not have access to tool servers (Linear, UXCam, Tableau, etc.)."
        )
    if LINEAR_DEFAULT_TEAM_ID:
        log.info("Linear default team configured: %s", LINEAR_DEFAULT_TEAM_ID)
    else:
        log.info("LINEAR_DEFAULT_TEAM_ID not set. Bot will resolve team dynamically via list_teams.")

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
