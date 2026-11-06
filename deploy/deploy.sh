#!/usr/bin/env bash
# Pulls the latest main and restarts the stack. Run on the server, manually or from CI.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/app}"
COMPOSE=(docker compose -f compose.yaml -f compose.prod.yaml)

cd "$APP_DIR"
git fetch --prune origin main
git reset --hard origin/main

"${COMPOSE[@]}" build api
"${COMPOSE[@]}" up -d --remove-orphans
docker image prune -f >/dev/null

for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)" 2>/dev/null; then
    echo "Deployed $(git rev-parse --short HEAD), api is healthy"
    "${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
    exit 0
  fi
  sleep 2
done

echo "api did not become healthy" >&2
"${COMPOSE[@]}" logs --tail 50 api migrate >&2
exit 1
