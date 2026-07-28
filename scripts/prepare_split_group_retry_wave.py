from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from build_static_retry_queue import (
    DEFAULT_RETRYABLE_STATUSES,
    build_retry_entry,
    has_verified_success,
    is_active_row,
    load_trajectory_rows,
    row_order_key,
)
from project_routing import (
    attach_project_routing_metadata,
    build_project_stats,
    load_task_metadata,
    summarize_route_policies_for_tasks,
    summarize_projects_for_tasks,
    task_priority_key,
)


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def read_task_ids(path: Path) -> list[str]:
    seen: set[str] = set()
    task_ids: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        task_id = raw_line.strip()
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        task_ids.append(task_id)
    return task_ids


def task_slug(task_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", task_id).strip("_")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clear_manifest_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for manifest_path in path.glob("*.jsonl"):
        manifest_path.unlink(missing_ok=True)


def remove_manifest_dir(path: Path) -> None:
    if not path.exists():
        return
    for manifest_path in path.glob("*.jsonl"):
        manifest_path.unlink(missing_ok=True)


def select_retry_rows(
    *,
    trajectory_index: Path,
    task_ids: set[str],
    retryable_statuses: set[str],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows = load_trajectory_rows(trajectory_index)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "")
        status = str(row.get("status") or "")
        if not task_id or not status or task_id not in task_ids:
            continue
        grouped.setdefault(task_id, []).append(row)

    selected_rows: list[dict[str, Any]] = []
    selected_task_rows: dict[str, list[dict[str, Any]]] = {}
    for task_id, task_rows in grouped.items():
        task_rows.sort(key=row_order_key)
        latest = task_rows[-1]
        if any(has_verified_success(row) for row in task_rows):
            continue
        if is_active_row(latest):
            continue
        if latest["status"] not in retryable_statuses:
            continue
        selected_rows.append(latest)
        selected_task_rows[task_id] = task_rows

    selected_rows.sort(key=lambda row: (str(row.get("status") or ""), str(row.get("task_id") or ""), row_order_key(row)))
    return selected_rows, selected_task_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a retry-only binary wave from currently unsolved tasks in a split-group markdown file.")
    parser.add_argument("--group-file", type=Path, required=True)
    parser.add_argument("--wave-dir", type=Path, required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--trajectory-index", type=Path, default=Path("codex_rescue_runs_local/trajectory_index.jsonl"))
    parser.add_argument("--results-root", type=Path, default=Path("codex_rescue_runs_local"))
    parser.add_argument("--difficulty", default="level1")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-timeout-seconds", type=int, default=5400)
    parser.add_argument("--api-key-env", default="CYBERGYM_API_KEY")
    parser.add_argument("--tasks-json", type=Path, default=Path("cybergym_data/tasks.json"))
    parser.add_argument(
        "--preserve-processed-state",
        action="store_true",
        help="Keep processed queue state for matching task names instead of resetting the retry frontier.",
    )
    args = parser.parse_args()

    task_ids = read_task_ids(args.group_file.resolve())
    wave_dir = args.wave_dir.resolve()
    queue_file = wave_dir / "combined_queue.json"
    state_file = wave_dir / "combined_queue.binary.state.json"
    manifest_dir = wave_dir / "retry_manifests"
    fresh_manifest_dir = wave_dir / "fresh_manifests"
    routing_summary_path = wave_dir / "project_routing_summary.json"

    clear_manifest_dir(manifest_dir)
    remove_manifest_dir(fresh_manifest_dir)
    metadata_by_task = load_task_metadata(args.tasks_json.resolve())
    project_stats = build_project_stats(args.trajectory_index.resolve(), metadata_by_task=metadata_by_task)
    selected_rows, selected_task_rows = select_retry_rows(
        trajectory_index=args.trajectory_index.resolve(),
        task_ids=set(task_ids),
        retryable_statuses=set(DEFAULT_RETRYABLE_STATUSES),
    )
    selected_rows.sort(
        key=lambda row: (
            *task_priority_key(
                str(row["task_id"]),
                metadata_by_task=metadata_by_task,
                project_stats=project_stats,
            ),
            str(row.get("status") or ""),
            row_order_key(row),
        )
    )

    queue_items: list[dict[str, str]] = []
    queue_names: set[str] = set()
    for row in selected_rows:
        task_id = str(row["task_id"])
        name = task_slug(task_id)
        manifest_path = manifest_dir / f"{name}.jsonl"
        entry = attach_project_routing_metadata(
            build_retry_entry(
                row=row,
                task_rows=selected_task_rows[task_id],
                results_root=args.results_root.resolve(),
                server=args.server,
                data_dir=args.data_dir,
                difficulty=args.difficulty,
                campaign=args.campaign,
                codex_bin=args.codex_bin,
                codex_timeout_seconds=args.codex_timeout_seconds,
                api_key_env=args.api_key_env,
            ),
            task_id=task_id,
            metadata_by_task=metadata_by_task,
            project_stats=project_stats,
        )
        manifest_path.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
        queue_items.append(
            {
                "manifest": str(manifest_path),
                "name": name,
                "output_root": args.output_root,
                "task_id": task_id,
            }
        )
        queue_names.add(name)

    write_json(queue_file, {"items": queue_items})
    write_json(
        wave_dir / "task_source.json",
        {
            "tasks": task_ids,
        },
    )
    processed_names: list[str] = []
    processed_task_ids: list[str] = []
    if args.preserve_processed_state and state_file.exists():
        try:
            existing_state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing_state = {}
        processed_names = [str(name) for name in (existing_state.get("processed_names") or []) if str(name) in queue_names]
        processed_task_ids = [
            str(task_id)
            for task_id in (existing_state.get("processed_task_ids") or [])
            if any(item.get("task_id") == str(task_id) for item in queue_items)
        ]
    write_json(
        state_file,
        {
            "processed_names": processed_names,
            "processed_task_ids": processed_task_ids,
            "queue_complete": False,
            "updated_at": now_utc(),
        },
    )
    write_json(
        wave_dir / "wave_prep_summary.json",
        {
            "group_file": str(args.group_file.resolve()),
            "wave_dir": str(wave_dir),
            "campaign": args.campaign,
            "server": args.server,
            "data_dir": args.data_dir,
            "output_root": args.output_root,
            "task_count": len(task_ids),
            "retry_task_count": len(queue_items),
            "first_tasks": [str(row["task_id"]) for row in selected_rows[:10]],
            "retry_manifest_dir": str(manifest_dir),
            "combined_queue_file": str(queue_file),
            "queue_mode": "retry_only",
            "preserve_processed_state": bool(args.preserve_processed_state),
            "project_routing_summary": str(routing_summary_path),
            "routing_strategy": "project_priority_score_desc",
        },
    )
    write_json(
        routing_summary_path,
        {
            "routing_strategy": "project_priority_score_desc",
            "tasks_json": str(args.tasks_json.resolve()),
            "trajectory_index": str(args.trajectory_index.resolve()),
            "retry_task_count": len(queue_items),
            "route_policy_counts": summarize_route_policies_for_tasks(
                [str(row["task_id"]) for row in selected_rows],
                metadata_by_task=metadata_by_task,
                project_stats=project_stats,
            ),
            "top_projects": summarize_projects_for_tasks(
                [str(row["task_id"]) for row in selected_rows],
                metadata_by_task=metadata_by_task,
                project_stats=project_stats,
            ),
        },
    )
    print(
        json.dumps(
            {
                "group_file": str(args.group_file.resolve()),
                "task_count": len(task_ids),
                "retry_task_count": len(queue_items),
                "wave_dir": str(wave_dir),
                "queue_file": str(queue_file),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
