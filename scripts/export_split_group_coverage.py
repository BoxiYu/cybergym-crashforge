from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def load_latest_rows(path: Path) -> tuple[dict[str, dict[str, Any]], set[str]]:
    latest_by_task: dict[str, dict[str, Any]] = {}
    success_tasks: set[str] = set()
    if not path.exists():
        return latest_by_task, success_tasks
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        task_id = row.get("task_id")
        if not task_id:
            continue
        latest_by_task[str(task_id)] = row
        verdict_counts = row.get("verdict_counts") or {}
        if row.get("status") == "success" or row.get("has_verified_success") or int(verdict_counts.get("verified_success") or 0) > 0:
            success_tasks.add(str(task_id))
    return latest_by_task, success_tasks


def load_task_ids(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize_group(path: Path, latest_by_task: dict[str, dict[str, Any]], success_tasks: set[str]) -> dict[str, Any]:
    task_ids = load_task_ids(path)
    attempted = [task_id for task_id in task_ids if task_id in latest_by_task]
    succeeded = [task_id for task_id in task_ids if task_id in success_tasks]
    latest_status_counts: dict[str, int] = {}
    for task_id in attempted:
        status = str((latest_by_task.get(task_id) or {}).get("status") or "unknown")
        latest_status_counts[status] = latest_status_counts.get(status, 0) + 1
    unattempted = [task_id for task_id in task_ids if task_id not in latest_by_task]
    return {
        "group_file": str(path),
        "total": len(task_ids),
        "attempted_count": len(attempted),
        "success_count": len(succeeded),
        "attempted_rate": (len(attempted) / len(task_ids)) if task_ids else 0.0,
        "success_rate_all": (len(succeeded) / len(task_ids)) if task_ids else 0.0,
        "latest_status_counts": dict(sorted(latest_status_counts.items())),
        "attempted_tasks": attempted,
        "success_tasks": succeeded,
        "unattempted_tasks": unattempted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export coverage and latest-status summaries for splits groups.")
    parser.add_argument("--splits-dir", type=Path, default=Path("splits"))
    parser.add_argument("--trajectory-index", type=Path, default=Path("codex_rescue_runs_local/trajectory_index.jsonl"))
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()

    latest_by_task, success_tasks = load_latest_rows(args.trajectory_index.resolve())
    groups = {}
    for path in sorted(args.splits_dir.resolve().glob("group_*.md")):
        groups[path.name] = summarize_group(path, latest_by_task, success_tasks)

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(
            {
                "generated_at": now_utc(),
                "trajectory_index": str(args.trajectory_index.resolve()),
                "splits_dir": str(args.splits_dir.resolve()),
                "groups": groups,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"group_count": len(groups), "output_json": str(args.output_json.resolve())}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
