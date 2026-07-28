from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["task_id\tattempted\tlatest_status\tlatest_success\tever_success"]
    for row in rows:
        lines.append(
            f"{row['task_id']}\t{int(bool(row['attempted']))}\t{row['latest_status'] or ''}\t"
            f"{int(bool(row['latest_success']))}\t{int(bool(row['ever_success']))}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_queue_items(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    payload = read_json(path)
    if isinstance(payload, dict):
        items = payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def load_queue_state(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {
            "exists": False,
            "processed_names": [],
            "processed_task_ids": [],
            "processed_count": 0,
            "queue_complete": False,
            "updated_at": None,
        }
    payload = read_json(path)
    processed_names = [str(item) for item in (payload.get("processed_names") or []) if item]
    processed_task_ids = [str(item) for item in (payload.get("processed_task_ids") or []) if item]
    return {
        "exists": True,
        "processed_names": processed_names,
        "processed_task_ids": processed_task_ids,
        "processed_count": len(processed_names),
        "queue_complete": bool(payload.get("queue_complete")),
        "updated_at": payload.get("updated_at"),
    }


def load_pid_status(path: Path | None) -> dict[str, Any]:
    pid_text = path.read_text(encoding="utf-8").strip() if path is not None and path.exists() else ""
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
        "pid_file": str(path) if path is not None else None,
        "pid": pid,
        "running": running,
    }


def load_runtime_active_tasks(
    path: Path | None,
    *,
    allowed_manifest_kinds: set[str] | None = None,
) -> list[str]:
    if path is None or not path.exists():
        return []
    payload = read_json(path)
    items = payload.get("active_runners") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return []
    active_tasks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        manifest_kind = item.get("manifest_kind")
        if allowed_manifest_kinds is not None and manifest_kind not in allowed_manifest_kinds:
            continue
        task_id = item.get("task_id")
        if not task_id:
            continue
        active_tasks.append(str(task_id))
    return sorted(set(active_tasks))


def count_latest_statuses(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = row.get("latest_status")
        if status:
            counts[str(status)] = counts.get(str(status), 0) + 1
            continue
        if row.get("attempted"):
            counts["no_submission"] = counts.get("no_submission", 0) + 1
        else:
            counts["unattempted"] = counts.get("unattempted", 0) + 1
    return dict(sorted(counts.items()))


def build_queue_summary(
    *,
    queue_state: dict[str, Any],
    queue_items: list[dict[str, Any]],
) -> dict[str, Any]:
    processed_names = list(queue_state.get("processed_names", []))
    processed_task_ids = list(queue_state.get("processed_task_ids", []))
    processed_name_set = set(processed_names)
    processed_task_id_set = set(processed_task_ids)
    for item in queue_items:
        queue_name = str(item.get("name") or "")
        task_id = str(item.get("task_id") or "")
        if queue_name and queue_name in processed_name_set and task_id:
            processed_task_id_set.add(task_id)
    processed_task_ids = sorted(processed_task_id_set)
    pending_queue_items = [
        item
        for item in queue_items
        if str(item.get("name")) not in processed_name_set
        and str(item.get("task_id") or "") not in processed_task_id_set
    ]
    return {
        "exists": bool(queue_state.get("exists")) or bool(queue_items),
        "processed_names": processed_names,
        "processed_task_ids": processed_task_ids,
        "processed_count": len(processed_names),
        "queue_total_count": len(queue_items),
        "queue_remaining_count": len(pending_queue_items),
        "queue_complete": bool(queue_state.get("queue_complete")),
        "updated_at": queue_state.get("updated_at"),
        "next_queue_name": pending_queue_items[0].get("name") if pending_queue_items else None,
        "next_queue_task_id": pending_queue_items[0].get("task_id") if pending_queue_items else None,
        "queue_pending_head": [
            {
                "name": item.get("name"),
                "task_id": item.get("task_id"),
            }
            for item in pending_queue_items[:5]
        ],
    }


def build_snapshot(
    *,
    live_summary: dict[str, Any],
    binary_wave_status: dict[str, Any],
    main_queue_state: dict[str, Any],
    main_queue_items: list[dict[str, Any]],
    followup_queue_state: dict[str, Any],
    followup_queue_items: list[dict[str, Any]],
    post_followup_queue_state: dict[str, Any],
    post_followup_queue_items: list[dict[str, Any]],
    runtime_active_tasks: list[str],
    followup_runtime_active_tasks: list[str],
    post_followup_runtime_active_tasks: list[str],
    monitor_status: dict[str, Any],
    post_followup_launcher_status: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    hard = live_summary["hard"]
    tasks = hard["tasks"]
    rows: list[dict[str, Any]] = []
    row_by_task_id: dict[str, dict[str, Any]] = {}
    for task_id, info in sorted(tasks.items()):
        row = {
            "task_id": task_id,
            "attempted": info.get("attempted", False),
            "latest_status": info.get("latest_status"),
            "latest_success": info.get("latest_success", False),
            "ever_success": info.get("ever_success", False),
        }
        rows.append(row)
        row_by_task_id[task_id] = row

    # Treat runtime-active tasks as the current effective latest state, even if
    # the trajectory/live summary has not yet emitted a finished result record.
    for task_id in runtime_active_tasks:
        row = row_by_task_id.get(task_id)
        if row is None:
            row = {
                "task_id": task_id,
                "attempted": True,
                "latest_status": "active_no_result",
                "latest_success": False,
                "ever_success": False,
            }
            rows.append(row)
            row_by_task_id[task_id] = row
            continue
        row["attempted"] = True
        row["latest_status"] = "active_no_result"
        row["latest_success"] = False

    rows.sort(key=lambda row: str(row["task_id"]))

    live_active_tasks = [task_id for task_id, info in sorted(tasks.items()) if info.get("latest_status") == "active_no_result"]
    effective_active_tasks = sorted(
        {
            str(row["task_id"])
            for row in rows
            if row.get("latest_status") == "active_no_result"
        }
    )
    effective_attempted = sum(1 for row in rows if row.get("attempted"))
    effective_latest_success = sum(1 for row in rows if row.get("latest_success"))
    effective_latest_status_counts = count_latest_statuses(rows)

    main_queue = build_queue_summary(queue_state=main_queue_state, queue_items=main_queue_items)
    followup_queue = build_queue_summary(queue_state=followup_queue_state, queue_items=followup_queue_items)
    post_followup_queue = build_queue_summary(queue_state=post_followup_queue_state, queue_items=post_followup_queue_items)
    effective_queue_kind = "followup" if followup_queue["exists"] else "main"
    effective_queue = followup_queue if effective_queue_kind == "followup" else main_queue
    followup_usage_limit_halt = (((binary_wave_status.get("followup_retry") or {}).get("usage_limit_halt")) or None)
    followup_launcher_status = (((binary_wave_status.get("followup_retry") or {}).get("launcher")) or {})
    followup_launcher_running = bool(followup_launcher_status.get("running"))
    now_dt = datetime.now(UTC)
    usage_limit_reset_at = parse_datetime((followup_usage_limit_halt or {}).get("reset_at"))
    followup_usage_limit_blocked = bool(
        followup_usage_limit_halt
        and usage_limit_reset_at is not None
        and usage_limit_reset_at > now_dt
    )
    if followup_launcher_running:
        followup_usage_limit_blocked = False
    followup_has_work = bool(followup_queue["exists"] and not followup_queue["queue_complete"] and followup_queue["queue_remaining_count"] > 0)
    post_followup_has_work = bool(
        post_followup_queue["exists"]
        and not post_followup_queue["queue_complete"]
        and post_followup_queue["queue_remaining_count"] > 0
    )
    followup_ready_to_resume = bool(
        monitor_status.get("running")
        and main_queue["queue_complete"]
        and followup_has_work
        and not followup_launcher_running
        and not followup_usage_limit_blocked
    )
    post_followup_waiting_on_followup = bool(followup_has_work)
    post_followup_ready_to_start = bool(
        monitor_status.get("running")
        and not followup_has_work
        and post_followup_has_work
        and not bool(post_followup_launcher_status.get("running"))
    )
    if not monitor_status.get("running"):
        next_auto_action = "resume_monitor"
    elif followup_has_work and followup_usage_limit_blocked:
        next_auto_action = "wait_for_usage_limit_reset"
    elif followup_ready_to_resume:
        next_auto_action = "resume_followup"
    elif followup_has_work:
        next_auto_action = "wait_for_followup_completion"
    elif post_followup_ready_to_start:
        next_auto_action = "start_post_followup"
    elif post_followup_has_work:
        next_auto_action = "wait_for_post_followup_completion"
    else:
        next_auto_action = "idle"
    automation = {
        "monitor_running": bool(monitor_status.get("running")),
        "followup_launcher_running": followup_launcher_running,
        "followup_usage_limit_blocked": followup_usage_limit_blocked,
        "followup_ready_to_resume": followup_ready_to_resume,
        "post_followup_launcher_running": bool(post_followup_launcher_status.get("running")),
        "post_followup_waiting_on_followup_completion": post_followup_waiting_on_followup,
        "post_followup_ready_to_start": post_followup_ready_to_start,
        "next_auto_action": next_auto_action,
    }

    snapshot = {
        "generated_at": now_utc(),
        "live_summary_generated_at": live_summary.get("generated_at"),
        "binary_wave_status_generated_at": binary_wave_status.get("generated_at"),
        "hard_total": hard.get("total"),
        "hard_attempted": effective_attempted,
        "hard_latest_success": effective_latest_success,
        "hard_latest_status_counts": effective_latest_status_counts,
        "live_hard_attempted": hard.get("attempted"),
        "live_hard_latest_success": hard.get("latest_success"),
        "live_hard_latest_status_counts": hard.get("latest_status_counts", {}),
        "effective_queue_kind": effective_queue_kind,
        "processed_queue_count": effective_queue["processed_count"],
        "processed_queue_tail": effective_queue["processed_names"][-10:],
        "queue_total_count": effective_queue["queue_total_count"],
        "queue_remaining_count": effective_queue["queue_remaining_count"],
        "queue_complete": effective_queue["queue_complete"],
        "next_queue_name": effective_queue["next_queue_name"],
        "next_queue_task_id": effective_queue["next_queue_task_id"],
        "queue_pending_head": effective_queue["queue_pending_head"],
        "followup_processed": followup_queue["processed_count"],
        "followup_total": followup_queue["queue_total_count"],
        "followup_remaining": followup_queue["queue_remaining_count"],
        "next_followup_name": followup_queue["next_queue_name"],
        "next_followup_task": followup_queue["next_queue_task_id"],
        "followup_pending_head": followup_queue["queue_pending_head"],
        "followup_usage_limit_halt": followup_usage_limit_halt,
        "followup_launcher": followup_launcher_status,
        "monitor": monitor_status,
        "automation": automation,
        "main_queue": main_queue,
        "followup_queue": followup_queue,
        "post_followup_queue": post_followup_queue,
        "post_followup_processed": post_followup_queue["processed_count"],
        "post_followup_total": post_followup_queue["queue_total_count"],
        "post_followup_remaining": post_followup_queue["queue_remaining_count"],
        "next_post_followup_name": post_followup_queue["next_queue_name"],
        "next_post_followup_task": post_followup_queue["next_queue_task_id"],
        "post_followup_pending_head": post_followup_queue["queue_pending_head"],
        "post_followup_launcher": post_followup_launcher_status,
        "active_tasks": live_active_tasks,
        "runtime_active_tasks": runtime_active_tasks,
        "followup_runtime_active_tasks": followup_runtime_active_tasks,
        "post_followup_runtime_active_tasks": post_followup_runtime_active_tasks,
        "all_runtime_active_tasks": sorted(
            set(runtime_active_tasks) | set(followup_runtime_active_tasks) | set(post_followup_runtime_active_tasks)
        ),
        "effective_active_tasks": effective_active_tasks,
        "success_tasks": [str(row["task_id"]) for row in rows if row.get("latest_status") == "success"],
        "fix_also_crashes_tasks": [str(row["task_id"]) for row in rows if row.get("latest_status") == "fix_also_crashes"],
        "no_vul_crash_tasks": [str(row["task_id"]) for row in rows if row.get("latest_status") == "no_vul_crash"],
        "invalid_result_tasks": [str(row["task_id"]) for row in rows if row.get("latest_status") == "invalid_result"],
        "unattempted_tasks": [str(row["task_id"]) for row in rows if not row.get("attempted")],
        "tasks": rows,
    }
    return snapshot, rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export compact task-level status snapshots for the group2 hard wave.")
    parser.add_argument("--live-summary", type=Path, required=True)
    parser.add_argument("--binary-wave-status", type=Path, required=True)
    parser.add_argument("--queue-state", type=Path, required=True)
    parser.add_argument("--queue-file", type=Path)
    parser.add_argument("--followup-queue-state", type=Path)
    parser.add_argument("--followup-queue-file", type=Path)
    parser.add_argument("--post-followup-queue-state", type=Path)
    parser.add_argument("--post-followup-queue-file", type=Path)
    parser.add_argument("--active-runner-metrics", type=Path)
    parser.add_argument("--monitor-pid-file", type=Path)
    parser.add_argument("--post-followup-launcher-pid-file", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-tsv", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    live_summary = read_json(args.live_summary.resolve())
    binary_wave_status = read_json(args.binary_wave_status.resolve())
    main_queue_state = load_queue_state(args.queue_state.resolve())
    main_queue_items = load_queue_items(args.queue_file.resolve() if args.queue_file else None)
    followup_queue_state = load_queue_state(args.followup_queue_state.resolve() if args.followup_queue_state else None)
    followup_queue_items = load_queue_items(args.followup_queue_file.resolve() if args.followup_queue_file else None)
    post_followup_queue_state = load_queue_state(args.post_followup_queue_state.resolve() if args.post_followup_queue_state else None)
    post_followup_queue_items = load_queue_items(args.post_followup_queue_file.resolve() if args.post_followup_queue_file else None)
    metrics_path = args.active_runner_metrics.resolve() if args.active_runner_metrics else None
    runtime_active_tasks = load_runtime_active_tasks(
        metrics_path,
        allowed_manifest_kinds={"fresh_manifests", "retry_manifests"},
    )
    followup_runtime_active_tasks = load_runtime_active_tasks(
        metrics_path,
        allowed_manifest_kinds={"followup_retry_manifests"},
    )
    post_followup_runtime_active_tasks = load_runtime_active_tasks(
        metrics_path,
        allowed_manifest_kinds={"post_followup_retry_manifests"},
    )
    monitor_status = load_pid_status(args.monitor_pid_file.resolve() if args.monitor_pid_file else None)
    post_followup_launcher_status = load_pid_status(
        args.post_followup_launcher_pid_file.resolve() if args.post_followup_launcher_pid_file else None
    )
    snapshot, rows = build_snapshot(
        live_summary=live_summary,
        binary_wave_status=binary_wave_status,
        main_queue_state=main_queue_state,
        main_queue_items=main_queue_items,
        followup_queue_state=followup_queue_state,
        followup_queue_items=followup_queue_items,
        post_followup_queue_state=post_followup_queue_state,
        post_followup_queue_items=post_followup_queue_items,
        runtime_active_tasks=runtime_active_tasks,
        followup_runtime_active_tasks=followup_runtime_active_tasks,
        post_followup_runtime_active_tasks=post_followup_runtime_active_tasks,
        monitor_status=monitor_status,
        post_followup_launcher_status=post_followup_launcher_status,
    )
    write_json(args.output_json.resolve(), snapshot)
    write_tsv(args.output_tsv.resolve(), rows)
    print(
        json.dumps(
            {
                "task_count": len(rows),
                "active_task_count": len(snapshot["active_tasks"]),
                "runtime_active_task_count": len(snapshot["runtime_active_tasks"]),
                "effective_active_task_count": len(snapshot["effective_active_tasks"]),
                "output_json": str(args.output_json.resolve()),
                "output_tsv": str(args.output_tsv.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
