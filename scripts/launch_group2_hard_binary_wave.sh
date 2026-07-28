#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WAVE_DIR="$ROOT/reports/group2_hard_wave_2026-07-27"
ARCHIVE_URL="https://huggingface.co/datasets/sunblaze-ucb/cybergym-server-binary/resolve/main/cybergym-server-data.7z"
ARCHIVE_PATH="$ROOT/cybergym-server-data.7z"
EXPECTED_ARCHIVE_BYTES=20841640942
BINARY_DIR="$ROOT/cybergym-server-data"
PYTHON_BIN="$ROOT/.venv/bin/python"
SERVER_PORT="${GROUP2_HARD_BINARY_PORT:-18668}"
SERVER_URL="http://127.0.0.1:${SERVER_PORT}"
SERVER_LOG_DIR="$ROOT/server_poc_group2_binary"
SERVER_LOG="$WAVE_DIR/binary_server.log"
SERVER_PID_FILE="$WAVE_DIR/binary_server.pid"
QUEUE_FILE="$WAVE_DIR/combined_queue.json"
STATE_FILE="$WAVE_DIR/combined_queue.binary.state.json"
SCHEDULER_LOG="$ROOT/codex_rescue_runs_local/group2_hard_wave_20260727_scheduler_binary.log"
RUN_LOG_DIR="$ROOT/codex_rescue_runs_local/queue_logs_group2_hard_wave_20260727_binary"
LAUNCHER_LOG="$WAVE_DIR/queue_launcher_binary.log"
LAUNCHER_PID_FILE="$WAVE_DIR/queue_launcher_binary.pid"
MONITOR_LOG="$WAVE_DIR/monitor.stdout.log"
MONITOR_PID_FILE="$WAVE_DIR/monitor.pid"
POLL_SECONDS="${GROUP2_HARD_POLL_SECONDS:-30}"
MAX_ACTIVE_RUNNERS="${GROUP2_HARD_MAX_ACTIVE:-6}"
IGNORE_USAGE_LIMIT="${GROUP2_HARD_IGNORE_USAGE_LIMIT:-1}"
BINARY_EXTRACT_LOG="$WAVE_DIR/binary_extract.log"
GLOBAL_MAX_ACTIVE_RUNNERS="${GROUP2_HARD_GLOBAL_MAX_ACTIVE:-16}"
GLOBAL_RUNNER_HEADROOM="${GROUP2_HARD_GLOBAL_RUNNER_HEADROOM:-2}"
LOADAVG_LIMIT_FACTOR="${GROUP2_HARD_LOADAVG_LIMIT_FACTOR:-1.25}"
MIN_MEM_AVAILABLE_MB="${GROUP2_HARD_MIN_MEM_AVAILABLE_MB:-4096}"
MIN_SWAP_FREE_MB="${GROUP2_HARD_MIN_SWAP_FREE_MB:-512}"

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

archive_size() {
  if [[ -f "$ARCHIVE_PATH" ]]; then
    stat -c '%s' "$ARCHIVE_PATH"
  else
    echo 0
  fi
}

archive_complete() {
  local size
  size="$(archive_size)"
  [[ "$size" -ge "$EXPECTED_ARCHIVE_BYTES" ]]
}

have_binary_healthz() {
  curl -fsS "$SERVER_URL/healthz" 2>/dev/null | python3 -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
binary_dir = payload.get("binary_dir")
if payload.get("status") not in {"ok", "degraded"}:
    raise SystemExit(1)
if not binary_dir:
    raise SystemExit(1)
print(binary_dir)
' >/dev/null
}

wait_for_archive() {
  while ! archive_complete; do
    local size
    size="$(archive_size)"
    if pgrep -af 'wget.*cybergym-server-data\.7z' >/dev/null; then
      log "waiting for active cybergym-server-data.7z download (size=${size} bytes)"
      sleep "$POLL_SECONDS"
      continue
    fi
    log "archive missing or incomplete; starting/resuming download"
    wget -c "$ARCHIVE_URL" -O "$ARCHIVE_PATH"
  done
  log "archive ready: $(ls -lh "$ARCHIVE_PATH")"
}

ensure_runner_images() {
  log "ensuring binary-only runner images are present"
  "$PYTHON_BIN" "$ROOT/scripts/server_data/download_binary_only_runners.py"
}

