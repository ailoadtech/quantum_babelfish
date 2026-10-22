#!/usr/bin/env bash
# Start the qbabel playground on http://127.0.0.1:8000 (override with PORT=...)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  uv venv .venv
fi
uv pip install --quiet --python .venv/bin/python -r web/requirements.txt

exec .venv/bin/python -m uvicorn web.app:app --host 127.0.0.1 --port "${PORT:-8000}"
