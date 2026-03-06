# Telegram Bot for Open WebUI

Telegram bot that forwards your messages to Open WebUI and returns AI responses with full tool support (code-tools, memory-tools, tableau-tools, uxcam-tools).

## Setup

### 1. Create Telegram Bot

1. Open Telegram, find **@BotFather**
2. Send `/newbot`, follow prompts to name it
3. Copy the bot token

### 2. Get Open WebUI API Key

1. Open your Open WebUI instance
2. Go to **Settings -> Account -> API Keys**
3. Click **Create new API key**
4. Copy the key (starts with `sk-`)

Note: API keys must be enabled by admin under **Admin -> Settings -> General -> Enable API Keys**.

### 3. Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Token from BotFather |
| `OPENWEBUI_API_URL` | Yes | e.g. `https://your-openwebui.up.railway.app` |
| `OPENWEBUI_API_KEY` | Yes | Open WebUI API key (`sk-...`) |
| `OPENWEBUI_MODEL` | Yes | Model ID (e.g. `gpt-4o`, `claude-sonnet-4-20250514`) |
| `OPENWEBUI_TOOL_IDS` | Yes (for tool use) | Comma-separated tool IDs enabled for this bot (e.g. `server:linear,server:uxcam,server:tableau,server:code-tools,server:memory-tools`) |
| `LINEAR_DEFAULT_TEAM_ID` | No | Default team UUID for Linear task creation requests from context |
| `LINEAR_TOOLS_URL` | No | Linear tools service base URL for deterministic fallback (default: `https://linear-tools-production.up.railway.app`) |
| `LINEAR_TOOLS_API_KEY` | No | Bearer token for `LINEAR_TOOLS_URL` fallback execution |
| `LINEAR_TASK_FALLBACK_ENABLED` | No | Enable direct fallback task creation when model stalls (`true` by default) |
| `WEBHOOK_URL` | No | Public URL of this service (for webhook mode) |
| `ALLOWED_TELEGRAM_USERS` | No | Comma-separated Telegram user IDs |
| `MAX_HISTORY` | No | Max messages per user (default: 50) |

### 4. Run Locally

```bash
export TELEGRAM_BOT_TOKEN="your-token"
export OPENWEBUI_API_URL="https://your-openwebui.up.railway.app"
export OPENWEBUI_API_KEY="sk-your-key"
export OPENWEBUI_MODEL="gpt-4o"
export OPENWEBUI_TOOL_IDS="server:linear,server:uxcam,server:tableau,server:code-tools,server:memory-tools"
# Optional: force task creation into a specific Linear team
export LINEAR_DEFAULT_TEAM_ID="1e2c18d8-a192-4a48-a7f8-f6a5e758cec8"

cd telegram-bot
./run.sh
```

Without `WEBHOOK_URL`, the bot runs in long-polling mode (good for dev).

### 5. Deploy to Railway

1. Create new service in Railway
2. Connect repo, set root directory: `telegram-bot`
3. Add env vars from table above
4. Set `WEBHOOK_URL` to the generated Railway domain (e.g. `https://telegram-bot-xxx.up.railway.app`)
5. Start command: `uvicorn app:web --host 0.0.0.0 --port $PORT`

## Bot Commands

- `/start` - Welcome message
- `/clear` - Reset conversation history
- `/status` - Check connection to Open WebUI

## How It Works

1. You send a message in Telegram
2. Bot forwards it to Open WebUI `/api/chat/completions` with your conversation history
3. Open WebUI processes it with the configured model + all connected tools
4. Response streams back and is sent to your Telegram chat