have_group2_binary_assets() {
  "$PYTHON_BIN" - <<'PY' >/dev/null
from pathlib import Path
root = Path("cybergym-server-data")
tasks = [
    line.strip()
    for line in Path("splits/group_02_hard.md").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
missing = []
for task_id in tasks:
    family, subid = task_id.split(":", 1)
    for mode in ("vul", "fix"):
        candidate = root / family / subid / mode
        if not candidate.exists():
            missing.append(str(candidate))
            if len(missing) >= 5:
                raise SystemExit(1)
if missing:
    raise SystemExit(1)
PY
}

ensure_binary_dir() {
  if have_group2_binary_assets; then
    log "binary dir already present with group2 hard assets: $BINARY_DIR"
    return
  fi

  local existing_extract_pid
  existing_extract_pid="$(pgrep -f "/usr/lib/7zip/7z x -y $ARCHIVE_PATH" || true)"
  if [[ -n "$existing_extract_pid" ]]; then
    log "waiting for existing 7z extraction pid=$existing_extract_pid"
    while pgrep -f "/usr/lib/7zip/7z x -y $ARCHIVE_PATH" >/dev/null; do
      sleep 15
    done
    if have_group2_binary_assets; then
      log "existing 7z extraction completed with required group2 hard assets"
      return
    fi
    log "existing 7z extraction finished but required assets are still missing; retrying extraction"
  fi

  log "extracting $ARCHIVE_PATH"
  set +e
  7z x -y "$ARCHIVE_PATH" >"$BINARY_EXTRACT_LOG" 2>&1
  local extract_status=$?
  set -e
  if have_group2_binary_assets; then
    log "binary dir extracted (7z exit status=$extract_status)"
    return
  fi
  log "binary dir extraction incomplete (7z exit status=$extract_status); see $BINARY_EXTRACT_LOG"
  return 1
}

start_binary_server() {
  if have_binary_healthz; then
    if sync_pid_file_from_process_match \
      "$SERVER_PID_FILE" \
      "-m cybergym.server" \
      "--port $SERVER_PORT" \
      "server_poc_group2_binary/poc.db" >/dev/null; then
      log "binary-only server already healthy on $SERVER_URL pid=$(cat "$SERVER_PID_FILE")"
    else
      log "binary-only server already healthy on $SERVER_URL"
    fi
    return
  fi

  if [[ -f "$SERVER_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$SERVER_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
      log "waiting for existing binary-only server pid=$existing_pid"
    fi
  else
    log "starting binary-only server on $SERVER_URL"
    : >"$SERVER_LOG"
    setsid -f "$PYTHON_BIN" -m cybergym.server \
      --host 127.0.0.1 \
      --port "$SERVER_PORT" \
      --mask_map_path "$ROOT/mask_map.json" \
      --log_dir "$SERVER_LOG_DIR" \
      --db_path "$SERVER_LOG_DIR/poc.db" \
      --binary_dir "$BINARY_DIR" \
      >>"$SERVER_LOG" 2>&1
  fi

  local attempts=0
  until have_binary_healthz; do
    attempts=$((attempts + 1))
    if (( attempts > 120 )); then
      log "binary-only server failed to become healthy; see $SERVER_LOG"
      return 1
    fi
    sleep 5
  done
  sync_pid_file_from_process_match \
    "$SERVER_PID_FILE" \
    "-m cybergym.server" \
    "--port $SERVER_PORT" \
    "server_poc_group2_binary/poc.db" >/dev/null || true
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
    if pid_matches_substrings "$existing_pid" "scripts/rescue_queue_launcher.py" "group2_hard_wave_2026-07-27/combined_queue.json"; then
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
  done < <(list_process_pids_by_substrings "scripts/rescue_queue_launcher.py" "group2_hard_wave_2026-07-27/combined_queue.json")
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

  log "starting group2 hard queue launcher"
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
    --poll-seconds 15 \
    --scheduler-log "$SCHEDULER_LOG" \
    --run-log-dir "$RUN_LOG_DIR" \
    --results-root "$ROOT/codex_rescue_runs_local" \
    --state-file "$STATE_FILE" \
    --active-runner-output-root "./codex_rescue_runs_local/group2_hard_wave_20260727_single" \
    "${ignore_usage_limit_args[@]}" \
    >>"$LAUNCHER_LOG" 2>&1
  sync_pid_file_from_process_match \
    "$LAUNCHER_PID_FILE" \
    "scripts/rescue_queue_launcher.py" \
    "group2_hard_wave_2026-07-27/combined_queue.json" >/dev/null || true
  log "queue launcher pid=$(cat "$LAUNCHER_PID_FILE" 2>/dev/null || echo unknown)"
}

start_monitor() {
  if [[ -f "$MONITOR_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$MONITOR_PID_FILE" 2>/dev/null || true)"
    if pid_matches_substrings "$existing_pid" "scripts/monitor_group2_hard_wave.sh"; then
      log "group2 monitor already running pid=$existing_pid"
      return
    fi
    rm -f "$MONITOR_PID_FILE"
  fi

  local matching_monitor_pid=""
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    if [[ -z "$matching_monitor_pid" ]]; then
      matching_monitor_pid="$pid"
      continue
    fi
    log "terminating duplicate group2 monitor pid=$pid"
    terminate_pid "$pid"
  done < <(list_process_pids_by_substrings "scripts/monitor_group2_hard_wave.sh")
  if [[ -n "$matching_monitor_pid" ]] && kill -0 "$matching_monitor_pid" 2>/dev/null; then
    printf '%s\n' "$matching_monitor_pid" >"$MONITOR_PID_FILE"
    log "group2 monitor already running pid=$matching_monitor_pid"
    return
  fi

  log "starting group2 hard monitor"
  : >"$MONITOR_LOG"
  setsid -f "$ROOT/scripts/monitor_group2_hard_wave.sh" >>"$MONITOR_LOG" 2>&1
  sync_pid_file_from_process_match \
    "$MONITOR_PID_FILE" \
    "scripts/monitor_group2_hard_wave.sh" >/dev/null || true
  log "group2 monitor pid=$(cat "$MONITOR_PID_FILE" 2>/dev/null || echo unknown)"
}

main() {
  wait_for_archive
  ensure_runner_images
  ensure_binary_dir
  start_binary_server
  start_queue_launcher
  start_monitor
  log "group2 hard binary wave staged"
}

main "$@"
