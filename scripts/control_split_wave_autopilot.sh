#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ACTION="${1:-status}"
LOCK_FILE="$ROOT/reports/split_wave_autopilot.lock"
LOG_FILE="$ROOT/codex_rescue_runs_local/split_wave_autopilot.log"
INTERVAL_SECONDS="${SPLIT_WAVE_AUTOPILOT_INTERVAL_SECONDS:-300}"

autopilot_pid() {
  if [[ -f "$LOCK_FILE" ]]; then
    local pid
    pid="$(tr -d '[:space:]' <"$LOCK_FILE" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
      printf '%s\n' "$pid"
      return 0
    fi
  fi
  return 1
}

autopilot_running() {
  local pid
  pid="$(autopilot_pid || true)"
  [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null
}

status() {
  local pid
  pid="$(autopilot_pid || true)"
  if autopilot_running; then
    echo "running pid=$pid log=$LOG_FILE"
  else
    echo "stopped pid=${pid:-none} log=$LOG_FILE"
  fi
}

start() {
  if autopilot_running; then
    status
    return 0
  fi
  setsid -f "$ROOT/.venv/bin/python" \
    "$ROOT/scripts/split_wave_autopilot.py" \
    --interval-seconds "$INTERVAL_SECONDS" \
    >>"$LOG_FILE" 2>&1
  sleep 1
  status
}

stop() {
  local pid
  pid="$(autopilot_pid || true)"
  if [[ -z "${pid:-}" ]]; then
    echo "already stopped"
    return 0
  fi
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
    for _ in {1..20}; do
      if ! kill -0 "$pid" 2>/dev/null; then
        echo "stopped pid=$pid"
        return 0
      fi
      sleep 0.25
    done
    kill -9 "$pid" 2>/dev/null || true
    echo "force_stopped pid=$pid"
    return 0
  fi
  echo "already stopped pid=$pid"
}

case "$ACTION" in
  status)
    status
    ;;
  start)
    start
    ;;
  stop)
    stop
    ;;
  restart)
    stop
    start
    ;;
  *)
    echo "usage: $0 {status|start|stop|restart}" >&2
    exit 1
    ;;
esac
