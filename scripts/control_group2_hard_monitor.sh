#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 {status|pause|resume}" >&2
  exit 1
fi

ACTION="$1"
WAVE_DIR="$ROOT/reports/group2_hard_wave_2026-07-27"
MONITOR_LOG="$WAVE_DIR/monitor.stdout.log"
MONITOR_PID_FILE="$WAVE_DIR/monitor.pid"

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

monitor_pid() {
  local pid=""
  if [[ -f "$MONITOR_PID_FILE" ]]; then
    pid="$(cat "$MONITOR_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      printf '%s\n' "$pid"
      return 0
    fi
  fi
  sync_pid_file_from_process_match \
    "$MONITOR_PID_FILE" \
    "scripts/monitor_group2_hard_wave.sh" >/dev/null || true
  if [[ -f "$MONITOR_PID_FILE" ]]; then
    pid="$(cat "$MONITOR_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      printf '%s\n' "$pid"
      return 0
    fi
  fi
  return 1
}

monitor_running() {
  monitor_pid >/dev/null 2>&1
}

refresh_group2_hard_state() {
  bash "$ROOT/scripts/control_group2_hard_followup.sh" refresh >/dev/null
}

status() {
  "$ROOT/.venv/bin/python" - <<'PY'
from __future__ import annotations

import json
from pathlib import Path

pid_path = Path("reports/group2_hard_wave_2026-07-27/monitor.pid")
snapshot_path = Path("reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.json")
consistency_path = Path("reports/group2_hard_wave_2026-07-27/consistency_check.json")

pid_text = pid_path.read_text(encoding="utf-8").strip() if pid_path.exists() else ""
print(f"monitor_pid_file={pid_path if pid_path.exists() else 'missing'}")
print(f"monitor_pid={pid_text or 'none'}")

if not snapshot_path.exists():
    print("snapshot_exists=False")
    raise SystemExit(0)

snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
consistency = json.loads(consistency_path.read_text(encoding="utf-8")) if consistency_path.exists() else {}
followup = snapshot.get("followup_queue") or {}
automation = snapshot.get("automation") or {}
halt = snapshot.get("followup_usage_limit_halt") or {}
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

print("snapshot_exists=True")
print(f"generated_at={snapshot.get('generated_at')}")
print(f"monitor_running={bool((snapshot.get('monitor') or {}).get('running'))}")
print(f"monitor_snapshot_pid={(snapshot.get('monitor') or {}).get('pid')}")
print(f"next_auto_action={automation.get('next_auto_action')}")
print(f"followup_usage_limit_blocked={bool(automation.get('followup_usage_limit_blocked'))}")
print(f"followup_ready_to_resume={bool(automation.get('followup_ready_to_resume'))}")
print(f"followup_remaining={followup.get('queue_remaining_count')}")
print(f"followup_head={json.dumps(followup_head, sort_keys=True)}")
print(f"usage_limit_pending_items={halt.get('pending_items')}")
print(f"usage_limit_reset_at={halt.get('reset_at')}")
print(f"post_followup_remaining={snapshot.get('post_followup_remaining')}")
print(f"post_followup_head={json.dumps(post_followup_head, sort_keys=True)}")
print(f"consistency_ok={consistency.get('ok')}")
print(f"consistency_errors={json.dumps(consistency.get('errors', []), sort_keys=True)}")
PY
}

pause() {
  local pid
  pid="$(monitor_pid || true)"
  if [[ -z "$pid" ]]; then
    echo "monitor already stopped"
    return 0
  fi
  kill "$pid"
  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      refresh_group2_hard_state
      echo "stopped monitor pid=$pid"
      return 0
    fi
    sleep 0.25
  done
  kill -9 "$pid" 2>/dev/null || true
  refresh_group2_hard_state
  echo "force_stopped monitor pid=$pid"
}

resume() {
  if monitor_running; then
    echo "monitor already running pid=$(monitor_pid)"
    return 0
  fi
  : >"$MONITOR_LOG"
  setsid -f "$ROOT/scripts/monitor_group2_hard_wave.sh" >>"$MONITOR_LOG" 2>&1
  sync_pid_file_from_process_match \
    "$MONITOR_PID_FILE" \
    "scripts/monitor_group2_hard_wave.sh" >/dev/null || true
  refresh_group2_hard_state
  echo "started monitor pid=$(cat "$MONITOR_PID_FILE" 2>/dev/null || echo unknown)"
}

case "$ACTION" in
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
