#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_WAVES = (
    "official_level1_quick_win_priority_a_2026-07-29",
    "official_level1_quick_win_priority_b_2026-07-29",
)
DISCOVERABLE_WAVE_RE = re.compile(
    r"^official_level1_quick_win_priority_[ab]_\d{4}-\d{2}-\d{2}(?:_d\d+)?$"
)


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def read_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def collect_completed_rows(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    completed: list[dict[str, Any]] = []
    for row in tasks:
        status = str(row.get("status") or "")
        if status == "active_no_result":
            continue
        run_root = Path(str(row.get("run_root") or ""))
        audit = read_json_if_exists(run_root / "official_audit.json") if run_root else {}
        completed.append(
            {
                "task_id": row.get("task_id"),
                "status": status,
                "run_root": row.get("run_root"),
                "official_reason": ((audit.get("official_final_submission") or {}).get("reason")),
                "candidate_path": ((audit.get("final_message") or {}).get("candidate_path")),
                "local_verification": ((audit.get("final_message") or {}).get("local_verification")),
            }
        )
    return completed


def collect_wave_summary(reports_root: Path, wave_name: str) -> dict[str, Any]:
    wave_dir = reports_root / wave_name
    state = read_json_if_exists(wave_dir / "combined_queue.binary.state.json")
    live = read_json_if_exists(wave_dir / "live_progress.json")
    tasks = list(live.get("tasks") or [])
    completed_rows = collect_completed_rows(tasks)
    completed_status_counts: dict[str, int] = {}
    for row in completed_rows:
        completed_status_counts[row["status"]] = completed_status_counts.get(row["status"], 0) + 1
    active_samples = [
        {
            "task_id": row.get("task_id"),
            "status": row.get("status"),
            "elapsed_seconds": row.get("elapsed_seconds"),
            "run_root": row.get("run_root"),
        }
        for row in tasks
        if str(row.get("status") or "") == "active_no_result"
    ]
    return {
        "wave_name": wave_name,
        "wave_dir": str(wave_dir.resolve()),
        "queue_complete": bool(state.get("queue_complete")),
        "queue_item_count": int(state.get("queue_item_count") or 0),
        "processed_name_count": len(list(state.get("processed_names") or [])),
        "processed_task_count": len(list(state.get("processed_task_ids") or [])),
        "pending_item_count": int(state.get("pending_item_count") or 0),
        "deferred_item_count": int(state.get("deferred_item_count") or 0),
        "launched_task_lock_count": int(state.get("launched_task_lock_count") or 0),
        "local_active": int(state.get("local_active") or 0),
        "global_active": int(state.get("global_active") or 0),
        "local_limit": int(state.get("local_limit") or 0),
        "global_limit": state.get("global_limit"),
        "launch_slots": int(state.get("launch_slots") or 0),
        "blocked_reason": state.get("blocked_reason"),
        "deferred_task_ids": list(state.get("deferred_task_ids") or []),
        "pending_task_ids_preview": list(state.get("pending_task_ids_preview") or []),
        "state_updated_at": state.get("updated_at"),
        "live_generated_at": live.get("generated_at"),
        "live_active_task_count": int(live.get("active_task_count") or 0),
        "live_latest_task_count": int(live.get("latest_task_count") or 0),
        "completed_task_count": len(completed_rows),
        "completed_status_counts": completed_status_counts,
        "live_final_submission_success_task_count": int(
            (live.get("summary") or {}).get("final_submission_success_task_count")
            or live.get("final_submission_success_task_count")
            or 0
        ),
        "active_samples": active_samples,
        "completed_rows": completed_rows,
    }


def discover_default_waves(reports_root: Path) -> tuple[str, ...]:
    discovered: list[str] = []
    for path in sorted(reports_root.iterdir()):
        if not path.is_dir():
            continue
        if not DISCOVERABLE_WAVE_RE.fullmatch(path.name):
            continue
        if not (path / "combined_queue.binary.state.json").exists():
            continue
        if not (path / "live_progress.json").exists():
            continue
        discovered.append(path.name)
    return tuple(discovered) if discovered else DEFAULT_WAVES


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export an operator summary for the active official quick-win campaign."
    )
    parser.add_argument(
        "--reports-root",
        type=Path,
        default=Path("reports"),
    )
    parser.add_argument(
        "--wave",
        action="append",
        default=[],
        help="Specific quick-win wave directory name(s) under reports/.",
    )
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=Path("reports/official_level1_quick_win_campaign_2026-07-29"),
    )
    args = parser.parse_args()

    wave_names = tuple(args.wave) if args.wave else discover_default_waves(args.reports_root.resolve())
    rows = [collect_wave_summary(args.reports_root.resolve(), wave_name) for wave_name in wave_names]

    payload = {
        "generated_at": now_utc(),
        "reports_root": str(args.reports_root.resolve()),
        "wave_count": len(rows),
        "summary": {
            "queue_item_count": sum(int(row.get("queue_item_count") or 0) for row in rows),
            "processed_task_count": sum(int(row.get("processed_task_count") or 0) for row in rows),
            "pending_item_count": sum(int(row.get("pending_item_count") or 0) for row in rows),
            "deferred_item_count": sum(int(row.get("deferred_item_count") or 0) for row in rows),
            "live_active_task_count": sum(int(row.get("live_active_task_count") or 0) for row in rows),
            "completed_task_count": sum(int(row.get("completed_task_count") or 0) for row in rows),
            "final_submission_success_task_count": sum(
                int(row.get("live_final_submission_success_task_count") or 0) for row in rows
            ),
        },
        "waves": rows,
    }

    json_path = args.output_prefix.with_suffix(".json")
    md_path = args.output_prefix.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# Official Level1 Quick-Win Campaign",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Wave count: `{payload['wave_count']}`",
        f"- Queue items: `{payload['summary']['queue_item_count']}`",
        f"- Processed tasks: `{payload['summary']['processed_task_count']}`",
        f"- Pending items: `{payload['summary']['pending_item_count']}`",
        f"- Deferred items: `{payload['summary']['deferred_item_count']}`",
        f"- Live active tasks: `{payload['summary']['live_active_task_count']}`",
        f"- Completed tasks: `{payload['summary']['completed_task_count']}`",
        f"- Final-submission successes in these waves: `{payload['summary']['final_submission_success_task_count']}`",
        "",
    ]
    for row in rows:
        lines.extend(
            [
                f"## {row['wave_name']}",
                "",
                f"- Queue complete: `{row['queue_complete']}`",
                f"- Queue items: `{row['queue_item_count']}` | processed=`{row['processed_task_count']}` | pending=`{row['pending_item_count']}` | deferred=`{row['deferred_item_count']}`",
                f"- Launcher capacity: local_active=`{row['local_active']}` / local_limit=`{row['local_limit']}` | launch_slots=`{row['launch_slots']}` | blocked_reason=`{row['blocked_reason']}`",
                f"- Live active tasks: `{row['live_active_task_count']}` | completed=`{row['completed_task_count']}` | successes=`{row['live_final_submission_success_task_count']}`",
                f"- State updated at: `{row['state_updated_at']}`",
            ]
        )
        if row["pending_task_ids_preview"]:
            lines.append("- Pending task preview: " + ", ".join(f"`{item}`" for item in row["pending_task_ids_preview"][:12]))
        if row["deferred_task_ids"]:
            lines.append("- Deferred task ids: " + ", ".join(f"`{item}`" for item in row["deferred_task_ids"][:12]))
        if row["completed_status_counts"]:
            lines.append("- Completed status counts:")
            for key, value in sorted(row["completed_status_counts"].items()):
                lines.append(f"  - `{key}` = `{value}`")
        if row["active_samples"]:
            lines.append("- Active samples:")
            for sample in row["active_samples"][:6]:
                lines.append(
                    f"  - `{sample['task_id']}` | status=`{sample['status']}` | elapsed_s=`{sample['elapsed_seconds']}` | run=`{sample['run_root']}`"
                )
        if row["completed_rows"]:
            lines.append("- Completed samples:")
            for sample in row["completed_rows"][:6]:
                lines.append(
                    f"  - `{sample['task_id']}` | status=`{sample['status']}` | reason=`{sample['official_reason']}` | candidate=`{sample['candidate_path']}`"
                )
        lines.append("")
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "output_json": str(json_path.resolve()),
                "output_md": str(md_path.resolve()),
                "wave_count": payload["wave_count"],
                "pending_item_count": payload["summary"]["pending_item_count"],
                "live_active_task_count": payload["summary"]["live_active_task_count"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
