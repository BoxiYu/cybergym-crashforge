#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 2 ]]; then
  echo "usage: $0 {status|pause|resume} WAVE_NAME [GROUP_FILE] [PORT]" >&2
  echo "resume honors env: SPLIT_GROUP_MAX_ACTIVE SPLIT_GROUP_POLL_SECONDS SPLIT_GROUP_CAMPAIGN" >&2
  exit 1
fi

ACTION="$1"
WAVE_NAME="$2"
WAVE_DIR="$ROOT/reports/$WAVE_NAME"
PID_FILE="$WAVE_DIR/queue_launcher_binary.pid"
QUEUE_FILE="$WAVE_DIR/combined_queue.json"

sync_launcher_pid() {
  local matched_pid=""
  while IFS= read -r line; do
    local pid args
    read -r pid args <<<"$(printf '%s\n' "$line" | sed -E 's/^[[:space:]]+//')"
    [[ -n "$pid" ]] || continue
    [[ "$args" == *"scripts/rescue_queue_launcher.py"* ]] || continue
    [[ "$args" == *"$QUEUE_FILE"* ]] || continue
    matched_pid="$pid"
    break
  done < <(ps -eo pid=,args=)
  if [[ -n "$matched_pid" ]]; then
    printf '%s\n' "$matched_pid" >"$PID_FILE"
    printf '%s\n' "$matched_pid"
    return 0
  fi
  return 1
}

launcher_pid() {
  if [[ -f "$PID_FILE" ]]; then
    cat "$PID_FILE"
  fi
}

launcher_running() {
  local pid
  pid="$(launcher_pid || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  pid="$(sync_launcher_pid || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

status() {
  local pid
  pid="$(launcher_pid || true)"
  if launcher_running; then
    pid="$(launcher_pid || true)"
    echo "running pid=$pid wave=$WAVE_NAME"
  else
    echo "stopped pid=${pid:-none} wave=$WAVE_NAME"
  fi
}

pause() {
  local pid
  pid="$(launcher_pid || true)"
  if [[ -z "$pid" ]]; then
    echo "no pid file for $WAVE_NAME"
    return 0
  fi
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    for _ in {1..20}; do
      if ! kill -0 "$pid" 2>/dev/null; then
        echo "stopped pid=$pid wave=$WAVE_NAME"
        return 0
      fi
      sleep 0.25
    done
    kill -9 "$pid" 2>/dev/null || true
    echo "force_stopped pid=$pid wave=$WAVE_NAME"
    return 0
  fi
  echo "already stopped pid=$pid wave=$WAVE_NAME"
}

resume() {
  if launcher_running; then
    status
    return 0
  fi
  if [[ $# -lt 4 ]]; then
    echo "resume requires GROUP_FILE and PORT" >&2
    exit 1
  fi
  local group_file="$3"
  local port="$4"
  SPLIT_GROUP_BINARY_PORT="$port" \
    SPLIT_GROUP_MAX_ACTIVE="${SPLIT_GROUP_MAX_ACTIVE:-20}" \
    SPLIT_GROUP_POLL_SECONDS="${SPLIT_GROUP_POLL_SECONDS:-5}" \
    bash "$ROOT/scripts/launch_split_group_binary_wave.sh" "$group_file" "$WAVE_NAME"
}

case "$ACTION" in
  status)
    status
    ;;
  pause)
    pause
    ;;
  resume)
    resume "$@"
    ;;
  *)
    echo "unknown action: $ACTION" >&2
    exit 1
    ;;
esac
