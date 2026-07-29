#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 2 ]]; then
  echo "usage: $0 GROUP_FILE WAVE_NAME" >&2
  exit 1
fi

GROUP_FILE="$1"
WAVE_NAME="$2"
GROUP_STEM="$(basename "$GROUP_FILE" .md)"

PYTHON_BIN="$ROOT/.venv/bin/python"
WAVE_DIR="$ROOT/reports/$WAVE_NAME"
SERVER_PORT="${SPLIT_GROUP_BINARY_PORT:-18669}"
SERVER_URL="http://127.0.0.1:${SERVER_PORT}"
SERVER_LOG_DIR="${SPLIT_GROUP_SERVER_LOG_DIR:-$ROOT/server_poc_${WAVE_NAME}}"
SERVER_LOG="$WAVE_DIR/binary_server.log"
SERVER_PID_FILE="$WAVE_DIR/binary_server.pid"
QUEUE_FILE="$WAVE_DIR/combined_queue.json"
STATE_FILE="$WAVE_DIR/combined_queue.binary.state.json"
SCHEDULER_LOG="$ROOT/codex_rescue_runs_local/${WAVE_NAME}_scheduler_binary.log"
RUN_LOG_DIR="$ROOT/codex_rescue_runs_local/queue_logs_${WAVE_NAME}_binary"
LAUNCHER_LOG="$WAVE_DIR/queue_launcher_binary.log"
LAUNCHER_PID_FILE="$WAVE_DIR/queue_launcher_binary.pid"
OUTPUT_ROOT="./codex_rescue_runs_local/${WAVE_NAME}_single"
CAMPAIGN="${SPLIT_GROUP_CAMPAIGN:-$WAVE_NAME}"
MAX_ACTIVE_RUNNERS="${SPLIT_GROUP_MAX_ACTIVE:-8}"
POLL_SECONDS="${SPLIT_GROUP_POLL_SECONDS:-15}"
IGNORE_USAGE_LIMIT="${SPLIT_GROUP_IGNORE_USAGE_LIMIT:-1}"
GLOBAL_MAX_ACTIVE_RUNNERS="${SPLIT_GROUP_GLOBAL_MAX_ACTIVE:-16}"
GLOBAL_RUNNER_HEADROOM="${SPLIT_GROUP_GLOBAL_RUNNER_HEADROOM:-2}"
LOADAVG_LIMIT_FACTOR="${SPLIT_GROUP_LOADAVG_LIMIT_FACTOR:-1.25}"
MIN_MEM_AVAILABLE_MB="${SPLIT_GROUP_MIN_MEM_AVAILABLE_MB:-4096}"
MIN_SWAP_FREE_MB="${SPLIT_GROUP_MIN_SWAP_FREE_MB:-512}"

mkdir -p "$WAVE_DIR" "$SERVER_LOG_DIR" "$RUN_LOG_DIR"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*"
}

find_process_pid_by_substrings() {
  local line pid args needle
  while IFS= read -r line; do
    pid="${line%% *}"
    args="${line#* }"
    for needle in "$@"; do
      [[ "$args" == *"$needle"* ]] || continue 2
    done
    printf '%s\n' "$pid"
    return 0
  done < <(ps -eo pid=,args=)
  return 1
}

list_process_pids_by_substrings() {
  local line pid args needle matched
  while IFS= read -r line; do
    pid="${line%% *}"
    args="${line#* }"
    matched=1
    for needle in "$@"; do
      if [[ "$args" != *"$needle"* ]]; then
        matched=0
        break
      fi
    done
    if [[ "$matched" == 1 ]]; then
      printf '%s\n' "$pid"
    fi
  done < <(ps -eo pid=,args=)
}

sync_pid_file_from_process_match() {
  local pid_file="$1"
  shift
  local pid
  pid="$(find_process_pid_by_substrings "$@" || true)"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  printf '%s\n' "$pid" >"$pid_file"
  printf '%s\n' "$pid"
  return 0
}

pid_matches_substrings() {
  local pid="$1"
  shift
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  local args needle
  args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  [[ -n "$args" ]] || return 1
  for needle in "$@"; do
    [[ "$args" == *"$needle"* ]] || return 1
  done
  return 0
}

