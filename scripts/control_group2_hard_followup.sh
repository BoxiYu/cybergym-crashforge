#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 {refresh|status|pending|pause|resume}" >&2
  exit 1
fi

ACTION="$1"
WAVE_DIR="$ROOT/reports/group2_hard_wave_2026-07-27"
PYTHON_BIN="$ROOT/.venv/bin/python"
TRAJECTORY_INDEX="$ROOT/codex_rescue_runs_local/trajectory_index.jsonl"
LIVE_SUMMARY_JSON="$WAVE_DIR/group2_live_summary.json"
WAVE_BINARY_STATUS_JSON="$WAVE_DIR/group2_binary_wave_status.json"
ACTIVE_RUNNER_METRICS_JSON="$WAVE_DIR/group2_active_runner_metrics.json"
INTERVENTION_WATCH_JSON="$WAVE_DIR/group2_intervention_watch.json"
HARD_STATUS_SNAPSHOT_JSON="$WAVE_DIR/group2_hard_status_snapshot.json"
HARD_STATUS_TSV="$WAVE_DIR/group2_hard_task_status.tsv"
INTERMEDIATE_STATUS_SNAPSHOT_JSON="$WAVE_DIR/group2_hard_status_snapshot.intermediate.json"
INTERMEDIATE_STATUS_TSV="$WAVE_DIR/group2_hard_task_status.intermediate.tsv"
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
FOLLOWUP_QUEUE_FILE="$WAVE_DIR/followup_retry_queue.json"
FOLLOWUP_SUMMARY_JSON="$WAVE_DIR/followup_retry_summary.json"
FOLLOWUP_STATE_FILE="$WAVE_DIR/followup_retry_queue.state.json"
FOLLOWUP_LAUNCHER_LOG="$WAVE_DIR/followup_queue_launcher.log"
FOLLOWUP_LAUNCHER_PID_FILE="$WAVE_DIR/followup_queue_launcher.pid"
FOLLOWUP_SCHEDULER_LOG="$ROOT/codex_rescue_runs_local/group2_hard_wave_20260727_followup_scheduler_binary.log"
FOLLOWUP_RUN_LOG_DIR="$ROOT/codex_rescue_runs_local/queue_logs_group2_hard_wave_20260727_followup_binary"
ACTIVE_RUNNER_OUTPUT_ROOT="./codex_rescue_runs_local/group2_hard_wave_20260727_single"
MONITOR_PID_FILE="$WAVE_DIR/monitor.pid"
POST_FOLLOWUP_LAUNCHER_PID_FILE="$WAVE_DIR/post_followup_queue_launcher.pid"
POCDB_PATH="$ROOT/server_poc_group2_binary/poc.db"
SERVER_PORT="${GROUP2_HARD_BINARY_PORT:-18668}"
SERVER_URL="http://127.0.0.1:${SERVER_PORT}"
MAX_ACTIVE_RUNNERS="${GROUP2_HARD_MAX_ACTIVE:-6}"
POLL_SECONDS="${GROUP2_HARD_POLL_SECONDS:-15}"
IGNORE_USAGE_LIMIT="${GROUP2_HARD_IGNORE_USAGE_LIMIT:-1}"
GLOBAL_MAX_ACTIVE_RUNNERS="${GROUP2_HARD_GLOBAL_MAX_ACTIVE:-16}"
GLOBAL_RUNNER_HEADROOM="${GROUP2_HARD_GLOBAL_RUNNER_HEADROOM:-2}"
LOADAVG_LIMIT_FACTOR="${GROUP2_HARD_LOADAVG_LIMIT_FACTOR:-1.25}"
MIN_MEM_AVAILABLE_MB="${GROUP2_HARD_MIN_MEM_AVAILABLE_MB:-4096}"
MIN_SWAP_FREE_MB="${GROUP2_HARD_MIN_SWAP_FREE_MB:-512}"

