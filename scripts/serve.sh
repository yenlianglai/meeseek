#!/bin/bash
# One-command start: build the web console, then run the server (which
# serves the console at http://localhost:8787/ui plus the scheduler + API).
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8787}"
# This machine only. HOST=0.0.0.0 opens the console, its API and the MCP
# surface to the network, with no auth (SECURITY.md).
HOST="${HOST:-127.0.0.1}"
# Hot reload watches the whole tree and costs a core while idle: opt in with DEV=1.
RELOAD=()
[ "${DEV:-0}" = 1 ] && RELOAD=(--reload)

# Frontend: install once, rebuild only when sources changed since last build.
if [ -d frontend ]; then
  if [ ! -d frontend/node_modules ]; then
    echo "[serve] installing frontend deps..."
    npm ci --prefix frontend
  fi
  if [ ! -d frontend/dist ] || \
     [ -n "$(find frontend/src frontend/index.html frontend/vite.config.ts frontend/package.json \
              -newer frontend/dist -print -quit 2>/dev/null)" ]; then
    echo "[serve] building web console..."
    npm run build --prefix frontend
  else
    echo "[serve] web console up to date."
  fi
fi

# Warn, never block: a missing Workspace scope breaks only the Chat, Gmail,
# Drive, Docs and Sheets tools. The console shows it too (/auth_api/status).
if ! uv run python -m meeseek_pack_google.auth --check; then
  echo "[serve] starting anyway — only Chat/Gmail/Drive/Docs tools are affected."
fi

echo "[serve] console → http://localhost:${PORT}/ui"
exec uv run python server.py \
  --host "$HOST" \
  --port "$PORT" \
  ${RELOAD[@]+"${RELOAD[@]}"}
