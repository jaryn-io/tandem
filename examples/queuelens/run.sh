#!/usr/bin/env bash
set -euo pipefail

# QueueLens Startup Launcher
# Resolves script directory and starts the local server

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "Starting QueueLens local environment..."

if command -v python3 >/dev/null 2>&1; then
    exec python3 "${SCRIPT_DIR}/server.py" "$@"
elif command -v python >/dev/null 2>&1; then
    exec python "${SCRIPT_DIR}/server.py" "$@"
else
    echo "Python 3 is not installed or not in PATH."
    echo "QueueLens can also be run directly by opening 'index.html' in your browser:"
    echo "  file://${SCRIPT_DIR}/index.html"
    exit 1
fi
