#!/usr/bin/env bash
# ForgeDesk launch script - Single documented launch command for Linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export FORGEDESK_HOST="${FORGEDESK_HOST:-127.0.0.1}"
export FORGEDESK_PORT="${FORGEDESK_PORT:-8080}"

echo "Starting ForgeDesk application..."
exec python3 app.py --host "$FORGEDESK_HOST" --port "$FORGEDESK_PORT" "$@"