terminate_pid() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null || return 0
  kill "$pid" 2>/dev/null || true
  for _ in {1..40}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.25
  done
  kill -9 "$pid" 2>/dev/null || true
}

pid_has_desired_launcher_config() {
  local pid="$1"
  local args
  args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  [[ "$args" == *"--max-active-runners $MAX_ACTIVE_RUNNERS"* ]] &&
    [[ "$args" == *"--global-max-active-runners $GLOBAL_MAX_ACTIVE_RUNNERS"* ]] &&
    [[ "$args" == *"--global-runner-headroom $GLOBAL_RUNNER_HEADROOM"* ]] &&
    [[ "$args" == *"--loadavg-limit-factor $LOADAVG_LIMIT_FACTOR"* ]] &&
    [[ "$args" == *"--min-mem-available-mb $MIN_MEM_AVAILABLE_MB"* ]] &&
    [[ "$args" == *"--min-swap-free-mb $MIN_SWAP_FREE_MB"* ]]
}

have_binary_healthz() {
  curl -fsS "$SERVER_URL/healthz" >/dev/null 2>&1
}

ensure_runner_images() {
  log "ensuring binary-only runner images are present"
  "$PYTHON_BIN" "$ROOT/scripts/server_data/download_binary_only_runners.py" >/dev/null
}

ensure_group_binary_assets() {
  "$PYTHON_BIN" - "$GROUP_FILE" <<'PY'
from pathlib import Path
import sys

group_file = Path(sys.argv[1]).resolve()
root = Path("cybergym-server-data")
missing = []
for line in group_file.read_text(encoding="utf-8").splitlines():
    task_id = line.strip()
    if not task_id:
        continue
    family, subid = task_id.split(":", 1)
    for mode in ("vul", "fix"):
        candidate = root / family / subid / mode
        if candidate.exists():
            continue
        missing.append(str(candidate))
        if len(missing) >= 10:
            break
    if len(missing) >= 10:
        break
if missing:
    print("\n".join(missing))
    raise SystemExit(1)
PY
}

prepare_wave() {
  log "preparing fresh manifests for $GROUP_FILE -> $WAVE_DIR"
  "$PYTHON_BIN" "$ROOT/scripts/prepare_split_group_fresh_wave.py" \
    --group-file "$GROUP_FILE" \
    --wave-dir "$WAVE_DIR" \
    --output-root "$OUTPUT_ROOT" \
    --server "$SERVER_URL" \
    --data-dir "./cybergym_data/data" \
    --campaign "$CAMPAIGN" \
    >/dev/null
}

start_binary_server() {
  if have_binary_healthz; then
    if sync_pid_file_from_process_match \
      "$SERVER_PID_FILE" \
      "-m cybergym.server" \
      "--port $SERVER_PORT" \
      "--db_path $SERVER_LOG_DIR/poc.db" >/dev/null; then
      log "binary-only server already healthy on $SERVER_URL pid=$(cat "$SERVER_PID_FILE")"
    else
      log "binary-only server already healthy on $SERVER_URL"
    fi
    return
  fi

  log "starting binary-only server on $SERVER_URL"
  : >"$SERVER_LOG"
  setsid -f "$PYTHON_BIN" -m cybergym.server \
    --host 127.0.0.1 \
    --port "$SERVER_PORT" \
    --mask_map_path "$ROOT/mask_map.json" \
    --log_dir "$SERVER_LOG_DIR" \
    --db_path "$SERVER_LOG_DIR/poc.db" \
    --binary_dir "$ROOT/cybergym-server-data" \
    >>"$SERVER_LOG" 2>&1

  local attempts=0
  until have_binary_healthz; do
    attempts=$((attempts + 1))
    if (( attempts > 120 )); then
      log "binary-only server failed to become healthy; see $SERVER_LOG"
      return 1
    fi
    sleep 2
  done
  sync_pid_file_from_process_match \
    "$SERVER_PID_FILE" \
    "-m cybergym.server" \
    "--port $SERVER_PORT" \
    "--db_path $SERVER_LOG_DIR/poc.db" >/dev/null || true
  log "binary-only server healthy on $SERVER_URL pid=$(cat "$SERVER_PID_FILE" 2>/dev/null || echo unknown)"
}

