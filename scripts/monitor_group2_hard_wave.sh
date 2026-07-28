#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="$ROOT/.venv/bin/python"
WAVE_DIR="$ROOT/reports/group2_hard_wave_2026-07-27"
INTERVAL_SECONDS="${GROUP2_HARD_MONITOR_INTERVAL:-120}"
INDEX_JSONL="$ROOT/codex_rescue_runs_local/trajectory_index.jsonl"
SUMMARY_JSON="$ROOT/codex_rescue_runs_local/trajectory_summary.json"
LOG_PATH="$WAVE_DIR/monitor.log"
LIVE_SUMMARY_JSON="$WAVE_DIR/group2_live_summary.json"
WAVE_BINARY_STATUS_JSON="$WAVE_DIR/group2_binary_wave_status.json"
ACTIVE_RUNNER_METRICS_JSON="$WAVE_DIR/group2_active_runner_metrics.json"
INTERVENTION_WATCH_JSON="$WAVE_DIR/group2_intervention_watch.json"
HARD_STATUS_SNAPSHOT_JSON="$WAVE_DIR/group2_hard_status_snapshot.json"
HARD_STATUS_TSV="$WAVE_DIR/group2_hard_task_status.tsv"
INTERMEDIATE_STATUS_SNAPSHOT_JSON="$WAVE_DIR/group2_hard_status_snapshot.intermediate.json"
INTERMEDIATE_STATUS_TSV="$WAVE_DIR/group2_hard_task_status.intermediate.tsv"
MONITOR_PID_FILE="$WAVE_DIR/monitor.pid"
PENDING_FOLLOWUP_JSON="$WAVE_DIR/followup_pending_tasks.json"
PENDING_FOLLOWUP_TSV="$WAVE_DIR/followup_pending_tasks.tsv"
NEXT_WAVE_REPORT_MD="$WAVE_DIR/next_wave_status.md"
RESUME_PACKET_JSON="$WAVE_DIR/resume_packet.json"
RESUME_PACKET_MD="$WAVE_DIR/resume_packet.md"
CONSISTENCY_CHECK_JSON="$WAVE_DIR/consistency_check.json"
UNSOLVED_JSON="$WAVE_DIR/unsolved_tasks.json"
UNSOLVED_TSV="$WAVE_DIR/unsolved_tasks.tsv"
POST_FOLLOWUP_EXHAUSTED_JSON="$WAVE_DIR/post_followup_exhausted_tasks.json"
POST_FOLLOWUP_EXHAUSTED_TSV="$WAVE_DIR/post_followup_exhausted_tasks.tsv"
POST_FOLLOWUP_QUEUE_FILE="$WAVE_DIR/post_followup_retry_queue.json"
POST_FOLLOWUP_MANIFEST_DIR="$WAVE_DIR/post_followup_retry_manifests"
POST_FOLLOWUP_SUMMARY_JSON="$WAVE_DIR/post_followup_retry_summary.json"
POST_FOLLOWUP_STATE_FILE="$WAVE_DIR/post_followup_retry_queue.state.json"
POST_FOLLOWUP_LAUNCHER_LOG="$WAVE_DIR/post_followup_queue_launcher.log"
POST_FOLLOWUP_LAUNCHER_PID_FILE="$WAVE_DIR/post_followup_queue_launcher.pid"
POST_FOLLOWUP_SCHEDULER_LOG="$ROOT/codex_rescue_runs_local/group2_hard_wave_20260727_post_followup_scheduler_binary.log"
POST_FOLLOWUP_RUN_LOG_DIR="$ROOT/codex_rescue_runs_local/queue_logs_group2_hard_wave_20260727_post_followup_binary"
POCDB_PATH="$ROOT/server_poc_group2_binary/poc.db"
STATE_FILE="$WAVE_DIR/combined_queue.binary.state.json"
RETRY_DIR="$WAVE_DIR/retry_after_binary_fix"
TASK_SOURCE_FILE="$WAVE_DIR/group2_hard_tasks.json"
RUNS_DIR="$ROOT/codex_rescue_runs_local/group2_hard_wave_20260727_single"
ACTIVE_RUNNER_OUTPUT_ROOT="./codex_rescue_runs_local/group2_hard_wave_20260727_single"
FOLLOWUP_QUEUE_FILE="$WAVE_DIR/followup_retry_queue.json"
FOLLOWUP_SUMMARY_FILE="$WAVE_DIR/followup_retry_summary.json"
FOLLOWUP_STATE_FILE="$WAVE_DIR/followup_retry_queue.state.json"
FOLLOWUP_LAUNCHER_LOG="$WAVE_DIR/followup_queue_launcher.log"
FOLLOWUP_LAUNCHER_PID_FILE="$WAVE_DIR/followup_queue_launcher.pid"
FOLLOWUP_SCHEDULER_LOG="$ROOT/codex_rescue_runs_local/group2_hard_wave_20260727_followup_scheduler_binary.log"
FOLLOWUP_RUN_LOG_DIR="$ROOT/codex_rescue_runs_local/queue_logs_group2_hard_wave_20260727_followup_binary"
POST_MAIN_PREVIEW_QUEUE_FILE="$WAVE_DIR/post_main_static_retry_queue.json"
POST_MAIN_PREVIEW_SUMMARY_FILE="$WAVE_DIR/post_main_static_retry_summary.json"
POST_MAIN_PREVIEW_MANIFEST_DIR="$WAVE_DIR/post_main_static_retry_manifests"
MAX_ACTIVE_RUNNERS="${GROUP2_HARD_MAX_ACTIVE:-6}"
POST_MAX_ACTIVE_RUNNERS="${GROUP2_HARD_POST_MAX_ACTIVE:-6}"
IGNORE_USAGE_LIMIT="${GROUP2_HARD_IGNORE_USAGE_LIMIT:-1}"
GLOBAL_MAX_ACTIVE_RUNNERS="${GROUP2_HARD_GLOBAL_MAX_ACTIVE:-16}"
GLOBAL_RUNNER_HEADROOM="${GROUP2_HARD_GLOBAL_RUNNER_HEADROOM:-2}"
LOADAVG_LIMIT_FACTOR="${GROUP2_HARD_LOADAVG_LIMIT_FACTOR:-1.25}"
MIN_MEM_AVAILABLE_MB="${GROUP2_HARD_MIN_MEM_AVAILABLE_MB:-4096}"
MIN_SWAP_FREE_MB="${GROUP2_HARD_MIN_SWAP_FREE_MB:-512}"

