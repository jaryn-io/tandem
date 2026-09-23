#!/usr/bin/env bash
# ShiftBoard Local Startup Script
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8080

echo "=================================================="
echo "ShiftBoard — Visual Production Planning System"
echo "=================================================="
echo "Directory: $DIR"
echo ""

if command -v python3 >/dev/null 2>&1; then
  echo "Starting local static web server at http://127.0.0.1:$PORT ..."
  echo "Exposure model: Bound strictly to loopback interface (127.0.0.1)."
  echo "External / LAN network access is refused to protect application state and local storage."
  echo "Press Ctrl+C to stop."
  cd "$DIR"
  python3 -m http.server "$PORT" --bind 127.0.0.1
elif command -v xdg-open >/dev/null 2>&1; then
  echo "Opening index.html directly in browser..."
  xdg-open "$DIR/index.html"
else
  echo "Please open $DIR/index.html in any modern browser."
fi
