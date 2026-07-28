#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 {refresh|status|pause|resume}" >&2
  exit 1
fi

ACTION="$1"
WAVE_DIR="$ROOT/reports/group2_hard_wave_2026-07-27"
PYTHON_BIN="$ROOT/.venv/bin/python"
QUEUE_FILE="$WAVE_DIR/post_followup_retry_queue.json"
QUEUE_SUMMARY_JSON="$WAVE_DIR/post_followup_retry_summary.json"
QUEUE_STATE_FILE="$WAVE_DIR/post_followup_retry_queue.state.json"
LAUNCHER_LOG="$WAVE_DIR/post_followup_queue_launcher.log"
LAUNCHER_PID_FILE="$WAVE_DIR/post_followup_queue_launcher.pid"
SCHEDULER_LOG="$ROOT/codex_rescue_runs_local/group2_hard_wave_20260727_post_followup_scheduler_binary.log"
RUN_LOG_DIR="$ROOT/codex_rescue_runs_local/queue_logs_group2_hard_wave_20260727_post_followup_binary"
SNAPSHOT_JSON="$WAVE_DIR/group2_hard_status_snapshot.json"
CONSISTENCY_CHECK_JSON="$WAVE_DIR/consistency_check.json"
SERVER_URL="http://127.0.0.1:${GROUP2_HARD_BINARY_PORT:-18668}"
POCDB_PATH="$ROOT/server_poc_group2_binary/poc.db"
MAX_ACTIVE_RUNNERS="${GROUP2_HARD_POST_MAX_ACTIVE:-6}"
POLL_SECONDS="${GROUP2_HARD_POST_POLL_SECONDS:-15}"
ACTIVE_RUNNER_OUTPUT_ROOT="./codex_rescue_runs_local/group2_hard_wave_20260727_single"
IGNORE_USAGE_LIMIT="${GROUP2_HARD_IGNORE_USAGE_LIMIT:-1}"
GLOBAL_MAX_ACTIVE_RUNNERS="${GROUP2_HARD_GLOBAL_MAX_ACTIVE:-16}"
GLOBAL_RUNNER_HEADROOM="${GROUP2_HARD_GLOBAL_RUNNER_HEADROOM:-2}"
LOADAVG_LIMIT_FACTOR="${GROUP2_HARD_LOADAVG_LIMIT_FACTOR:-1.25}"
MIN_MEM_AVAILABLE_MB="${GROUP2_HARD_MIN_MEM_AVAILABLE_MB:-4096}"
MIN_SWAP_FREE_MB="${GROUP2_HARD_MIN_SWAP_FREE_MB:-512}"

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

launcher_has_desired_config() {
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

stop_pid() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  kill "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.25
  done
  kill -9 "$pid" 2>/dev/null || true
}

launcher_pid() {
  local pid=""
  if [[ -f "$LAUNCHER_PID_FILE" ]]; then
    pid="$(cat "$LAUNCHER_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      printf '%s\n' "$pid"
      return 0
    fi
  fi
  sync_pid_file_from_process_match \
    "$LAUNCHER_PID_FILE" \
    "scripts/rescue_queue_launcher.py" \
    "group2_hard_wave_2026-07-27/post_followup_retry_queue.json" >/dev/null || true
  if [[ -f "$LAUNCHER_PID_FILE" ]]; then
    pid="$(cat "$LAUNCHER_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      printf '%s\n' "$pid"
      return 0
    fi
  fi
  return 1
}

launcher_running() {
  launcher_pid >/dev/null 2>&1
}

refresh() {
  bash "$ROOT/scripts/control_group2_hard_followup.sh" refresh >/dev/null
  echo "$QUEUE_SUMMARY_JSON"
}

