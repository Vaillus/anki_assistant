#!/usr/bin/env bash
# Start the review app from this checkout and open it in the browser.
#
#   ./start.sh
#
# Needs Anki Desktop open with AnkiConnect. A server already listening on the
# port (a previous run, maybe from another checkout) is stopped first, so the
# browser always shows the code of the directory this script lives in.
# The server keeps running after the script returns; its output goes to
# .anki-web.log next to this file.
set -euo pipefail
cd "$(dirname "$0")"

PORT=5070
URL="http://localhost:$PORT"
LOG=.anki-web.log

if ! curl -s -m 2 localhost:8765 -d '{"action":"version","version":6}' >/dev/null; then
  echo "AnkiConnect does not answer on localhost:8765 — open Anki first." >&2
  exit 1
fi

if pids=$(lsof -tnP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null) && [ -n "$pids" ]; then
  echo "stopping the server already on port $PORT (pid $pids)"
  kill $pids
  sleep 1
fi

nohup uv run anki-web >"$LOG" 2>&1 &
disown

for _ in $(seq 1 40); do
  curl -s -m 1 "$URL" >/dev/null && break
  sleep 0.25
done
if ! curl -s -m 1 "$URL" >/dev/null; then
  echo "the server did not come up; see $LOG" >&2
  tail -n 20 "$LOG" >&2
  exit 1
fi

open "$URL"
echo "anki-web running at $URL (log: $LOG)"