start_queue_launcher() {
  local ignore_usage_limit_args=()
  if [[ "$IGNORE_USAGE_LIMIT" == "1" ]]; then
    ignore_usage_limit_args+=(--ignore-usage-limit)
  fi
  if [[ -f "$LAUNCHER_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$LAUNCHER_PID_FILE" 2>/dev/null || true)"
    if pid_matches_substrings "$existing_pid" "scripts/rescue_queue_launcher.py" "$WAVE_NAME/combined_queue.json"; then
      if pid_has_desired_launcher_config "$existing_pid"; then
        log "queue launcher already running pid=$existing_pid"
        return
      fi
      log "replacing queue launcher pid=$existing_pid due_to=launcher_config_mismatch desired_max_active=$MAX_ACTIVE_RUNNERS desired_global_max=$GLOBAL_MAX_ACTIVE_RUNNERS"
      terminate_pid "$existing_pid"
    fi
    rm -f "$LAUNCHER_PID_FILE"
  fi

  local matching_pids=()
  local pid desired_pid=""
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    matching_pids+=("$pid")
  done < <(list_process_pids_by_substrings "scripts/rescue_queue_launcher.py" "$WAVE_NAME/combined_queue.json")
  if (( ${#matching_pids[@]} > 0 )); then
    for pid in "${matching_pids[@]}"; do
      if pid_has_desired_launcher_config "$pid"; then
        if [[ -z "$desired_pid" ]]; then
          desired_pid="$pid"
          continue
        fi
      fi
      log "terminating duplicate_or_mismatched queue launcher pid=$pid desired_max_active=$MAX_ACTIVE_RUNNERS desired_global_max=$GLOBAL_MAX_ACTIVE_RUNNERS"
      terminate_pid "$pid"
    done
    if [[ -n "$desired_pid" ]] && kill -0 "$desired_pid" 2>/dev/null; then
      printf '%s\n' "$desired_pid" >"$LAUNCHER_PID_FILE"
      log "queue launcher already running pid=$desired_pid"
      return
    fi
  fi

  log "starting queue launcher for $WAVE_NAME"
  : >"$LAUNCHER_LOG"
  setsid -f "$PYTHON_BIN" "$ROOT/scripts/rescue_queue_launcher.py" \
    --queue-file "$QUEUE_FILE" \
    --server "$SERVER_URL" \
    --data-dir "$ROOT/cybergym_data/data" \
    --pocdb-path "$SERVER_LOG_DIR/poc.db" \
    --python-bin "$PYTHON_BIN" \
    --max-active-runners "$MAX_ACTIVE_RUNNERS" \
    --global-max-active-runners "$GLOBAL_MAX_ACTIVE_RUNNERS" \
    --global-runner-headroom "$GLOBAL_RUNNER_HEADROOM" \
    --loadavg-limit-factor "$LOADAVG_LIMIT_FACTOR" \
    --min-mem-available-mb "$MIN_MEM_AVAILABLE_MB" \
    --min-swap-free-mb "$MIN_SWAP_FREE_MB" \
    --poll-seconds "$POLL_SECONDS" \
    --scheduler-log "$SCHEDULER_LOG" \
    --run-log-dir "$RUN_LOG_DIR" \
    --results-root "$ROOT/codex_rescue_runs_local" \
    --state-file "$STATE_FILE" \
    --active-runner-output-root "$OUTPUT_ROOT" \
    "${ignore_usage_limit_args[@]}" \
    >>"$LAUNCHER_LOG" 2>&1
  sync_pid_file_from_process_match \
    "$LAUNCHER_PID_FILE" \
    "scripts/rescue_queue_launcher.py" \
    "$WAVE_NAME/combined_queue.json" >/dev/null || true
  log "queue launcher pid=$(cat "$LAUNCHER_PID_FILE" 2>/dev/null || echo unknown)"
}

main() {
  ensure_runner_images
  ensure_group_binary_assets
  start_binary_server
  prepare_wave
  start_queue_launcher
  log "split-group binary wave staged: $WAVE_NAME"
}

main "$@"
