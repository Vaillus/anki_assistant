#!/bin/bash
# Bring up the anki-assistant web server. Idempotent — a server already running is left
# alone. Anki desktop is not started: the app opens Anki's collection file itself and
# needs Anki desktop closed (the two take turns on the file).
#
# "Anki Assistant.app" in ~/Applications calls `serve` from its own process and then
# renders the page itself; `start` is the terminal equivalent, which hands the window
# to that app (or to the browser when it is not installed).
#
#   scripts/app/launch.sh          serve, then show the window
#   scripts/app/launch.sh serve    servers only, no window (what the app calls)
#   scripts/app/launch.sh stop     stop the server
#   scripts/app/launch.sh status   report what is running

set -u

# A GUI launch inherits a bare PATH: uv lives in one of these.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT=5070  # matches HOST/PORT in src/anki_assistant/web/main.py
APP_URL="http://127.0.0.1:$PORT"
BUNDLE_ID="local.anki-assistant"  # matches install.sh
LOG="$HOME/Library/Logs/anki-assistant.log"
SERVER_WAIT="${SERVER_WAIT:-30}"  # seconds allowed for uvicorn to bind the port

alert() {  # a double-click has no terminal to print to
  osascript -e "display alert \"Anki Assistant\" message \"$1\"" >/dev/null 2>&1
}

web_up() { curl -fs -m 1 -o /dev/null "$APP_URL"; }

anki_open() { pgrep -f "Anki.app/Contents/MacOS" >/dev/null; }

server_pids() { lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null; }

wait_for() {  # wait_for <seconds> <command...>
  local deadline=$(( SECONDS + $1 )); shift
  while (( SECONDS < deadline )); do
    "$@" && return 0
    sleep 1
  done
  return 1
}

case "${1:-start}" in
  stop)
    pids="$(server_pids)"
    [ -z "$pids" ] && { echo "not running"; exit 0; }
    # shellcheck disable=SC2086
    kill $pids && echo "stopped ($pids)"
    exit 0
    ;;
  status)
    web_up && echo "web: up at $APP_URL" || echo "web: down"
    anki_open && echo "anki desktop: open (the app's actions wait for it to close)" ||
      echo "anki desktop: closed"
    exit 0
    ;;
  start|serve) MODE="${1:-start}" ;;
  *) echo "usage: $(basename "$0") [start|serve|stop|status]" >&2; exit 2 ;;
esac

# Keep the log from growing forever.
[ -f "$LOG" ] && [ "$(stat -f%z "$LOG")" -gt 1000000 ] && : > "$LOG"
mkdir -p "$(dirname "$LOG")"
echo "--- $(date '+%Y-%m-%d %H:%M:%S') launch" >> "$LOG"

# 1. The web server.
if ! web_up; then
  cd "$PROJECT_DIR" || exit 1
  ANKI_WEB_RELOAD=0 nohup uv run anki-web >> "$LOG" 2>&1 &
  if ! wait_for "$SERVER_WAIT" web_up; then
    alert "The server did not start. Last lines of $LOG:

$(tail -n 12 "$LOG" | sed 's/"/\\"/g')"
    exit 1
  fi
fi

# 2. The window, when there is one to show. The app renders the page itself, so
# `serve` stops here; `start` just brings that app up, and falls back to the default
# browser when the bundle is not installed.
if [ "$MODE" = start ]; then
  open -b "$BUNDLE_ID" 2>/dev/null || open "$APP_URL"
fi
