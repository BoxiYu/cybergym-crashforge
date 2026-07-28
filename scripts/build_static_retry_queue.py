from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from project_routing import (
    attach_project_routing_metadata,
    build_project_stats,
    load_task_metadata,
    task_priority_key,
)

DEFAULT_RETRYABLE_STATUSES = (
    "no_vul_crash",
    "fix_also_crashes",
    "codex_failed",
    "no_submission",
    "invalid_result",
)
MAX_HISTORY_SOURCE_RUN_ROOTS = 4


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_result_timestamp(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00").replace(" ", "T")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def row_order_key(row: dict[str, Any]) -> tuple[float, str]:
    primary = parse_result_timestamp(row.get("started_at")) or 0.0
    return (primary, str(row.get("run_id") or row.get("run_root") or ""))


def load_trajectory_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_task_ids(path: Path | None) -> set[str]:
    if path is None:
        return set()
    payload = read_json(path)
    if isinstance(payload, dict):
        if isinstance(payload.get("tasks"), list):
            payload = payload["tasks"]
        elif isinstance(payload.get("items"), list):
            payload = payload["items"]
    if not isinstance(payload, list):
        raise ValueError(f"unsupported task source payload in {path}")

    task_ids: set[str] = set()
    for item in payload:
        if isinstance(item, str):
            task_ids.add(item)
            continue
        if isinstance(item, dict) and item.get("excluded_from_original_123"):
            continue
        if isinstance(item, dict) and item.get("task_id"):
            task_ids.add(str(item["task_id"]))
    return task_ids


def has_verified_success(row: dict[str, Any]) -> bool:
    if row.get("status") == "success":
        return True
    if row.get("has_verified_success"):
        return True
    verdict_counts = row.get("verdict_counts") or {}
    try:
        return int(verdict_counts.get("verified_success") or 0) > 0
    except (TypeError, ValueError):
        return False


def is_active_row(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "")
    if status.startswith("active_"):
        return True
    if row.get("active_process"):
        return True
    return str(row.get("executor_status") or "") in {"running", "pending"}


def latest_run_id(row: dict[str, Any]) -> str:
    return str(row.get("run_id") or Path(str(row.get("run_root") or "")).name)


def task_slug(task_id: str) -> str:
    return task_id.replace(":", "_")


def selected_row_sort_key(
    row: dict[str, Any],
    *,
    status_priority: tuple[str, ...] | None = None,
) -> tuple[int, str, tuple[float, str]]:
    priority_index = {status: index for index, status in enumerate(status_priority or ())}
    rank = priority_index.get(str(row.get("status") or ""), len(priority_index))
    return (rank, str(row.get("task_id") or ""), row_order_key(row))


def collect_recent_source_run_roots(
    task_rows: list[dict[str, Any]],
    *,
    results_root: Path,
    max_items: int = MAX_HISTORY_SOURCE_RUN_ROOTS,
) -> list[str]:
    recent_rows = sorted(task_rows, key=row_order_key, reverse=True)
    roots: list[str] = []
    seen: set[str] = set()
    for task_row in recent_rows:
        raw_run_root = task_row.get("run_root")
        if not raw_run_root:
            continue
        resolved = str((results_root / str(raw_run_root)).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
        if len(roots) >= max_items:
            break
    return roots


def build_retry_entry(
    *,
    row: dict[str, Any],
    task_rows: list[dict[str, Any]],
    results_root: Path,
    server: str,
    data_dir: str,
    difficulty: str,
    campaign: str,
    codex_bin: str,
    codex_timeout_seconds: int,
    api_key_env: str,
) -> dict[str, Any]:
    attempt = int(row.get("attempt") or 0) + 1
    run_id = latest_run_id(row)
    status_counts = Counter(str(task_row.get("status") or "<missing>") for task_row in task_rows)
    failure_counts = Counter(str(task_row.get("failure_category") or "<none>") for task_row in task_rows)
    verdict_counts: Counter[str] = Counter()
    for task_row in task_rows:
        for verdict, count in (task_row.get("verdict_counts") or {}).items():
            try:
                verdict_counts[str(verdict)] += int(count)
            except (TypeError, ValueError):
                continue
    return {
        "api_key_env": api_key_env,
        "attempt": attempt,
        "campaign": campaign,
        "codex_bin": codex_bin,
        "codex_timeout_seconds": codex_timeout_seconds,
        "data_dir": data_dir,
        "difficulty": difficulty,
        "failure_category": row.get("failure_category") or row["status"],
        "history_failure_category_counts": dict(failure_counts),
        "history_source_run_roots": collect_recent_source_run_roots(task_rows, results_root=results_root),
        "history_status_counts": dict(status_counts),
        "history_verdict_counts": dict(verdict_counts),
        "parent_run_id": run_id,
        "prior_status": row["status"],
        "rescue_queue": "other",
        "retryable": True,
        "server": server,
        "source_run_id": run_id,
        "source_run_root": str((results_root / str(row["run_root"])).resolve()),
        "task_id": row["task_id"],
    }


def write_static_retry_queue(
    *,
    trajectory_index: Path,
    task_source: Path | None,
    results_root: Path,
    output_manifest_dir: Path,
    output_queue_file: Path,
    output_root: str,
    server: str,
    data_dir: str,
    difficulty: str,
    campaign: str,
    retryable_statuses: set[str],
    status_priority: tuple[str, ...] | None = None,
    codex_bin: str,
    codex_timeout_seconds: int,
    api_key_env: str,
    tasks_json: Path,
) -> dict[str, Any]:
    rows = load_trajectory_rows(trajectory_index)
    selected_task_ids = load_task_ids(task_source)
    metadata_by_task = load_task_metadata(tasks_json.resolve())
    project_stats = build_project_stats(trajectory_index.resolve(), metadata_by_task=metadata_by_task)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        task_id = row.get("task_id")
        status = row.get("status")
        if not task_id or not status:
            continue
        if selected_task_ids and task_id not in selected_task_ids:
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
        selected_task_rows[str(task_id)] = list(task_rows)

    selected_rows.sort(
        key=lambda row: (
            *task_priority_key(
                str(row["task_id"]),
                metadata_by_task=metadata_by_task,
                project_stats=project_stats,
            ),
            *selected_row_sort_key(row, status_priority=status_priority),
        )
    )
    output_manifest_dir.mkdir(parents=True, exist_ok=True)
    output_queue_file.parent.mkdir(parents=True, exist_ok=True)

    width = max(2, len(str(len(selected_rows))))
    queue_items: list[dict[str, str]] = []
    for index, row in enumerate(selected_rows, start=1):
        name = f"{index:0{width}d}_{task_slug(str(row['task_id']))}"
        manifest_path = output_manifest_dir / f"{name}.jsonl"
        entry = attach_project_routing_metadata(
            build_retry_entry(
                row=row,
                task_rows=selected_task_rows[str(row["task_id"])],
                results_root=results_root,
                server=server,
                data_dir=data_dir,
                difficulty=difficulty,
                campaign=campaign,
                codex_bin=codex_bin,
                codex_timeout_seconds=codex_timeout_seconds,
                api_key_env=api_key_env,
            ),
            task_id=str(row["task_id"]),
            metadata_by_task=metadata_by_task,
            project_stats=project_stats,
        )
        manifest_path.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
        queue_items.append(
            {
                "name": name,
                "manifest": str(manifest_path),
                "task_id": str(row["task_id"]),
                "output_root": output_root,
            }
        )

    payload = {"items": queue_items}
    output_queue_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "trajectory_index": str(trajectory_index),
        "task_source": str(task_source) if task_source else None,
        "selected_task_count": len(selected_rows),
        "output_manifest_dir": str(output_manifest_dir),
        "output_queue_file": str(output_queue_file),
        "output_root": output_root,
        "retryable_statuses": sorted(retryable_statuses),
        "status_priority": list(status_priority or ()),
        "tasks_json": str(tasks_json.resolve()),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a queue for currently static retryable tasks from trajectory_index.jsonl.")
    parser.add_argument("--trajectory-index", type=Path, required=True)
    parser.add_argument("--task-source", type=Path)
    parser.add_argument("--results-root", type=Path, default=Path("codex_rescue_runs_local"))
    parser.add_argument("--output-manifest-dir", type=Path, required=True)
    parser.add_argument("--output-queue-file", type=Path, required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--difficulty", default="level1")
    parser.add_argument("--campaign", default="focus123_retryable")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-timeout-seconds", type=int, default=5400)
    parser.add_argument("--api-key-env", default="CYBERGYM_API_KEY")
    parser.add_argument("--tasks-json", type=Path, default=Path("cybergym_data/tasks.json"))
    parser.add_argument(
        "--retryable-status",
        action="append",
        dest="retryable_statuses",
        default=[],
        help="Repeatable. Defaults to standard static retryable statuses if omitted.",
    )
    parser.add_argument(
        "--status-priority",
        action="append",
        default=[],
        help="Repeatable or comma-separated. Earlier statuses are queued first.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    retryable_statuses = set(args.retryable_statuses or DEFAULT_RETRYABLE_STATUSES)
    priority_parts: list[str] = []
    for raw in args.status_priority:
        priority_parts.extend(part.strip() for part in str(raw).split(","))
    status_priority = tuple(part for part in priority_parts if part)
    summary = write_static_retry_queue(
        trajectory_index=args.trajectory_index,
        task_source=args.task_source,
        results_root=args.results_root,
        output_manifest_dir=args.output_manifest_dir,
        output_queue_file=args.output_queue_file,
        output_root=args.output_root,
        server=args.server,
        data_dir=args.data_dir,
        difficulty=args.difficulty,
        campaign=args.campaign,
        retryable_statuses=retryable_statuses,
        status_priority=status_priority or None,
        codex_bin=args.codex_bin,
        codex_timeout_seconds=args.codex_timeout_seconds,
        api_key_env=args.api_key_env,
        tasks_json=args.tasks_json,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
