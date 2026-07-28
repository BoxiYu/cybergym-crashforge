from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlite3

from verdicts import classify_poc_verdict


CODEX_TASK_DIR_RE = re.compile(r"\bcodex exec -C (\S+)")
MANIFEST_RUNNER_RE = re.compile(
    r"scripts/codex_rescue_runner\.py run --manifest \S*/group2_hard_wave_2026-07-27/"
    r"(fresh_manifests|retry_after_binary_fix|retry_manifests|followup_retry_manifests|post_followup_retry_manifests)/"
)
USAGE_LIMIT_HALT_RE = re.compile(
    r"halting: usage_limit_detected "
    r"pending_items=(?P<pending_items>\d+) "
    r"run_root=(?P<run_root>\S+) "
    r"reset_at=(?P<reset_at>\S+) "
    r"events_path=(?P<events_path>\S+)"
)


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def load_live_hard_summary(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    hard = payload.get("hard") or {}
    return {
        "attempted": int(hard.get("attempted") or 0),
        "latest_success": int(hard.get("latest_success") or 0),
        "ever_success": int(hard.get("ever_success") or 0),
        "latest_pass_rate_all": hard.get("latest_pass_rate_all"),
        "latest_pass_rate_attempted": hard.get("latest_pass_rate_attempted"),
        "latest_status_counts": dict(hard.get("latest_status_counts") or {}),
    }


def load_queue_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "processed_names": [],
            "processed_task_ids": [],
            "processed_count": 0,
            "queue_complete": False,
            "updated_at": None,
            "exists": False,
        }
    payload = read_json(path)
    processed_names = sorted(str(item) for item in (payload.get("processed_names") or []) if item)
    processed_task_ids = sorted(str(item) for item in (payload.get("processed_task_ids") or []) if item)
    return {
        "processed_names": processed_names,
        "processed_task_ids": processed_task_ids,
        "processed_count": len(processed_names),
        "queue_complete": bool(payload.get("queue_complete")),
        "updated_at": payload.get("updated_at"),
        "exists": True,
    }


def collect_runner_state() -> dict[str, Any]:
    proc = subprocess.run(["ps", "-eo", "cmd="], check=True, capture_output=True, text=True)
    fresh_runners = []
    retry_runners = []
    retry_watcher_commands = []
    active_codex_task_dirs = []
    active_codex_run_roots = []
    watcher_active = False
    for line in proc.stdout.splitlines():
        text = line.strip()
        is_retry_watcher = "44089_attempt2.jsonl" in text and "while true; do n=$(ps -eo cmd=" in text
        match = CODEX_TASK_DIR_RE.search(text)
        manifest_match = MANIFEST_RUNNER_RE.search(text)
        if match:
            task_dir = Path(match.group(1))
            active_codex_task_dirs.append(str(task_dir))
            if task_dir.name == "task":
                active_codex_run_roots.append(str(task_dir.parent))
        if manifest_match:
            manifest_kind = manifest_match.group(1)
            if manifest_kind == "fresh_manifests":
                fresh_runners.append(text)
            elif not is_retry_watcher:
                retry_runners.append(text)
        if is_retry_watcher:
            watcher_active = True
            retry_watcher_commands.append(text)
    return {
        "active_fresh_runner_count": len(fresh_runners),
        "active_retry_runner_count": len(retry_runners),
        "retry_watcher_active": watcher_active,
        "fresh_runner_commands": fresh_runners,
        "retry_runner_commands": retry_runners,
        "retry_watcher_commands": retry_watcher_commands,
        "active_codex_task_dirs": unique_preserve_order(active_codex_task_dirs),
        "active_codex_run_roots": unique_preserve_order(active_codex_run_roots),
    }


