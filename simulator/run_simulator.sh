#!/bin/bash
# Interactive launcher for printer_simulator.py - exists so you don't
# have to remember/retype the full venv-qualified python path. Any
# arguments given to this script (e.g. --print-duration 10, --fail-at
# error) are passed straight through to printer_simulator.py either
# way. For real pairing (not just RPC testing), run this with sudo -
# printer_simulator.py itself still binds port 80 by default; sudo
# here, not inside this script, since the foreground case execs
# straight into python and the background case's nohup'd process
# needs to inherit root the same way.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON3=/home/garfield/Documents/code/queue3d/app/.venv/bin/python3
SCRIPT="$(pwd)/printer_simulator.py"
PID_FILE="$(pwd)/.simulator.pid"
LOG_FILE="$(pwd)/simulator.log"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  RUNNING_PID="$(cat "$PID_FILE")"
  echo "The simulator is already running in the background (PID $RUNNING_PID, log: $LOG_FILE)."
  read -rp "Stop it? [y/N] " stop_answer
  if [[ "$stop_answer" =~ ^[Yy]$ ]]; then
    kill "$RUNNING_PID"
    rm -f "$PID_FILE"
    echo "Stopped."
  else
    echo "Leaving it running."
  fi
  exit 0
fi
rm -f "$PID_FILE"  # stale - the file's there but that pid isn't running anymore

# The default --http-port (80, matching the real printer's /auth) needs
# root to bind - found by testing: without this check, printer_simulator.py
# just dies on a PermissionError the moment it tries. Scan for an
# explicit --http-port override (either "--http-port 8080" or
# "--http-port=8080") before assuming the default applies.
EFFECTIVE_HTTP_PORT=80
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    --http-port=*) EFFECTIVE_HTTP_PORT="${args[$i]#*=}" ;;
    --http-port) EFFECTIVE_HTTP_PORT="${args[$((i + 1))]:-80}" ;;
  esac
done

if [[ "$EFFECTIVE_HTTP_PORT" =~ ^[0-9]+$ ]] && [ "$EFFECTIVE_HTTP_PORT" -lt 1024 ] && [ "$EUID" -ne 0 ]; then
  echo "Port $EFFECTIVE_HTTP_PORT needs root to bind (the default, 80, matches the real printer's /auth)."
  read -rp "Re-run this with sudo now? [y/N] " sudo_answer
  if [[ "$sudo_answer" =~ ^[Yy]$ ]]; then
    exec sudo "$0" "$@"
  fi
  echo "Continuing without root - startup will fail unless --http-port is 1024 or higher."
fi

echo "Run the printer simulator in the (f)oreground or (b)ackground?"
read -rp "[f/b] " mode
case "$mode" in
  f|F)
    echo "Starting in the foreground - Ctrl-C to stop."
    exec "$PYTHON3" -u "$SCRIPT" "$@"
    ;;
  b|B)
    # -u (unbuffered stdout/stderr): without it, Python buffers output
    # fully whenever it's not a tty - found by testing, the log file
    # stayed completely empty until the process exited, making it
    # useless for watching a background run live.
    nohup "$PYTHON3" -u "$SCRIPT" "$@" > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    disown
    echo "Started in the background (PID $(cat "$PID_FILE")). Log: $LOG_FILE"
    echo "Run this script again to stop it."
    ;;
  *)
    echo "Didn't understand '$mode' - expected f or b." >&2
    exit 1
    ;;
esac