mkdir -p "$WAVE_DIR"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "$LOG_PATH"
}

write_monitor_pid() {
  printf '%s\n' "$BASHPID" >"$MONITOR_PID_FILE"
}

cleanup_monitor_pid() {
  local pid=""
  if [[ -f "$MONITOR_PID_FILE" ]]; then
    pid="$(cat "$MONITOR_PID_FILE" 2>/dev/null || true)"
  fi
  if [[ "$pid" == "$BASHPID" ]]; then
    rm -f "$MONITOR_PID_FILE"
  fi
}

write_monitor_pid
trap cleanup_monitor_pid EXIT

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

launcher_has_desired_config() {
  local pid="$1"
  local max_active="$2"
  local args
  args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  [[ "$args" == *"--max-active-runners $max_active"* ]] &&
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

followup_queue_item_count() {
  "$PYTHON_BIN" - <<'PY'
import json
from pathlib import Path
path = Path("reports/group2_hard_wave_2026-07-27/followup_retry_queue.json")
if not path.exists():
    print(0)
    raise SystemExit(0)
payload = json.loads(path.read_text(encoding="utf-8"))
items = payload.get("items", []) if isinstance(payload, dict) else payload
print(len(items) if isinstance(items, list) else 0)
PY
}

post_followup_queue_item_count() {
  "$PYTHON_BIN" - <<'PY'
import json
from pathlib import Path
path = Path("reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.json")
if not path.exists():
    print(0)
    raise SystemExit(0)
payload = json.loads(path.read_text(encoding="utf-8"))
items = payload.get("items", []) if isinstance(payload, dict) else payload
print(len(items) if isinstance(items, list) else 0)
PY
}

main_queue_complete() {
  "$PYTHON_BIN" - <<'PY'
import json
from pathlib import Path
path = Path("reports/group2_hard_wave_2026-07-27/combined_queue.binary.state.json")
if not path.exists():
    raise SystemExit(1)
payload = json.loads(path.read_text(encoding="utf-8"))
raise SystemExit(0 if payload.get("queue_complete") else 1)
PY
}

followup_queue_complete() {
  "$PYTHON_BIN" - <<'PY'
import json
from pathlib import Path
path = Path("reports/group2_hard_wave_2026-07-27/followup_retry_queue.state.json")
if not path.exists():
    raise SystemExit(1)
payload = json.loads(path.read_text(encoding="utf-8"))
raise SystemExit(0 if payload.get("queue_complete") else 1)
PY
}

post_followup_queue_complete() {
  "$PYTHON_BIN" - <<'PY'
import json
from pathlib import Path
path = Path("reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.state.json")
if not path.exists():
    raise SystemExit(1)
payload = json.loads(path.read_text(encoding="utf-8"))
raise SystemExit(0 if payload.get("queue_complete") else 1)
PY
}

active_main_runner_count() {
  grep -E -c "scripts/codex_rescue_runner.py run --manifest reports/group2_hard_wave_2026-07-27/(fresh_manifests|retry_manifests)/" < <(ps -eo args=) || true
}

followup_usage_limit_halt_active() {
  if [[ "$IGNORE_USAGE_LIMIT" == "1" ]]; then
    return 1
  fi
  "$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import re
from datetime import datetime, UTC
from pathlib import Path

log_path = Path("codex_rescue_runs_local/group2_hard_wave_20260727_followup_scheduler_binary.log")
if not log_path.exists():
    raise SystemExit(1)

pattern = re.compile(r"halting: usage_limit_detected .* reset_at=(\S+)")
reset_at = None
for line in reversed(log_path.read_text(encoding="utf-8", errors="ignore").splitlines()):
    match = pattern.search(line)
    if match is None:
        continue
    raw = match.group(1)
    try:
        reset_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        reset_at = None
    break

if reset_at is None:
    raise SystemExit(1)

if reset_at.tzinfo is None:
    reset_at = reset_at.replace(tzinfo=UTC)

raise SystemExit(0 if reset_at > datetime.now(UTC) else 1)
PY
}

followup_launcher_running() {
  if [[ -f "$FOLLOWUP_LAUNCHER_PID_FILE" ]]; then
    local pid
    pid="$(cat "$FOLLOWUP_LAUNCHER_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      if ! launcher_has_desired_config "$pid" "$MAX_ACTIVE_RUNNERS"; then
        log "stopping stale follow-up launcher pid=$pid due_to=launcher_config_mismatch desired_max_active=$MAX_ACTIVE_RUNNERS desired_global_max=$GLOBAL_MAX_ACTIVE_RUNNERS"
        stop_pid "$pid"
        rm -f "$FOLLOWUP_LAUNCHER_PID_FILE"
        return 1
      fi
      return 0
    fi
  fi
  local matched_pid
  matched_pid="$(find_process_pid_by_substrings \
    "scripts/rescue_queue_launcher.py" \
    "group2_hard_wave_2026-07-27/followup_retry_queue.json" || true)"
  if [[ -n "$matched_pid" ]]; then
    if ! launcher_has_desired_config "$matched_pid" "$MAX_ACTIVE_RUNNERS"; then
      log "stopping stale follow-up launcher pid=$matched_pid due_to=launcher_config_mismatch desired_max_active=$MAX_ACTIVE_RUNNERS desired_global_max=$GLOBAL_MAX_ACTIVE_RUNNERS"
      stop_pid "$matched_pid"
      rm -f "$FOLLOWUP_LAUNCHER_PID_FILE"
      return 1
    fi
    printf '%s\n' "$matched_pid" >"$FOLLOWUP_LAUNCHER_PID_FILE"
    return 0
  fi
  return 1
}

post_followup_launcher_running() {
  if [[ -f "$POST_FOLLOWUP_LAUNCHER_PID_FILE" ]]; then
    local pid
    pid="$(cat "$POST_FOLLOWUP_LAUNCHER_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      if ! launcher_has_desired_config "$pid" "$POST_MAX_ACTIVE_RUNNERS"; then
        log "stopping stale post-followup launcher pid=$pid due_to=launcher_config_mismatch desired_max_active=$POST_MAX_ACTIVE_RUNNERS desired_global_max=$GLOBAL_MAX_ACTIVE_RUNNERS"
        stop_pid "$pid"
        rm -f "$POST_FOLLOWUP_LAUNCHER_PID_FILE"
        return 1
      fi
      return 0
    fi
  fi
  local matched_pid
  matched_pid="$(find_process_pid_by_substrings \
    "scripts/rescue_queue_launcher.py" \
    "group2_hard_wave_2026-07-27/post_followup_retry_queue.json" || true)"
  if [[ -n "$matched_pid" ]]; then
    if ! launcher_has_desired_config "$matched_pid" "$POST_MAX_ACTIVE_RUNNERS"; then
      log "stopping stale post-followup launcher pid=$matched_pid due_to=launcher_config_mismatch desired_max_active=$POST_MAX_ACTIVE_RUNNERS desired_global_max=$GLOBAL_MAX_ACTIVE_RUNNERS"
      stop_pid "$matched_pid"
      rm -f "$POST_FOLLOWUP_LAUNCHER_PID_FILE"
      return 1
    fi
    printf '%s\n' "$matched_pid" >"$POST_FOLLOWUP_LAUNCHER_PID_FILE"
    return 0
  fi
  return 1
}

start_followup_launcher() {
  local ignore_usage_limit_args=()
  if [[ "$IGNORE_USAGE_LIMIT" == "1" ]]; then
    ignore_usage_limit_args+=(--ignore-usage-limit)
  fi
  mkdir -p "$FOLLOWUP_RUN_LOG_DIR"
  : >"$FOLLOWUP_LAUNCHER_LOG"
  setsid -f "$PYTHON_BIN" "$ROOT/scripts/rescue_queue_launcher.py" \
    --queue-file "$FOLLOWUP_QUEUE_FILE" \
    --server "http://127.0.0.1:${GROUP2_HARD_BINARY_PORT:-18668}" \
    --data-dir "$ROOT/cybergym_data/data" \
    --pocdb-path "$POCDB_PATH" \
    --python-bin "$PYTHON_BIN" \
    --max-active-runners "$MAX_ACTIVE_RUNNERS" \
    --global-max-active-runners "$GLOBAL_MAX_ACTIVE_RUNNERS" \
    --global-runner-headroom "$GLOBAL_RUNNER_HEADROOM" \
    --loadavg-limit-factor "$LOADAVG_LIMIT_FACTOR" \
    --min-mem-available-mb "$MIN_MEM_AVAILABLE_MB" \
    --min-swap-free-mb "$MIN_SWAP_FREE_MB" \
    --poll-seconds 15 \
    --scheduler-log "$FOLLOWUP_SCHEDULER_LOG" \
    --run-log-dir "$FOLLOWUP_RUN_LOG_DIR" \
    --results-root "$ROOT/codex_rescue_runs_local" \
    --state-file "$FOLLOWUP_STATE_FILE" \
    --active-runner-output-root "$ACTIVE_RUNNER_OUTPUT_ROOT" \
    "${ignore_usage_limit_args[@]}" \
    >>"$FOLLOWUP_LAUNCHER_LOG" 2>&1
  find_process_pid_by_substrings \
    "scripts/rescue_queue_launcher.py" \
    "group2_hard_wave_2026-07-27/followup_retry_queue.json" \
    >"$FOLLOWUP_LAUNCHER_PID_FILE" || true
  log "started follow-up retry queue launcher pid=$(cat "$FOLLOWUP_LAUNCHER_PID_FILE" 2>/dev/null || echo unknown)"
}

start_post_followup_launcher() {
  local ignore_usage_limit_args=()
  if [[ "$IGNORE_USAGE_LIMIT" == "1" ]]; then
    ignore_usage_limit_args+=(--ignore-usage-limit)
  fi
  mkdir -p "$POST_FOLLOWUP_RUN_LOG_DIR"
  : >"$POST_FOLLOWUP_LAUNCHER_LOG"
  setsid -f "$PYTHON_BIN" "$ROOT/scripts/rescue_queue_launcher.py" \
    --queue-file "$POST_FOLLOWUP_QUEUE_FILE" \
    --server "http://127.0.0.1:${GROUP2_HARD_BINARY_PORT:-18668}" \
    --data-dir "$ROOT/cybergym_data/data" \
    --pocdb-path "$POCDB_PATH" \
    --python-bin "$PYTHON_BIN" \
    --max-active-runners "$POST_MAX_ACTIVE_RUNNERS" \
    --global-max-active-runners "$GLOBAL_MAX_ACTIVE_RUNNERS" \
    --global-runner-headroom "$GLOBAL_RUNNER_HEADROOM" \
    --loadavg-limit-factor "$LOADAVG_LIMIT_FACTOR" \
    --min-mem-available-mb "$MIN_MEM_AVAILABLE_MB" \
    --min-swap-free-mb "$MIN_SWAP_FREE_MB" \
    --poll-seconds 15 \
    --scheduler-log "$POST_FOLLOWUP_SCHEDULER_LOG" \
    --run-log-dir "$POST_FOLLOWUP_RUN_LOG_DIR" \
    --results-root "$ROOT/codex_rescue_runs_local" \
    --state-file "$POST_FOLLOWUP_STATE_FILE" \
    --active-runner-output-root "$ACTIVE_RUNNER_OUTPUT_ROOT" \
    "${ignore_usage_limit_args[@]}" \
    >>"$POST_FOLLOWUP_LAUNCHER_LOG" 2>&1
  find_process_pid_by_substrings \
    "scripts/rescue_queue_launcher.py" \
    "group2_hard_wave_2026-07-27/post_followup_retry_queue.json" \
    >"$POST_FOLLOWUP_LAUNCHER_PID_FILE" || true
  log "started post-followup retry queue launcher pid=$(cat "$POST_FOLLOWUP_LAUNCHER_PID_FILE" 2>/dev/null || echo unknown)"
}

rebuild_followup_queue() {
  if [[ ! -f "$TASK_SOURCE_FILE" ]]; then
    log "follow-up rebuild skipped: missing task source $TASK_SOURCE_FILE"
    return 1
  fi

  "$PYTHON_BIN" "$ROOT/scripts/build_static_retry_queue.py" \
    --trajectory-index "$INDEX_JSONL" \
    --task-source "$TASK_SOURCE_FILE" \
    --results-root "$ROOT/codex_rescue_runs_local" \
    --output-manifest-dir "$WAVE_DIR/followup_retry_manifests" \
    --output-queue-file "$FOLLOWUP_QUEUE_FILE" \
    --output-root "./codex_rescue_runs_local/group2_hard_wave_20260727_single" \
    --server "http://127.0.0.1:${GROUP2_HARD_BINARY_PORT:-18668}" \
    --data-dir "./cybergym_data/data" \
    --difficulty level1 \
    --campaign group2_hard_post_main_static_retry \
    --codex-bin codex \
    --codex-timeout-seconds 5400 \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/export_static_retry_queue_summary.py" \
    --queue-file "$FOLLOWUP_QUEUE_FILE" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --output-json "$FOLLOWUP_SUMMARY_FILE" \
    >>"$LOG_PATH" 2>&1
}

refresh_followup_queue_if_idle() {
  if followup_launcher_running; then
    return
  fi
  if [[ -f "$FOLLOWUP_STATE_FILE" ]]; then
    return
  fi
  rebuild_followup_queue || return
}

refresh_followup_summary() {
  if [[ ! -f "$FOLLOWUP_QUEUE_FILE" ]]; then
    return 0
  fi
  "$PYTHON_BIN" "$ROOT/scripts/export_static_retry_queue_summary.py" \
    --queue-file "$FOLLOWUP_QUEUE_FILE" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --output-json "$FOLLOWUP_SUMMARY_FILE" \
    >>"$LOG_PATH" 2>&1
}

reprioritize_followup_queue() {
  if [[ ! -f "$FOLLOWUP_QUEUE_FILE" ]]; then
    return 0
  fi
  "$PYTHON_BIN" "$ROOT/scripts/reprioritize_static_queue.py" \
    --queue-file "$FOLLOWUP_QUEUE_FILE" \
    --state-file "$FOLLOWUP_STATE_FILE" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --status-priority "fix_also_crashes,no_submission,invalid_result,codex_failed,no_vul_crash" \
    >>"$LOG_PATH" 2>&1
  refresh_followup_summary
}

refresh_post_main_retry_preview() {
  if [[ ! -f "$TASK_SOURCE_FILE" ]]; then
    log "post-main preview rebuild skipped: missing task source $TASK_SOURCE_FILE"
    return 1
  fi

  "$PYTHON_BIN" "$ROOT/scripts/build_static_retry_queue.py" \
    --trajectory-index "$INDEX_JSONL" \
    --task-source "$TASK_SOURCE_FILE" \
    --results-root "$ROOT/codex_rescue_runs_local" \
    --output-manifest-dir "$POST_MAIN_PREVIEW_MANIFEST_DIR" \
    --output-queue-file "$POST_MAIN_PREVIEW_QUEUE_FILE" \
    --output-root "./codex_rescue_runs_local/group2_hard_wave_20260727_single" \
    --server "http://127.0.0.1:${GROUP2_HARD_BINARY_PORT:-18668}" \
    --data-dir "./cybergym_data/data" \
    --difficulty level1 \
    --campaign group2_hard_post_main_static_retry \
    --codex-bin codex \
    --codex-timeout-seconds 5400 \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/export_static_retry_queue_summary.py" \
    --queue-file "$POST_MAIN_PREVIEW_QUEUE_FILE" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --output-json "$POST_MAIN_PREVIEW_SUMMARY_FILE" \
    >>"$LOG_PATH" 2>&1
}

maybe_start_followup_queue() {
  if followup_queue_complete; then
    return
  fi
  if followup_usage_limit_halt_active; then
    log "follow-up launcher remains halted by usage limit; skipping restart"
    return
  fi
  if followup_launcher_running; then
    return
  fi
  if ! main_queue_complete; then
    return
  fi
  local active_count
  active_count="$(active_main_runner_count)"
  if [[ "$active_count" -ne 0 ]]; then
    log "main queue complete but waiting for fresh runners to drain count=$active_count before follow-up launch"
    return
  fi
  rebuild_followup_queue || return
  local item_count
  item_count="$(followup_queue_item_count)"
  if [[ "$item_count" -eq 0 ]]; then
    log "follow-up rebuild produced zero retryable tasks"
    return
  fi
  start_followup_launcher
}

maybe_start_post_followup_queue() {
  if ! followup_queue_complete; then
    return
  fi
  if post_followup_queue_complete; then
    return
  fi
  if post_followup_launcher_running; then
    return
  fi
  local item_count
  item_count="$(post_followup_queue_item_count)"
  if [[ "$item_count" -eq 0 ]]; then
    log "post-followup queue currently empty"
    return
  fi
  start_post_followup_launcher
}

refresh_once() {
  write_monitor_pid

  "$PYTHON_BIN" "$ROOT/scripts/refresh_rescue_trajectory_index.py" \
    --results-root "$ROOT/codex_rescue_runs_local" \
    --output-jsonl "$INDEX_JSONL" \
    --summary-json "$SUMMARY_JSON" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/summarize_group_results.py" \
    --trajectory-index "$INDEX_JSONL" \
    --group "$ROOT/splits/group_02.md" \
    --easy-group "$ROOT/splits/group_02_easy.md" \
    --hard-group "$ROOT/splits/group_02_hard.md" \
    --output-json "$LIVE_SUMMARY_JSON" \
    >>"$LOG_PATH" 2>&1

  refresh_followup_queue_if_idle >>"$LOG_PATH" 2>&1
  reprioritize_followup_queue >>"$LOG_PATH" 2>&1

  refresh_post_main_retry_preview >>"$LOG_PATH" 2>&1

  maybe_start_followup_queue >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/summarize_group2_hard_binary_wave.py" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --pocdb-path "$POCDB_PATH" \
    --state-file "$STATE_FILE" \
    --retry-dir "$RETRY_DIR" \
    --followup-queue-file "$FOLLOWUP_QUEUE_FILE" \
    --followup-state-file "$FOLLOWUP_STATE_FILE" \
    --followup-launcher-pid-file "$FOLLOWUP_LAUNCHER_PID_FILE" \
    --followup-scheduler-log "$FOLLOWUP_SCHEDULER_LOG" \
    --post-main-preview-queue-file "$POST_MAIN_PREVIEW_QUEUE_FILE" \
    --post-main-preview-summary-file "$POST_MAIN_PREVIEW_SUMMARY_FILE" \
    --output-json "$WAVE_BINARY_STATUS_JSON" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_active_runner_metrics.py" \
    --wave-dir "$WAVE_DIR" \
    --runs-dir "$RUNS_DIR" \
    --pocdb-path "$POCDB_PATH" \
    --output-json "$ACTIVE_RUNNER_METRICS_JSON" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_intervention_watch.py" \
    --wave-dir "$WAVE_DIR" \
    --runs-dir "$RUNS_DIR" \
    --pocdb-path "$POCDB_PATH" \
    --output-json "$INTERVENTION_WATCH_JSON" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/backfill_queue_state_task_ids.py" \
    --queue-file "$FOLLOWUP_QUEUE_FILE" \
    --state-file "$FOLLOWUP_STATE_FILE" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/backfill_queue_state_task_ids.py" \
    --queue-file "$POST_FOLLOWUP_QUEUE_FILE" \
    --state-file "$POST_FOLLOWUP_STATE_FILE" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_pending_followup.py" \
    --queue-file "$FOLLOWUP_QUEUE_FILE" \
    --state-file "$FOLLOWUP_STATE_FILE" \
    --summary-file "$FOLLOWUP_SUMMARY_FILE" \
    --output-json "$PENDING_FOLLOWUP_JSON" \
    --output-tsv "$PENDING_FOLLOWUP_TSV" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_status_snapshot.py" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --binary-wave-status "$WAVE_BINARY_STATUS_JSON" \
    --queue-state "$STATE_FILE" \
    --queue-file "$WAVE_DIR/combined_queue.json" \
    --followup-queue-state "$FOLLOWUP_STATE_FILE" \
    --followup-queue-file "$FOLLOWUP_QUEUE_FILE" \
    --post-followup-queue-state "$POST_FOLLOWUP_STATE_FILE" \
    --post-followup-queue-file "$POST_FOLLOWUP_QUEUE_FILE" \
    --active-runner-metrics "$ACTIVE_RUNNER_METRICS_JSON" \
    --monitor-pid-file "$MONITOR_PID_FILE" \
    --post-followup-launcher-pid-file "$POST_FOLLOWUP_LAUNCHER_PID_FILE" \
    --output-json "$INTERMEDIATE_STATUS_SNAPSHOT_JSON" \
    --output-tsv "$INTERMEDIATE_STATUS_TSV" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_post_followup_retry.py" \
    --snapshot "$INTERMEDIATE_STATUS_SNAPSHOT_JSON" \
    --snapshot-label "$HARD_STATUS_SNAPSHOT_JSON" \
    --pending-followup "$PENDING_FOLLOWUP_JSON" \
    --trajectory-index "$INDEX_JSONL" \
    --results-root "$ROOT/codex_rescue_runs_local" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --output-exhausted-json "$POST_FOLLOWUP_EXHAUSTED_JSON" \
    --output-exhausted-tsv "$POST_FOLLOWUP_EXHAUSTED_TSV" \
    --output-manifest-dir "$POST_FOLLOWUP_MANIFEST_DIR" \
    --output-queue-file "$POST_FOLLOWUP_QUEUE_FILE" \
    --output-summary-json "$POST_FOLLOWUP_SUMMARY_JSON" \
    --output-root "./codex_rescue_runs_local/group2_hard_wave_20260727_single" \
    --server "http://127.0.0.1:${GROUP2_HARD_BINARY_PORT:-18668}" \
    --data-dir "./cybergym_data/data" \
    --difficulty level1 \
    --campaign group2_hard_post_followup_static_retry \
    >>"$LOG_PATH" 2>&1

  maybe_start_post_followup_queue >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_status_snapshot.py" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --binary-wave-status "$WAVE_BINARY_STATUS_JSON" \
    --queue-state "$STATE_FILE" \
    --queue-file "$WAVE_DIR/combined_queue.json" \
    --followup-queue-state "$FOLLOWUP_STATE_FILE" \
    --followup-queue-file "$FOLLOWUP_QUEUE_FILE" \
    --post-followup-queue-state "$POST_FOLLOWUP_STATE_FILE" \
    --post-followup-queue-file "$POST_FOLLOWUP_QUEUE_FILE" \
    --active-runner-metrics "$ACTIVE_RUNNER_METRICS_JSON" \
    --monitor-pid-file "$MONITOR_PID_FILE" \
    --post-followup-launcher-pid-file "$POST_FOLLOWUP_LAUNCHER_PID_FILE" \
    --output-json "$HARD_STATUS_SNAPSHOT_JSON" \
    --output-tsv "$HARD_STATUS_TSV" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_unsolved_tasks.py" \
    --snapshot "$HARD_STATUS_SNAPSHOT_JSON" \
    --output-json "$UNSOLVED_JSON" \
    --output-tsv "$UNSOLVED_TSV" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/check_group2_hard_wave_consistency.py" \
    --snapshot "$HARD_STATUS_SNAPSHOT_JSON" \
    --resume-packet "$RESUME_PACKET_JSON" \
    --followup-pending "$PENDING_FOLLOWUP_JSON" \
    --followup-queue "$FOLLOWUP_QUEUE_FILE" \
    --followup-state "$FOLLOWUP_STATE_FILE" \
    --post-followup-queue "$POST_FOLLOWUP_QUEUE_FILE" \
    --post-followup-state "$POST_FOLLOWUP_STATE_FILE" \
    --post-followup-summary "$POST_FOLLOWUP_SUMMARY_JSON" \
    --post-followup-exhausted "$POST_FOLLOWUP_EXHAUSTED_JSON" \
    --output-json "$CONSISTENCY_CHECK_JSON" \
    --skip-resume-packet-checks \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_resume_packet.py" \
    --snapshot "$HARD_STATUS_SNAPSHOT_JSON" \
    --consistency-check "$CONSISTENCY_CHECK_JSON" \
    --pending-followup "$PENDING_FOLLOWUP_JSON" \
    --post-followup-summary "$POST_FOLLOWUP_SUMMARY_JSON" \
    --post-followup-queue "$POST_FOLLOWUP_QUEUE_FILE" \
    --output-json "$RESUME_PACKET_JSON" \
    --output-md "$RESUME_PACKET_MD" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/check_group2_hard_wave_consistency.py" \
    --snapshot "$HARD_STATUS_SNAPSHOT_JSON" \
    --resume-packet "$RESUME_PACKET_JSON" \
    --followup-pending "$PENDING_FOLLOWUP_JSON" \
    --followup-queue "$FOLLOWUP_QUEUE_FILE" \
    --followup-state "$FOLLOWUP_STATE_FILE" \
    --post-followup-queue "$POST_FOLLOWUP_QUEUE_FILE" \
    --post-followup-state "$POST_FOLLOWUP_STATE_FILE" \
    --post-followup-summary "$POST_FOLLOWUP_SUMMARY_JSON" \
    --post-followup-exhausted "$POST_FOLLOWUP_EXHAUSTED_JSON" \
    --output-json "$CONSISTENCY_CHECK_JSON" \
    >>"$LOG_PATH" 2>&1

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_next_wave_report.py" \
    --snapshot "$HARD_STATUS_SNAPSHOT_JSON" \
    --pending-followup "$PENDING_FOLLOWUP_JSON" \
    --post-followup-summary "$POST_FOLLOWUP_SUMMARY_JSON" \
    --consistency-check "$CONSISTENCY_CHECK_JSON" \
    --output-md "$NEXT_WAVE_REPORT_MD" \
    >>"$LOG_PATH" 2>&1
}

main() {
  log "group2 hard monitor started interval=${INTERVAL_SECONDS}s"
  while true; do
    refresh_once
    log "group2 hard monitor refreshed -> $LIVE_SUMMARY_JSON"
    sleep "$INTERVAL_SECONDS"
  done
}

main "$@"
