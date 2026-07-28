from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASKS_JSON = ROOT / "cybergym_data" / "tasks.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_result_timestamp(value: Any) -> float:
    if not value or not isinstance(value, str):
        return 0.0
    normalized = value.replace("Z", "+00:00").replace(" ", "T")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def load_task_metadata(tasks_json: Path | None = None) -> dict[str, dict[str, Any]]:
    payload = read_json((tasks_json or DEFAULT_TASKS_JSON).resolve())
    return {
        str(item["task_id"]): dict(item)
        for item in payload
        if isinstance(item, dict) and item.get("task_id")
    }


def task_project_name(task_id: str, metadata_by_task: dict[str, dict[str, Any]]) -> str:
    metadata = metadata_by_task.get(task_id) or {}
    return str(metadata.get("project_name") or "<unknown>")


def task_route_key(task_id: str, metadata_by_task: dict[str, dict[str, Any]]) -> str:
    metadata = metadata_by_task.get(task_id) or {}
    repo = str(metadata.get("project_main_repo") or "").strip()
    if repo:
        return repo
    return task_project_name(task_id, metadata_by_task)


def task_route_scope(task_id: str, metadata_by_task: dict[str, dict[str, Any]]) -> str:
    metadata = metadata_by_task.get(task_id) or {}
    return "repo" if metadata.get("project_main_repo") else "project"


def project_policy_timeout(policy: str, *, baseline_timeout: int) -> int:
    if policy == "fastlane":
        return max(baseline_timeout, 7200)
    if policy == "sample_first":
        return min(baseline_timeout, 4800)
    if policy == "conservative":
        return min(baseline_timeout, 3600)
    return baseline_timeout


