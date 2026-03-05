#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"
