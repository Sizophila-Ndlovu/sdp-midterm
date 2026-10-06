#!/usr/bin/env bash
# Start the RAT dashboard.
#   Usage: ./start.sh [port]     (default 5000; also honours $PORT)
set -e
cd "$(dirname "$0")"

PORT="${1:-${PORT:-5000}}"

# Prefer an isolated virtualenv; fall back to system python3 (e.g. when the
# python3-venv package is not installed).
PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  if python3 -m venv .venv 2>/dev/null; then
    PY=".venv/bin/python"
    echo "Created virtualenv in .venv"
  else
    PY="python3"
    echo "Note: virtualenv unavailable - using system python3"
  fi
fi

# Runtime dependency is just flask (pytest is only needed for tests/smoke).
if ! "$PY" -c "import flask" 2>/dev/null; then
  echo "Installing dependencies..."
  "$PY" -m pip install -r requirements.txt
fi

echo
echo "RAT starting on http://localhost:${PORT}   (Ctrl+C to stop)"
echo "Database and cloned repositories live in ./data"
echo
export PORT
exec "$PY" app.py
