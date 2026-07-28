from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from build_static_retry_queue import (
    collect_recent_source_run_roots,
    has_verified_success,
    is_active_row,
    load_trajectory_rows,
    row_order_key,
)
from project_routing import (
    DEFAULT_TASKS_JSON,
    build_project_stats,
    load_task_metadata,
    task_project_name,
    task_route_key,
    task_route_scope,
)


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def classify_reflection_bucket(
    *,
    attempt_count: int,
    status_counts: Counter[str],
    verdict_counts: Counter[str],
) -> tuple[str, str, str]:
    no_vul_count = int(status_counts.get("no_vul_crash", 0))
    codex_failed_count = int(status_counts.get("codex_failed", 0))
    submission_fail_count = sum(
        int(status_counts.get(name, 0))
        for name in ("invalid_result", "no_submission", "submission_error", "missing_result")
    )
    fix_crash_count = int(status_counts.get("fix_also_crashes", 0))
    non_diff_count = int(verdict_counts.get("non_differential", 0))

    if no_vul_count >= max(3, attempt_count // 2):
        return (
            "repeated_no_vul_crash",
            "Repeated submissions still do not hit the vulnerable path.",
            "Re-check crash preconditions, seed shaping, and target reachability before spending more broad retries.",
        )
    if codex_failed_count >= max(3, attempt_count // 2):
        return (
            "repeated_codex_failed",
            "The agent keeps failing before producing a usable PoC.",
            "Tighten prompt context, reduce noise, and consider a focused Grok/manual deep dive on the repo-specific blocker.",
        )
    if submission_fail_count >= max(2, attempt_count // 2):
        return (
            "repeated_submission_failures",
            "The runs keep ending in malformed or missing submissions.",
            "Audit submit flow assumptions, PoC file naming, and output packaging before requeueing again.",
        )
    if fix_crash_count >= 2 or non_diff_count >= max(4, attempt_count):
        return (
            "repeated_fix_regression",
            "The PoC tends to break both vuln and fixed builds or loses differential behavior.",
            "Bias toward safer/minimized triggers and inspect fix-path behavior before another batch retry.",
        )
    return (
        "mixed_plateau",
        "The task has many failed attempts without one dominant failure mode.",
        "Escalate to targeted repo-level analysis and avoid spending unlimited generic retries on this plateau.",
    )


def build_task_report_rows(
    *,
    trajectory_index: Path,
    results_root: Path,
    tasks_json: Path,
    min_attempts: int,
    limit: int,
) -> list[dict[str, Any]]:
    rows = load_trajectory_rows(trajectory_index.resolve())
    metadata_by_task = load_task_metadata(tasks_json.resolve())
    project_stats = build_project_stats(trajectory_index.resolve(), metadata_by_task=metadata_by_task)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        grouped.setdefault(task_id, []).append(row)

    report_rows: list[dict[str, Any]] = []
    for task_id, task_rows in grouped.items():
        task_rows.sort(key=row_order_key)
        if len(task_rows) < min_attempts:
            continue
        if any(has_verified_success(row) for row in task_rows):
            continue
        latest = task_rows[-1]
        if is_active_row(latest):
            continue

        status_counts = Counter(str(row.get("status") or "<missing>") for row in task_rows)
        failure_counts = Counter(str(row.get("failure_category") or row.get("status") or "<missing>") for row in task_rows)
        verdict_counts: Counter[str] = Counter()
        for row in task_rows:
            for verdict, count in (row.get("verdict_counts") or {}).items():
                try:
                    verdict_counts[str(verdict)] += int(count)
                except (TypeError, ValueError):
                    continue

        bucket, reflection, suggested_action = classify_reflection_bucket(
            attempt_count=len(task_rows),
            status_counts=status_counts,
            verdict_counts=verdict_counts,
        )
        project_name = task_project_name(task_id, metadata_by_task)
        route_key = task_route_key(task_id, metadata_by_task)
        route_scope = task_route_scope(task_id, metadata_by_task)
        project_row = project_stats.get(route_key) or {}
        report_rows.append(
            {
                "task_id": task_id,
                "task_family": str(latest.get("task_family") or task_id.split(":", 1)[0]),
                "project_name": project_name,
                "route_key": route_key,
                "route_scope": route_scope,
                "attempt_count": len(task_rows),
                "latest_status": str(latest.get("status") or ""),
                "latest_failure_category": str(latest.get("failure_category") or latest.get("status") or ""),
                "latest_started_at": latest.get("started_at"),
                "latest_run_root": str((results_root / str(latest.get("run_root") or "")).resolve()),
                "recent_run_roots": collect_recent_source_run_roots(task_rows, results_root=results_root),
                "status_counts": dict(sorted(status_counts.items())),
                "failure_category_counts": dict(sorted(failure_counts.items())),
                "verdict_counts": dict(sorted(verdict_counts.items())),
                "reflection_bucket": bucket,
                "reflection": reflection,
                "suggested_action": suggested_action,
                "project_attempted": int(project_row.get("attempted") or 0),
                "project_success": int(project_row.get("success") or 0),
                "project_success_rate_attempted": project_row.get("success_rate_attempted"),
                "project_priority_score": project_row.get("priority_score"),
            }
        )

    bucket_priority = {
        "repeated_no_vul_crash": 0,
        "repeated_fix_regression": 1,
        "repeated_submission_failures": 2,
        "repeated_codex_failed": 3,
        "mixed_plateau": 4,
    }
    report_rows.sort(
        key=lambda row: (
            bucket_priority.get(str(row["reflection_bucket"]), 99),
            -int(row["attempt_count"]),
            str(row.get("latest_started_at") or ""),
            str(row["task_id"]),
        ),
        reverse=False,
    )
    return report_rows[:limit]


def build_report_payload(
    *,
    trajectory_index: Path,
    results_root: Path,
    tasks_json: Path,
    min_attempts: int,
    limit: int,
) -> dict[str, Any]:
    rows = build_task_report_rows(
        trajectory_index=trajectory_index,
        results_root=results_root,
        tasks_json=tasks_json,
        min_attempts=min_attempts,
        limit=limit,
    )
    bucket_counts = Counter(str(row["reflection_bucket"]) for row in rows)
    latest_status_counts = Counter(str(row["latest_status"]) for row in rows)
    return {
        "generated_at": now_utc(),
        "trajectory_index": str(trajectory_index.resolve()),
        "results_root": str(results_root.resolve()),
        "tasks_json": str(tasks_json.resolve()),
        "min_attempts": min_attempts,
        "task_count": len(rows),
        "reflection_bucket_counts": dict(sorted(bucket_counts.items())),
        "latest_status_counts": dict(sorted(latest_status_counts.items())),
        "tasks": rows,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Stubborn Failure Report",
        "",
        f"Generated at: `{payload.get('generated_at')}`",
        "",
        "## Summary",
        "",
        f"- Minimum attempts: `{payload.get('min_attempts')}`",
        f"- Reported stubborn tasks: `{payload.get('task_count')}`",
        f"- Reflection buckets: `{json.dumps(payload.get('reflection_bucket_counts', {}), sort_keys=True)}`",
        f"- Latest statuses: `{json.dumps(payload.get('latest_status_counts', {}), sort_keys=True)}`",
        "",
        "## Top Stubborn Tasks",
        "",
        "| task_id | project | attempts | latest_status | bucket | reflection | suggested_action |",
        "|---|---|---:|---|---|---|---|",
    ]
    for row in payload.get("tasks") or []:
        lines.append(
            f"| `{row['task_id']}` | `{row['project_name']}` | `{row['attempt_count']}` | "
            f"`{row['latest_status']}` | `{row['reflection_bucket']}` | "
            f"{row['reflection']} | {row['suggested_action']} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export stubborn unsolved tasks with reason buckets and retry reflections.")
    parser.add_argument("--trajectory-index", type=Path, default=Path("codex_rescue_runs_local/trajectory_index.jsonl"))
    parser.add_argument("--results-root", type=Path, default=Path("codex_rescue_runs_local"))
    parser.add_argument("--tasks-json", type=Path, default=DEFAULT_TASKS_JSON)
    parser.add_argument("--min-attempts", type=int, default=3)
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_report_payload(
        trajectory_index=args.trajectory_index,
        results_root=args.results_root,
        tasks_json=args.tasks_json,
        min_attempts=args.min_attempts,
        limit=args.limit,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.output_md is not None:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(render_markdown(payload) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output_json": str(args.output_json.resolve()),
                "output_md": str(args.output_md.resolve()) if args.output_md is not None else None,
                "task_count": payload["task_count"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
