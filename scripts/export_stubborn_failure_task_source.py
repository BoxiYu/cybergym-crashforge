from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


BUCKET_OWNER = {
    "repeated_codex_failed": "grok",
    "mixed_plateau": "grok",
    "repeated_fix_regression": "codex",
    "repeated_submission_failures": "codex",
    "repeated_no_vul_crash": "codex",
}

BUCKET_PRIORITY_BONUS = {
    "repeated_fix_regression": 28.0,
    "repeated_submission_failures": 24.0,
    "repeated_codex_failed": 22.0,
    "repeated_no_vul_crash": 18.0,
    "mixed_plateau": 12.0,
}

STATUS_PRIORITY_BONUS = {
    "invalid_result": 10.0,
    "fix_also_crashes": 9.0,
    "codex_failed": 8.0,
    "no_submission": 7.0,
    "submission_error": 7.0,
    "missing_result": 6.0,
    "no_vul_crash": 5.0,
}


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def owner_for_task(task: dict[str, Any]) -> str:
    bucket = str(task.get("reflection_bucket") or "")
    owner = BUCKET_OWNER.get(bucket, "codex")
    project_priority = float(task.get("project_priority_score") or 0.0)
    project_success_rate = float(task.get("project_success_rate_attempted") or 0.0)
    latest_status = str(task.get("latest_status") or "")
    if owner == "grok" and latest_status in {"invalid_result", "fix_also_crashes"}:
        return "codex"
    if owner == "codex" and bucket == "mixed_plateau" and project_priority >= 0.9 and project_success_rate >= 0.75:
        return "grok"
    return owner


def triage_score(task: dict[str, Any]) -> float:
    bucket = str(task.get("reflection_bucket") or "")
    latest_status = str(task.get("latest_status") or "")
    attempt_count = int(task.get("attempt_count") or 0)
    project_priority = float(task.get("project_priority_score") or 0.0)
    project_success_rate = float(task.get("project_success_rate_attempted") or 0.0)
    score = project_priority * 100.0
    score += project_success_rate * 35.0
    score += min(attempt_count, 8) * 2.5
    score += BUCKET_PRIORITY_BONUS.get(bucket, 0.0)
    score += STATUS_PRIORITY_BONUS.get(latest_status, 0.0)
    return round(score, 2)


def build_assignment(task: dict[str, Any]) -> dict[str, Any]:
    owner = owner_for_task(task)
    score = triage_score(task)
    return {
        "task_id": task.get("task_id"),
        "task_family": task.get("task_family"),
        "project_name": task.get("project_name"),
        "route_key": task.get("route_key"),
        "route_scope": task.get("route_scope"),
        "attempt_count": task.get("attempt_count"),
        "latest_status": task.get("latest_status"),
        "latest_failure_category": task.get("latest_failure_category"),
        "reflection_bucket": task.get("reflection_bucket"),
        "reflection": task.get("reflection"),
        "suggested_action": task.get("suggested_action"),
        "project_attempted": task.get("project_attempted"),
        "project_success": task.get("project_success"),
        "project_success_rate_attempted": task.get("project_success_rate_attempted"),
        "project_priority_score": task.get("project_priority_score"),
        "recent_run_roots": list(task.get("recent_run_roots") or []),
        "latest_run_root": task.get("latest_run_root"),
        "recommended_owner": owner,
        "triage_score": score,
    }


def build_payload(stubborn_report: dict[str, Any]) -> dict[str, Any]:
    tasks = [build_assignment(task) for task in (stubborn_report.get("tasks") or [])]
    tasks.sort(
        key=lambda task: (
            -float(task.get("triage_score") or 0.0),
            -int(task.get("attempt_count") or 0),
            str(task.get("task_id") or ""),
        )
    )

    owner_counts = Counter(str(task["recommended_owner"]) for task in tasks)
    bucket_counts = Counter(str(task.get("reflection_bucket") or "") for task in tasks)
    project_counts = Counter(str(task.get("project_name") or "") for task in tasks)

    owners: dict[str, list[dict[str, Any]]] = {"codex": [], "grok": []}
    for task in tasks:
        owners.setdefault(str(task["recommended_owner"]), []).append(task)

    return {
        "generated_at": now_utc(),
        "source_generated_at": stubborn_report.get("generated_at"),
        "task_count": len(tasks),
        "owner_counts": dict(sorted(owner_counts.items())),
        "reflection_bucket_counts": dict(sorted(bucket_counts.items())),
        "top_projects": dict(project_counts.most_common(10)),
        "owners": owners,
        "tasks": tasks,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Stubborn Failure Task Source",
        "",
        f"Generated at: `{payload.get('generated_at')}`",
        f"Source stubborn report: `{payload.get('source_generated_at')}`",
        "",
        "## Summary",
        "",
        f"- Task count: `{payload.get('task_count')}`",
        f"- Owner counts: `{json.dumps(payload.get('owner_counts', {}), sort_keys=True)}`",
        f"- Reflection buckets: `{json.dumps(payload.get('reflection_bucket_counts', {}), sort_keys=True)}`",
        f"- Top projects: `{json.dumps(payload.get('top_projects', {}), sort_keys=True)}`",
        "",
    ]
    for owner in ("codex", "grok"):
        lines.extend(
            [
                f"## {owner.title()} Queue",
                "",
                "| task_id | project | bucket | status | attempts | triage_score | suggested_action |",
                "|---|---|---|---|---:|---:|---|",
            ]
        )
        for task in payload.get("owners", {}).get(owner, []):
            lines.append(
                f"| `{task['task_id']}` | `{task['project_name']}` | `{task['reflection_bucket']}` | "
                f"`{task['latest_status']}` | `{task['attempt_count']}` | `{task['triage_score']}` | "
                f"{task['suggested_action']} |"
            )
        lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export stubborn failures into Codex/Grok task queues.")
    parser.add_argument("--input-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stubborn_report = load_json(args.input_json)
    payload = build_payload(stubborn_report)
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
                "owner_counts": payload["owner_counts"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