refresh_followup_summary() {
  if [[ ! -f "$FOLLOWUP_QUEUE_FILE" ]]; then
    return 0
  fi
  "$PYTHON_BIN" "$ROOT/scripts/export_static_retry_queue_summary.py" \
    --queue-file "$FOLLOWUP_QUEUE_FILE" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --output-json "$FOLLOWUP_SUMMARY_JSON" \
    >/dev/null
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
    >/dev/null
  refresh_followup_summary
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
  if [[ -f "$FOLLOWUP_LAUNCHER_PID_FILE" ]]; then
    pid="$(cat "$FOLLOWUP_LAUNCHER_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      printf '%s\n' "$pid"
      return 0
    fi
  fi
  sync_pid_file_from_process_match \
    "$FOLLOWUP_LAUNCHER_PID_FILE" \
    "scripts/rescue_queue_launcher.py" \
    "group2_hard_wave_2026-07-27/followup_retry_queue.json" >/dev/null || true
  if [[ -f "$FOLLOWUP_LAUNCHER_PID_FILE" ]]; then
    pid="$(cat "$FOLLOWUP_LAUNCHER_PID_FILE" 2>/dev/null || true)"
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

sync_monitor_pid() {
  sync_pid_file_from_process_match \
    "$MONITOR_PID_FILE" \
    "scripts/monitor_group2_hard_wave.sh" >/dev/null || true
}

refresh() {
  sync_monitor_pid

  "$PYTHON_BIN" "$ROOT/scripts/summarize_group_results.py" \
    --trajectory-index "$TRAJECTORY_INDEX" \
    --group "$ROOT/splits/group_02.md" \
    --easy-group "$ROOT/splits/group_02_easy.md" \
    --hard-group "$ROOT/splits/group_02_hard.md" \
    --output-json "$LIVE_SUMMARY_JSON" \
    >/dev/null

  reprioritize_followup_queue

  "$PYTHON_BIN" "$ROOT/scripts/summarize_group2_hard_binary_wave.py" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --pocdb-path "$POCDB_PATH" \
    --state-file "$WAVE_DIR/combined_queue.binary.state.json" \
    --retry-dir "$WAVE_DIR/retry_after_binary_fix" \
    --followup-queue-file "$FOLLOWUP_QUEUE_FILE" \
    --followup-state-file "$FOLLOWUP_STATE_FILE" \
    --followup-launcher-pid-file "$FOLLOWUP_LAUNCHER_PID_FILE" \
    --followup-scheduler-log "$FOLLOWUP_SCHEDULER_LOG" \
    --post-main-preview-queue-file "$WAVE_DIR/post_main_static_retry_queue.json" \
    --post-main-preview-summary-file "$WAVE_DIR/post_main_static_retry_summary.json" \
    --output-json "$WAVE_BINARY_STATUS_JSON" \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_active_runner_metrics.py" \
    --wave-dir "$WAVE_DIR" \
    --runs-dir "$ROOT/codex_rescue_runs_local/group2_hard_wave_20260727_single" \
    --pocdb-path "$POCDB_PATH" \
    --output-json "$ACTIVE_RUNNER_METRICS_JSON" \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_intervention_watch.py" \
    --wave-dir "$WAVE_DIR" \
    --runs-dir "$ROOT/codex_rescue_runs_local/group2_hard_wave_20260727_single" \
    --pocdb-path "$POCDB_PATH" \
    --output-json "$INTERVENTION_WATCH_JSON" \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/backfill_queue_state_task_ids.py" \
    --queue-file "$FOLLOWUP_QUEUE_FILE" \
    --state-file "$FOLLOWUP_STATE_FILE" \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/backfill_queue_state_task_ids.py" \
    --queue-file "$POST_FOLLOWUP_QUEUE_FILE" \
    --state-file "$WAVE_DIR/post_followup_retry_queue.state.json" \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_pending_followup.py" \
    --queue-file "$FOLLOWUP_QUEUE_FILE" \
    --state-file "$FOLLOWUP_STATE_FILE" \
    --summary-file "$WAVE_DIR/followup_retry_summary.json" \
    --output-json "$PENDING_FOLLOWUP_JSON" \
    --output-tsv "$PENDING_FOLLOWUP_TSV" \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_status_snapshot.py" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --binary-wave-status "$WAVE_BINARY_STATUS_JSON" \
    --queue-state "$WAVE_DIR/combined_queue.binary.state.json" \
    --queue-file "$WAVE_DIR/combined_queue.json" \
    --followup-queue-state "$FOLLOWUP_STATE_FILE" \
    --followup-queue-file "$FOLLOWUP_QUEUE_FILE" \
    --post-followup-queue-state "$WAVE_DIR/post_followup_retry_queue.state.json" \
    --post-followup-queue-file "$WAVE_DIR/post_followup_retry_queue.json" \
    --active-runner-metrics "$ACTIVE_RUNNER_METRICS_JSON" \
    --monitor-pid-file "$MONITOR_PID_FILE" \
    --post-followup-launcher-pid-file "$POST_FOLLOWUP_LAUNCHER_PID_FILE" \
    --output-json "$INTERMEDIATE_STATUS_SNAPSHOT_JSON" \
    --output-tsv "$INTERMEDIATE_STATUS_TSV" \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_post_followup_retry.py" \
    --snapshot "$INTERMEDIATE_STATUS_SNAPSHOT_JSON" \
    --snapshot-label "$HARD_STATUS_SNAPSHOT_JSON" \
    --pending-followup "$PENDING_FOLLOWUP_JSON" \
    --trajectory-index "$TRAJECTORY_INDEX" \
    --results-root "$ROOT/codex_rescue_runs_local" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --output-exhausted-json "$POST_FOLLOWUP_EXHAUSTED_JSON" \
    --output-exhausted-tsv "$POST_FOLLOWUP_EXHAUSTED_TSV" \
    --output-manifest-dir "$POST_FOLLOWUP_MANIFEST_DIR" \
    --output-queue-file "$POST_FOLLOWUP_QUEUE_FILE" \
    --output-summary-json "$POST_FOLLOWUP_SUMMARY_JSON" \
    --output-root "./codex_rescue_runs_local/group2_hard_wave_20260727_single" \
    --server "$SERVER_URL" \
    --data-dir "./cybergym_data/data" \
    --difficulty level1 \
    --campaign group2_hard_post_followup_static_retry \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_status_snapshot.py" \
    --live-summary "$LIVE_SUMMARY_JSON" \
    --binary-wave-status "$WAVE_BINARY_STATUS_JSON" \
    --queue-state "$WAVE_DIR/combined_queue.binary.state.json" \
    --queue-file "$WAVE_DIR/combined_queue.json" \
    --followup-queue-state "$FOLLOWUP_STATE_FILE" \
    --followup-queue-file "$FOLLOWUP_QUEUE_FILE" \
    --post-followup-queue-state "$WAVE_DIR/post_followup_retry_queue.state.json" \
    --post-followup-queue-file "$WAVE_DIR/post_followup_retry_queue.json" \
    --active-runner-metrics "$ACTIVE_RUNNER_METRICS_JSON" \
    --monitor-pid-file "$MONITOR_PID_FILE" \
    --post-followup-launcher-pid-file "$POST_FOLLOWUP_LAUNCHER_PID_FILE" \
    --output-json "$HARD_STATUS_SNAPSHOT_JSON" \
    --output-tsv "$HARD_STATUS_TSV" \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_unsolved_tasks.py" \
    --snapshot "$HARD_STATUS_SNAPSHOT_JSON" \
    --output-json "$UNSOLVED_JSON" \
    --output-tsv "$UNSOLVED_TSV" \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/check_group2_hard_wave_consistency.py" \
    --snapshot "$HARD_STATUS_SNAPSHOT_JSON" \
    --resume-packet "$RESUME_PACKET_JSON" \
    --followup-pending "$PENDING_FOLLOWUP_JSON" \
    --followup-queue "$FOLLOWUP_QUEUE_FILE" \
    --followup-state "$FOLLOWUP_STATE_FILE" \
    --post-followup-queue "$POST_FOLLOWUP_QUEUE_FILE" \
    --post-followup-state "$WAVE_DIR/post_followup_retry_queue.state.json" \
    --post-followup-summary "$POST_FOLLOWUP_SUMMARY_JSON" \
    --post-followup-exhausted "$POST_FOLLOWUP_EXHAUSTED_JSON" \
    --output-json "$CONSISTENCY_CHECK_JSON" \
    --skip-resume-packet-checks \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_resume_packet.py" \
    --snapshot "$HARD_STATUS_SNAPSHOT_JSON" \
    --consistency-check "$CONSISTENCY_CHECK_JSON" \
    --pending-followup "$PENDING_FOLLOWUP_JSON" \
    --post-followup-summary "$POST_FOLLOWUP_SUMMARY_JSON" \
    --post-followup-queue "$POST_FOLLOWUP_QUEUE_FILE" \
    --output-json "$RESUME_PACKET_JSON" \
    --output-md "$RESUME_PACKET_MD" \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/check_group2_hard_wave_consistency.py" \
    --snapshot "$HARD_STATUS_SNAPSHOT_JSON" \
    --resume-packet "$RESUME_PACKET_JSON" \
    --followup-pending "$PENDING_FOLLOWUP_JSON" \
    --followup-queue "$FOLLOWUP_QUEUE_FILE" \
    --followup-state "$FOLLOWUP_STATE_FILE" \
    --post-followup-queue "$POST_FOLLOWUP_QUEUE_FILE" \
    --post-followup-state "$WAVE_DIR/post_followup_retry_queue.state.json" \
    --post-followup-summary "$POST_FOLLOWUP_SUMMARY_JSON" \
    --post-followup-exhausted "$POST_FOLLOWUP_EXHAUSTED_JSON" \
    --output-json "$CONSISTENCY_CHECK_JSON" \
    >/dev/null

  "$PYTHON_BIN" "$ROOT/scripts/export_group2_hard_next_wave_report.py" \
    --snapshot "$HARD_STATUS_SNAPSHOT_JSON" \
    --pending-followup "$PENDING_FOLLOWUP_JSON" \
    --post-followup-summary "$POST_FOLLOWUP_SUMMARY_JSON" \
    --consistency-check "$CONSISTENCY_CHECK_JSON" \
    --output-md "$NEXT_WAVE_REPORT_MD" \
    >/dev/null

  echo "$HARD_STATUS_SNAPSHOT_JSON"
}

status() {
  "$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import json
from pathlib import Path

binary_status_path = Path("reports/group2_hard_wave_2026-07-27/group2_binary_wave_status.json")
snapshot_path = Path("reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.json")
consistency_path = Path("reports/group2_hard_wave_2026-07-27/consistency_check.json")
if not snapshot_path.exists():
    raise SystemExit("missing snapshot: run refresh first")
snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
binary_status = json.loads(binary_status_path.read_text(encoding="utf-8")) if binary_status_path.exists() else {}
consistency = json.loads(consistency_path.read_text(encoding="utf-8")) if consistency_path.exists() else {}
halt = snapshot.get("followup_usage_limit_halt") or {}
launcher = (((binary_status.get("followup_retry") or {}).get("launcher")) or {})
launcher_running = launcher.get("running")
launcher_pid = launcher.get("pid") if launcher_running else None
automation = snapshot.get("automation") or {}
followup_head = [
    row.get("task_id")
    for row in (snapshot.get("followup_pending_head") or [])
    if isinstance(row, dict) and row.get("task_id")
]
post_followup_head = [
    row.get("task_id")
    for row in (snapshot.get("post_followup_pending_head") or [])
    if isinstance(row, dict) and row.get("task_id")
]
print(f"generated_at={snapshot.get('generated_at')}")
print(f"hard_latest_success={snapshot.get('hard_latest_success')}")
print(f"hard_latest_status_counts={json.dumps(snapshot.get('hard_latest_status_counts', {}), sort_keys=True)}")
print(f"followup_processed={snapshot.get('followup_queue', {}).get('processed_count')}")
print(f"followup_remaining={snapshot.get('followup_queue', {}).get('queue_remaining_count')}")
print(f"followup_next={snapshot.get('followup_queue', {}).get('next_queue_task_id')}")
print(f"followup_head={json.dumps(followup_head, sort_keys=True)}")
print(f"post_followup_ready={snapshot.get('post_followup_total')}")
print(f"post_followup_remaining={snapshot.get('post_followup_remaining')}")
print(f"post_followup_head={json.dumps(post_followup_head, sort_keys=True)}")
print(f"post_followup_launcher_running={bool((snapshot.get('post_followup_launcher') or {}).get('running'))}")
print(f"monitor_running={bool((snapshot.get('monitor') or {}).get('running'))}")
print(f"next_auto_action={automation.get('next_auto_action')}")
print(f"followup_usage_limit_blocked={bool(automation.get('followup_usage_limit_blocked'))}")
print(f"followup_ready_to_resume={bool(automation.get('followup_ready_to_resume'))}")
print(f"consistency_ok={consistency.get('ok')}")
print(f"consistency_errors={json.dumps(consistency.get('errors', []), sort_keys=True)}")
print(f"followup_launcher_running={bool(launcher_running)}")
print(f"followup_launcher_pid={launcher_pid}")
print(f"usage_limit_pending_items={halt.get('pending_items')}")
print(f"usage_limit_reset_at={halt.get('reset_at')}")
PY
}

pending() {
  "$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import json
from pathlib import Path

pending_path = Path("reports/group2_hard_wave_2026-07-27/followup_pending_tasks.json")
if not pending_path.exists():
    raise SystemExit("missing pending export: run refresh first")
payload = json.loads(pending_path.read_text(encoding="utf-8"))
print(f"pending_count={payload.get('pending_count')}")
for row in payload.get("pending_tasks", []):
    print(
        "\t".join(
            [
                str(row.get("queue_name") or ""),
                str(row.get("task_id") or ""),
                str(row.get("prior_status") or ""),
                f"attempt={row.get('attempt')}",
            ]
        )
    )
PY
}

pause() {
  local pid
  pid="$(launcher_pid || true)"
  if [[ -z "$pid" ]]; then
    echo "followup launcher already stopped"
    return 0
  fi
  kill "$pid"
  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "stopped followup launcher pid=$pid"
      return 0
    fi
    sleep 0.25
  done
  kill -9 "$pid" 2>/dev/null || true
  echo "force_stopped followup launcher pid=$pid"
}

resume() {
  local pid=""
  pid="$(launcher_pid || true)"
  if [[ -n "$pid" ]]; then
    if launcher_has_desired_config "$pid"; then
      echo "followup launcher already running pid=$pid"
      return 0
    fi
    echo "restarting followup launcher pid=$pid due_to=launcher_config_mismatch"
    stop_pid "$pid"
    rm -f "$FOLLOWUP_LAUNCHER_PID_FILE"
  fi
  if [[ "$IGNORE_USAGE_LIMIT" != "1" ]]; then
    "$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

snapshot_path = Path("reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.json")
if not snapshot_path.exists():
    raise SystemExit("missing snapshot: run refresh first")
snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
followup = snapshot.get("followup_queue") or {}
halt = snapshot.get("followup_usage_limit_halt") or {}
if followup.get("queue_complete"):
    raise SystemExit("followup queue already complete")
reset_at = halt.get("reset_at")
if reset_at:
    reset_dt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
    if reset_dt.tzinfo is None:
        reset_dt = reset_dt.replace(tzinfo=UTC)
    if reset_dt > datetime.now(UTC):
        raise SystemExit(f"usage limit still active until {reset_at}")
PY
  fi

  local ignore_usage_limit_args=()
  if [[ "$IGNORE_USAGE_LIMIT" == "1" ]]; then
    ignore_usage_limit_args+=(--ignore-usage-limit)
  fi
  mkdir -p "$FOLLOWUP_RUN_LOG_DIR"
  : >"$FOLLOWUP_LAUNCHER_LOG"
  setsid -f "$PYTHON_BIN" "$ROOT/scripts/rescue_queue_launcher.py" \
    --queue-file "$FOLLOWUP_QUEUE_FILE" \
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
    --scheduler-log "$FOLLOWUP_SCHEDULER_LOG" \
    --run-log-dir "$FOLLOWUP_RUN_LOG_DIR" \
    --results-root "$ROOT/codex_rescue_runs_local" \
    --state-file "$FOLLOWUP_STATE_FILE" \
    --active-runner-output-root "$ACTIVE_RUNNER_OUTPUT_ROOT" \
    "${ignore_usage_limit_args[@]}" \
    >>"$FOLLOWUP_LAUNCHER_LOG" 2>&1
  sync_pid_file_from_process_match \
    "$FOLLOWUP_LAUNCHER_PID_FILE" \
    "scripts/rescue_queue_launcher.py" \
    "group2_hard_wave_2026-07-27/followup_retry_queue.json" >/dev/null || true
  echo "started followup launcher pid=$(cat "$FOLLOWUP_LAUNCHER_PID_FILE" 2>/dev/null || echo unknown)"
}

case "$ACTION" in
  refresh)
    refresh
    ;;
  status)
    status
    ;;
  pending)
    pending
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