status() {
  "$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import json
from pathlib import Path

snapshot_path = Path("reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.json")
summary_path = Path("reports/group2_hard_wave_2026-07-27/post_followup_retry_summary.json")
state_path = Path("reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.state.json")
consistency_path = Path("reports/group2_hard_wave_2026-07-27/consistency_check.json")
if not snapshot_path.exists():
    raise SystemExit("missing snapshot: run refresh first")
snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
consistency = json.loads(consistency_path.read_text(encoding="utf-8")) if consistency_path.exists() else {}
followup = snapshot.get("followup_queue") or {}
print(f"generated_at={snapshot.get('generated_at')}")
print(f"followup_remaining={followup.get('queue_remaining_count')}")
print(f"followup_queue_complete={bool(followup.get('queue_complete'))}")
print(f"post_followup_ready={summary.get('task_count')}")
print(f"post_followup_prior_status_counts={json.dumps(summary.get('prior_status_counts', {}), sort_keys=True)}")
print(f"post_followup_processed={len((state.get('processed_names') or []))}")
print(f"post_followup_queue_complete={bool(state.get('queue_complete'))}")
print(f"post_followup_remaining={snapshot.get('post_followup_remaining')}")
print(f"next_post_followup_task={snapshot.get('next_post_followup_task')}")
print(f"next_auto_action={(snapshot.get('automation') or {}).get('next_auto_action')}")
print(f"consistency_ok={consistency.get('ok')}")
print(f"consistency_errors={json.dumps(consistency.get('errors', []), sort_keys=True)}")
PY
  echo "post_followup_launcher_running=$(launcher_running && echo True || echo False)"
  echo "post_followup_launcher_pid=$(launcher_pid || echo none)"
}

pause() {
  local pid
  pid="$(launcher_pid || true)"
  if [[ -z "$pid" ]]; then
    echo "post-followup launcher already stopped"
    return 0
  fi
  kill "$pid"
  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "stopped post-followup launcher pid=$pid"
      return 0
    fi
    sleep 0.25
  done
  kill -9 "$pid" 2>/dev/null || true
  echo "force_stopped post-followup launcher pid=$pid"
}

resume() {
  local pid=""
  pid="$(launcher_pid || true)"
  if [[ -n "$pid" ]]; then
    if launcher_has_desired_config "$pid"; then
      echo "post-followup launcher already running pid=$pid"
      return 0
    fi
    echo "restarting post-followup launcher pid=$pid due_to=launcher_config_mismatch"
    stop_pid "$pid"
    rm -f "$LAUNCHER_PID_FILE"
  fi
  "$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import json
from pathlib import Path

snapshot_path = Path("reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.json")
summary_path = Path("reports/group2_hard_wave_2026-07-27/post_followup_retry_summary.json")
consistency_path = Path("reports/group2_hard_wave_2026-07-27/consistency_check.json")
if not snapshot_path.exists():
    raise SystemExit("missing snapshot: run refresh first")
if not summary_path.exists():
    raise SystemExit("missing post-followup summary: run refresh first")
snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
summary = json.loads(summary_path.read_text(encoding="utf-8"))
consistency = json.loads(consistency_path.read_text(encoding="utf-8")) if consistency_path.exists() else {}
followup = snapshot.get("followup_queue") or {}
if not followup.get("queue_complete"):
    raise SystemExit(
        f"followup queue not complete: remaining={followup.get('queue_remaining_count')}"
    )
if int(summary.get("task_count") or 0) <= 0:
    raise SystemExit("post-followup queue is empty")
if consistency_path.exists() and not bool(consistency.get("ok")):
    raise SystemExit(
        f"consistency check failed: {json.dumps(consistency.get('errors', []), sort_keys=True)}"
    )
PY

  local ignore_usage_limit_args=()
  if [[ "$IGNORE_USAGE_LIMIT" == "1" ]]; then
    ignore_usage_limit_args+=(--ignore-usage-limit)
  fi
  mkdir -p "$RUN_LOG_DIR"
  : >"$LAUNCHER_LOG"
  setsid -f "$PYTHON_BIN" "$ROOT/scripts/rescue_queue_launcher.py" \
    --queue-file "$QUEUE_FILE" \
    --server "$SERVER_URL" \
    --data-dir "$ROOT/cybergym_data/data" \
    --pocdb-path "$POCDB_PATH" \
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
    --state-file "$QUEUE_STATE_FILE" \
    --active-runner-output-root "$ACTIVE_RUNNER_OUTPUT_ROOT" \
    "${ignore_usage_limit_args[@]}" \
    >>"$LAUNCHER_LOG" 2>&1
  sync_pid_file_from_process_match \
    "$LAUNCHER_PID_FILE" \
    "scripts/rescue_queue_launcher.py" \
    "group2_hard_wave_2026-07-27/post_followup_retry_queue.json" >/dev/null || true
  echo "started post-followup launcher pid=$(cat "$LAUNCHER_PID_FILE" 2>/dev/null || echo unknown)"
}

case "$ACTION" in
  refresh)
    refresh
    ;;
  status)
    status
    ;;
  pause)
    pause
    ;;
  resume)
    resume
    ;;
  *)
    echo "unknown action: $ACTION" >&2
    exit 1
    ;;
esac