def build_project_stats(
    trajectory_index: Path,
    *,
    metadata_by_task: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    latest_by_task: dict[str, dict[str, Any]] = {}
    order_by_task: dict[str, tuple[float, float, str]] = {}
    for raw_line in trajectory_index.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        row = json.loads(raw_line)
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        order_key = (
            parse_result_timestamp(row.get("started_at")),
            parse_result_timestamp(row.get("ended_at")),
            str(row.get("run_root") or ""),
        )
        if task_id not in latest_by_task or order_key > order_by_task[task_id]:
            latest_by_task[task_id] = row
            order_by_task[task_id] = order_key

    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "route_key": "<unknown>",
            "route_scope": "project",
            "project_name": "<unknown>",
            "total": 0,
            "attempted": 0,
            "success": 0,
            "no_vul": 0,
            "codex_failed": 0,
        }
    )
    for task_id, metadata in metadata_by_task.items():
        route_key = task_route_key(task_id, metadata_by_task)
        project_name = task_project_name(task_id, metadata_by_task)
        route_scope = task_route_scope(task_id, metadata_by_task)
        grouped[route_key]["route_key"] = route_key
        grouped[route_key]["route_scope"] = route_scope
        grouped[route_key]["project_name"] = project_name
        if metadata.get("project_main_repo"):
            grouped[route_key]["project_main_repo"] = str(metadata["project_main_repo"])
        grouped[route_key]["total"] += 1
        row = latest_by_task.get(task_id)
        if row is None:
            continue
        grouped[route_key]["attempted"] += 1
        status = str(row.get("status") or "")
        verdict_counts = row.get("verdict_counts") or {}
        has_success = bool(row.get("has_verified_success")) or status == "success"
        try:
            has_success = has_success or int(verdict_counts.get("verified_success") or 0) > 0
        except (TypeError, ValueError):
            pass
        if has_success:
            grouped[route_key]["success"] += 1
        if status == "no_vul_crash":
            grouped[route_key]["no_vul"] += 1
        if status == "codex_failed":
            grouped[route_key]["codex_failed"] += 1

    stats: dict[str, dict[str, Any]] = {}
    for route_key, row in grouped.items():
        attempted = int(row["attempted"])
        success = int(row["success"])
        no_vul = int(row["no_vul"])
        codex_failed = int(row["codex_failed"])
        success_rate = (success / attempted) if attempted else None
        score = 0.45 if success_rate is None else success_rate
        if attempted >= 5 and success == 0 and codex_failed >= max(3, attempted // 2):
            score -= 0.35
        if attempted >= 5 and no_vul >= max(4, attempted // 2):
            score -= 0.15
        if success >= 3:
            score += 0.05
        row["success_rate_attempted"] = success_rate
        row["priority_score"] = score
        stats[route_key] = row
    return stats


def task_priority_key(
    task_id: str,
    *,
    metadata_by_task: dict[str, dict[str, Any]],
    project_stats: dict[str, dict[str, Any]],
) -> tuple[float, int, str, str]:
    route_key = task_route_key(task_id, metadata_by_task)
    project_name = task_project_name(task_id, metadata_by_task)
    stats = project_stats.get(route_key) or {}
    score = float(stats.get("priority_score", 0.45))
    attempted = int(stats.get("attempted") or 0)
    return (-score, -attempted, project_name, task_id)


def project_retry_threshold(
    task_id: str,
    *,
    metadata_by_task: dict[str, dict[str, Any]],
    project_stats: dict[str, dict[str, Any]],
    default_threshold: int = 8,
) -> int:
    route_key = task_route_key(task_id, metadata_by_task)
    stats = project_stats.get(route_key) or {}
    attempted = int(stats.get("attempted") or 0)
    success = int(stats.get("success") or 0)
    no_vul = int(stats.get("no_vul") or 0)
    codex_failed = int(stats.get("codex_failed") or 0)
    success_rate = stats.get("success_rate_attempted")
    if success_rate is None:
        return default_threshold
    if attempted >= 8 and success_rate >= 0.8:
        return 12
    if attempted >= 6 and success_rate >= 0.65:
        return 10
    if attempted >= 6 and success == 0 and codex_failed >= max(4, attempted // 2):
        return 5
    if attempted >= 6 and success_rate < 0.25:
        return 6
    if attempted >= 6 and no_vul >= max(4, attempted // 2):
        return 7
    return default_threshold


def project_route_policy(
    task_id: str,
    *,
    metadata_by_task: dict[str, dict[str, Any]],
    project_stats: dict[str, dict[str, Any]],
    baseline_timeout: int = 5400,
) -> dict[str, Any]:
    route_key = task_route_key(task_id, metadata_by_task)
    stats = project_stats.get(route_key) or {}
    attempted = int(stats.get("attempted") or 0)
    success = int(stats.get("success") or 0)
    no_vul = int(stats.get("no_vul") or 0)
    codex_failed = int(stats.get("codex_failed") or 0)
    success_rate = stats.get("success_rate_attempted")

    if success_rate is None or attempted < 3:
        policy = "cold_start"
        reason = "insufficient_project_history"
    elif attempted >= 8 and success_rate >= 0.7:
        policy = "fastlane"
        reason = "high_yield_project"
    elif attempted >= 6 and no_vul >= max(4, attempted // 2):
        policy = "sample_first"
        reason = "many_no_vul_outcomes"
    elif attempted >= 6 and success == 0 and codex_failed >= max(4, attempted // 2):
        policy = "conservative"
        reason = "codex_fail_heavy_project"
    elif attempted >= 6 and success_rate < 0.25:
        policy = "conservative"
        reason = "low_yield_project"
    else:
        policy = "balanced"
        reason = "mixed_project_outcomes"

    return {
        "policy": policy,
        "reason": reason,
        "codex_timeout_seconds": project_policy_timeout(policy, baseline_timeout=baseline_timeout),
    }


def attach_project_routing_metadata(
    entry: dict[str, Any],
    *,
    task_id: str,
    metadata_by_task: dict[str, dict[str, Any]],
    project_stats: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    metadata = metadata_by_task.get(task_id) or {}
    project_name = task_project_name(task_id, metadata_by_task)
    route_key = task_route_key(task_id, metadata_by_task)
    route_scope = task_route_scope(task_id, metadata_by_task)
    stats = dict(project_stats.get(route_key) or {})
    entry["project_name"] = project_name
    if metadata.get("project_main_repo"):
        entry["project_main_repo"] = str(metadata["project_main_repo"])
    if metadata.get("project_language"):
        entry["project_language"] = str(metadata["project_language"])
    entry["project_route_scope"] = route_scope
    entry["project_route_key"] = route_key
    route_policy = project_route_policy(
        task_id,
        metadata_by_task=metadata_by_task,
        project_stats=project_stats,
        baseline_timeout=int(entry.get("codex_timeout_seconds") or 5400),
    )
    entry["skip_cumulative_no_vul_threshold"] = project_retry_threshold(
        task_id,
        metadata_by_task=metadata_by_task,
        project_stats=project_stats,
    )
    entry["project_priority_score"] = float(stats.get("priority_score", 0.45))
    entry["project_route_policy"] = str(route_policy["policy"])
    entry["project_route_reason"] = str(route_policy["reason"])
    entry["codex_timeout_seconds"] = int(route_policy["codex_timeout_seconds"])
    return entry


def summarize_projects_for_tasks(
    task_ids: list[str],
    *,
    metadata_by_task: dict[str, dict[str, Any]],
    project_stats: dict[str, dict[str, Any]],
    limit: int = 12,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for task_id in task_ids:
        route_key = task_route_key(task_id, metadata_by_task)
        project_name = task_project_name(task_id, metadata_by_task)
        if route_key in seen:
            continue
        seen.add(route_key)
        stats = dict(project_stats.get(route_key) or {})
        stats["project_name"] = project_name
        stats["route_key"] = route_key
        stats["route_scope"] = task_route_scope(task_id, metadata_by_task)
        rows.append(stats)
        if len(rows) >= limit:
            break
    return rows


def summarize_route_policies_for_tasks(
    task_ids: list[str],
    *,
    metadata_by_task: dict[str, dict[str, Any]],
    project_stats: dict[str, dict[str, Any]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task_id in task_ids:
        policy = str(
            project_route_policy(
                task_id,
                metadata_by_task=metadata_by_task,
                project_stats=project_stats,
            )["policy"]
        )
        counts[policy] = counts.get(policy, 0) + 1
    return dict(sorted(counts.items()))