def load_retry_notes(retry_dir: Path) -> dict[str, Any]:
    notes_path = retry_dir / "notes.json"
    queue_path = retry_dir / "queue.json"
    manifest_paths = sorted(retry_dir.glob("*.jsonl"))
    notes = read_json(notes_path) if notes_path.exists() else {}
    queue = read_json(queue_path) if queue_path.exists() else {}
    return {
        "task_id": notes.get("task_id"),
        "reason": notes.get("reason"),
        "replacement_manifest": notes.get("replacement_manifest"),
        "replacement_queue_name": notes.get("replacement_queue_name"),
        "created_at": notes.get("created_at"),
        "queue_items": list((queue.get("items") or [])),
        "manifest_files": [str(path) for path in manifest_paths],
    }


def load_json_queue_items(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = read_json(path)
    if isinstance(payload, dict):
        items = payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def load_pid_status(pid_file: Path) -> dict[str, Any]:
    pid_text = pid_file.read_text(encoding="utf-8").strip() if pid_file.exists() else ""
    pid = int(pid_text) if pid_text.isdigit() else None
    running = False
    if pid is not None:
        try:
            os.kill(pid, 0)
        except OSError:
            running = False
        else:
            running = True
    return {
        "pid_file": str(pid_file),
        "pid": pid,
        "running": running,
    }


def load_followup_state(
    *,
    queue_file: Path | None,
    state_file: Path | None,
    launcher_pid_file: Path | None,
    scheduler_log: Path | None,
) -> dict[str, Any]:
    queue_items = load_json_queue_items(queue_file) if queue_file is not None else []
    queue_state = load_queue_state(state_file) if state_file is not None else {
        "processed_names": [],
        "processed_count": 0,
        "queue_complete": False,
        "updated_at": None,
        "exists": False,
    }
    launcher_status = load_pid_status(launcher_pid_file) if launcher_pid_file is not None else {
        "pid_file": None,
        "pid": None,
        "running": False,
    }
    usage_limit_halt = load_usage_limit_halt(scheduler_log) if scheduler_log is not None else None
    return {
        "queue_file": str(queue_file) if queue_file is not None else None,
        "queue_item_count": len(queue_items),
        "queue_items": queue_items,
        "queue_state": queue_state,
        "launcher": launcher_status,
        "usage_limit_halt": usage_limit_halt,
    }


def load_usage_limit_halt(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for raw_line in reversed(lines):
        match = USAGE_LIMIT_HALT_RE.search(raw_line)
        if match is None:
            continue
        payload = match.groupdict()
        return {
            "pending_items": int(payload["pending_items"]),
            "run_root": payload["run_root"],
            "reset_at": payload["reset_at"],
            "events_path": payload["events_path"],
            "log_line": raw_line,
        }
    return None


def load_optional_summary(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    payload = read_json(path)
    return payload if isinstance(payload, dict) else None


def summarize_poc_db(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select
              task_id,
              agent_id,
              poc_id,
              poc_length,
              vul_exit_code,
              fix_exit_code,
              created_at,
              updated_at
            from poc_records
            order by created_at
            """
        ).fetchall()
    finally:
        conn.close()

    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    totals = Counter()
    for row in rows:
        item = dict(row)
        task_id = str(item["task_id"])
        verdict = classify_poc_verdict(item["vul_exit_code"], item["fix_exit_code"], task_id)
        item["derived_verdict"] = verdict
        totals[verdict] += 1
        by_task[task_id].append(item)

    task_summary: dict[str, Any] = {}
    verified_success_tasks: list[str] = []
    no_vul_crash_tasks: list[str] = []
    pending_only_tasks: list[str] = []
    for task_id, items in sorted(by_task.items()):
        counts = Counter(item["derived_verdict"] for item in items)
        has_verified = counts["verified_success"] > 0
        has_no_vul = counts["no_vul_crash"] > 0
        has_pending_only = set(counts) <= {"submission_error_or_pending"}
        if has_verified:
            verified_success_tasks.append(task_id)
        if has_no_vul:
            no_vul_crash_tasks.append(task_id)
        if has_pending_only:
            pending_only_tasks.append(task_id)
        task_summary[task_id] = {
            "record_count": len(items),
            "derived_verdict_counts": dict(sorted(counts.items())),
            "latest_record": items[-1],
        }

    return {
        "record_count": len(rows),
        "derived_verdict_counts": dict(sorted(totals.items())),
        "verified_success_tasks": verified_success_tasks,
        "verified_success_task_count": len(verified_success_tasks),
        "no_vul_crash_tasks": no_vul_crash_tasks,
        "no_vul_crash_task_count": len(no_vul_crash_tasks),
        "pending_only_tasks": pending_only_tasks,
        "pending_only_task_count": len(pending_only_tasks),
        "tasks": task_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize the current state of the group2 hard binary wave.")
    parser.add_argument("--live-summary", type=Path, required=True)
    parser.add_argument("--pocdb-path", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--retry-dir", type=Path, required=True)
    parser.add_argument("--followup-queue-file", type=Path)
    parser.add_argument("--followup-state-file", type=Path)
    parser.add_argument("--followup-launcher-pid-file", type=Path)
    parser.add_argument("--followup-scheduler-log", type=Path)
    parser.add_argument("--post-main-preview-queue-file", type=Path)
    parser.add_argument("--post-main-preview-summary-file", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()

    live_hard = load_live_hard_summary(args.live_summary)
    queue_state = load_queue_state(args.state_file)
    runner_state = collect_runner_state()
    retry_notes = load_retry_notes(args.retry_dir)
    followup_state = load_followup_state(
        queue_file=args.followup_queue_file,
        state_file=args.followup_state_file,
        launcher_pid_file=args.followup_launcher_pid_file,
        scheduler_log=args.followup_scheduler_log,
    )
    post_main_preview = load_followup_state(
        queue_file=args.post_main_preview_queue_file,
        state_file=None,
        launcher_pid_file=None,
        scheduler_log=None,
    )
    post_main_preview["summary"] = load_optional_summary(args.post_main_preview_summary_file)
    db_summary = summarize_poc_db(args.pocdb_path)

    tainted_retry_task_id = retry_notes.get("task_id")
    effective_latest_success = live_hard["latest_success"]
    tainted_task_live_status = None
    if tainted_retry_task_id:
        full_live = read_json(args.live_summary)
        hard_tasks = ((full_live.get("hard") or {}).get("tasks") or {})
        tainted_task_live_status = (hard_tasks.get(tainted_retry_task_id) or {}).get("latest_status")

    payload = {
        "generated_at": now_utc(),
        "live_hard": live_hard,
        "queue_state": queue_state,
        "runner_state": runner_state,
        "poc_db": db_summary,
        "retry_after_binary_fix": retry_notes,
        "followup_retry": followup_state,
        "post_main_static_retry_preview": post_main_preview,
        "tainted_infra_failure": {
            "task_id": tainted_retry_task_id,
            "live_latest_status": tainted_task_live_status,
            "needs_clean_retry": bool(tainted_retry_task_id),
        },
        "effective_progress": {
            "trajectory_latest_success": live_hard["latest_success"],
            "db_verified_success_task_count": db_summary["verified_success_task_count"],
            "db_verified_success_tasks": db_summary["verified_success_tasks"],
            "active_fresh_runner_count": runner_state["active_fresh_runner_count"],
            "active_retry_runner_count": runner_state["active_retry_runner_count"],
            "effective_latest_success": effective_latest_success,
            "followup_queue_item_count": followup_state["queue_item_count"],
            "followup_processed_count": int((followup_state["queue_state"] or {}).get("processed_count") or 0),
            "followup_queue_complete": bool((followup_state["queue_state"] or {}).get("queue_complete")),
            "followup_launcher_running": bool(((followup_state.get("launcher") or {}).get("running"))),
            "followup_usage_limit_halted": bool(followup_state.get("usage_limit_halt")),
            "followup_usage_limit_reset_at": (followup_state.get("usage_limit_halt") or {}).get("reset_at"),
            "followup_usage_limit_pending_items": (followup_state.get("usage_limit_halt") or {}).get("pending_items"),
            "post_main_preview_queue_item_count": post_main_preview["queue_item_count"],
        },
    }
    write_json(args.output_json, payload)


if __name__ == "__main__":
    main()
