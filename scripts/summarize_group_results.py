from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def parse_result_timestamp(value: Any) -> float:
    if not value or not isinstance(value, str):
        return 0.0
    normalized = value.replace("Z", "+00:00").replace(" ", "T")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_task_ids(path: Path) -> list[str]:
    task_ids: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.search(r"(arvo|oss-fuzz):[^\s`]+", line)
        if match:
            task_ids.append(match.group(0))
    return task_ids


def has_success(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    if row.get("status") == "success" or row.get("has_verified_success"):
        return True
    verdict_counts = row.get("verdict_counts") or {}
    try:
        return int(verdict_counts.get("verified_success") or 0) > 0
    except (TypeError, ValueError):
        return False


def summarize_group(
    rows: list[dict[str, Any]],
    task_ids: list[str],
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        task_id = row.get("task_id")
        if task_id:
            grouped.setdefault(str(task_id), []).append(row)

    summary: dict[str, dict[str, Any]] = {}
    for task_id in task_ids:
        task_rows = sorted(
            grouped.get(task_id, []),
            key=lambda row: (
                parse_result_timestamp(row.get("started_at")) or parse_result_timestamp(row.get("ended_at")),
                str(row.get("run_id") or row.get("run_root") or ""),
            ),
        )
        latest = task_rows[-1] if task_rows else None
        summary[task_id] = {
            "attempted": bool(task_rows),
            "latest_status": latest.get("status") if latest else None,
            "latest_success": has_success(latest),
            "ever_success": any(has_success(row) for row in task_rows),
        }

    latest_status_counts = Counter((item["latest_status"] or "unattempted") for item in summary.values())
    attempted = sum(1 for item in summary.values() if item["attempted"])
    latest_success = sum(1 for item in summary.values() if item["latest_success"])
    ever_success = sum(1 for item in summary.values() if item["ever_success"])
    total = len(task_ids)
    return {
        "total": total,
        "attempted": attempted,
        "latest_success": latest_success,
        "ever_success": ever_success,
        "latest_pass_rate_all": round(latest_success / total * 100, 1) if total else 0.0,
        "latest_pass_rate_attempted": round(latest_success / attempted * 100, 1) if attempted else None,
        "latest_status_counts": dict(sorted(latest_status_counts.items())),
        "tasks": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize latest/ever success metrics for a task group.")
    parser.add_argument("--trajectory-index", type=Path, required=True)
    parser.add_argument("--group", type=Path, required=True)
    parser.add_argument("--easy-group", type=Path)
    parser.add_argument("--hard-group", type=Path)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()

    rows = load_rows(args.trajectory_index)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "group": summarize_group(rows, load_task_ids(args.group)),
    }
    if args.easy_group:
        payload["easy"] = summarize_group(rows, load_task_ids(args.easy_group))
    if args.hard_group:
        payload["hard"] = summarize_group(rows, load_task_ids(args.hard_group))

    content = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(content, encoding="utf-8")
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
