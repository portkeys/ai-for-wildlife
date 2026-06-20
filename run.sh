#!/usr/bin/env bash
# Start the Conservation Video Classifier.
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtualenv + installing deps…"
  python3 -m venv .venv
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  ./.venv/bin/python -m pip install --quiet -r requirements.txt
fi

# Optionally export your key here (or set it in the Settings UI):
# export OPENROUTER_API_KEY="sk-or-v1-..."

echo "→ http://localhost:8000"
exec ./.venv/bin/python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000 "$@"
