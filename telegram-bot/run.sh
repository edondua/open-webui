#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
pip install -q -r requirements.txt
# No WEBHOOK_URL → runs in polling mode for local dev
exec uvicorn app:web --host 0.0.0.0 --port "${PORT:-8788}" --reload
