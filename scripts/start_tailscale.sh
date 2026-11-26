#!/usr/bin/env bash
# Production-style start: build the UI (if needed), run uvicorn on 127.0.0.1:$PORT only and publish it to
# the tailnet with `tailscale serve` on $TAILSCALE_HTTPS_PORT (HTTPS, tailnet-only, no port forwarding).
# Ports come from the repo-root .env (defaults 8620 / 8445). On Windows use scripts/service.ps1.
#
#   REBUILD_UI=1   force `npm run build`
#   FORCE_SERVE=1  replace an existing tailscale serve mapping on that port
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || PY="$ROOT/backend/.venv/Scripts/python.exe"

env_get() { [ -f "$ROOT/.env" ] && grep -E "^$1=" "$ROOT/.env" | tail -n1 | cut -d= -f2- | tr -d '\r' || true; }
PORT="${PORT:-$(env_get PORT)}"; PORT="${PORT:-8620}"
HTTPS_PORT="${TAILSCALE_HTTPS_PORT:-$(env_get TAILSCALE_HTTPS_PORT)}"; HTTPS_PORT="${HTTPS_PORT:-8445}"
TARGET="http://127.0.0.1:$PORT"

if [ "${REBUILD_UI:-0}" = "1" ] || [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  (cd "$ROOT/frontend" && { [ -d node_modules ] || npm ci; } && npm run build)
fi

if command -v tailscale >/dev/null 2>&1; then
  if [ "$HTTPS_PORT" = "443" ]; then url_re="https://[^ :]+ "; else url_re="https://[^ ]+:${HTTPS_PORT} "; fi
  existing="$(tailscale serve status 2>/dev/null | grep -A2 -E "$url_re" | grep -oE 'proxy http://[^ ]+' | head -n1 || true)"
  if [ -n "$existing" ] && [ "$existing" != "proxy $TARGET" ] && [ "${FORCE_SERVE:-0}" != "1" ]; then
    echo "tailnet port $HTTPS_PORT already serves '$existing'." >&2
    echo "Set TAILSCALE_HTTPS_PORT in .env, or FORCE_SERVE=1 to replace it." >&2
    exit 1
  fi
  if [ "$HTTPS_PORT" = "443" ]; then
    tailscale serve --bg "$TARGET"
  else
    tailscale serve --bg --https="$HTTPS_PORT" "$TARGET"
  fi
else
  echo "tailscale CLI not found; serving on 127.0.0.1:$PORT only" >&2
fi

cd "$ROOT/backend"
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --proxy-headers --forwarded-allow-ips 127.0.0.1
