#!/usr/bin/env bash
# Development: FastAPI (auto-reload) on 127.0.0.1:$PORT + Vite on 127.0.0.1:$VITE_DEV_PORT (proxies /api).
# Ports come from the repo-root .env (defaults 8620 / 5620).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || PY="$ROOT/backend/.venv/Scripts/python.exe"
if [ ! -x "$PY" ]; then
  echo "backend venv missing: cd backend && uv venv --python 3.12 && uv pip install -e '.[dev]'" >&2
  exit 1
fi
env_get() { [ -f "$ROOT/.env" ] && grep -E "^$1=" "$ROOT/.env" | tail -n1 | cut -d= -f2- | tr -d '\r' || true; }
PORT="$(env_get PORT)"; PORT="${PORT:-8620}"
VITE_PORT="$(env_get VITE_DEV_PORT)"; VITE_PORT="${VITE_PORT:-5620}"
[ -d "$ROOT/frontend/node_modules" ] || (cd "$ROOT/frontend" && npm install)

cleanup() { trap - EXIT INT TERM; kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(cd "$ROOT/backend" && "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --reload --reload-dir app) &
(cd "$ROOT/frontend" && npm run dev) &

echo "API  -> http://127.0.0.1:$PORT/api/health"
echo "UI   -> http://127.0.0.1:$VITE_PORT"
wait
