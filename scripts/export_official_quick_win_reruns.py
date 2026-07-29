#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


QUEUE_PRIORITY = (
    "likely_missed_candidate",
    "crash_evidence_gap",
    "watchdog_format_loss",
    "rebuilt_vs_server_mismatch",
    "bundled_target_mismatch",
)


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def unique_task_ids(rows: list[dict[str, Any]]) -> list[str]:
    seen: OrderedDict[str, None] = OrderedDict()
    for row in rows:
        task_id = str(row.get("task_id") or "").strip()
        if not task_id or task_id in seen:
            continue
        seen[task_id] = None
    return list(seen.keys())


def write_task_file(path: Path, task_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(task_ids)
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export rerun task lists for the official Level1 quick-win queues."
    )
    parser.add_argument(
        "--quick-wins",
        type=Path,
        default=Path("reports/official_level1_quick_wins_2026-07-29.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/official_level1_quick_win_reruns_2026-07-29"),
    )
    args = parser.parse_args()

    payload = read_json(args.quick_wins)
    queues = dict(payload.get("queues") or {})
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    per_queue: dict[str, dict[str, Any]] = {}
    for queue_name in QUEUE_PRIORITY:
        rows = list(queues.get(queue_name) or [])
        task_ids = unique_task_ids(rows)
        task_file = output_dir / f"{queue_name}.tasks.md"
        write_task_file(task_file, task_ids)
        per_queue[queue_name] = {
            "task_count": len(task_ids),
            "task_ids": task_ids,
            "task_file": str(task_file.resolve()),
        }

    priority_a = unique_task_ids(
        list(queues.get("likely_missed_candidate") or [])
        + list(queues.get("crash_evidence_gap") or [])
        + list(queues.get("watchdog_format_loss") or [])
    )
    priority_b = unique_task_ids(
        list(queues.get("rebuilt_vs_server_mismatch") or [])
        + list(queues.get("bundled_target_mismatch") or [])
    )
    combined = unique_task_ids(
        [
            {"task_id": task_id}
            for task_id in priority_a + priority_b
        ]
    )

    priority_a_path = output_dir / "priority_a_format_and_submission_recovery.tasks.md"
    priority_b_path = output_dir / "priority_b_mismatch_triage.tasks.md"
    combined_path = output_dir / "all_quick_wins.tasks.md"
    write_task_file(priority_a_path, priority_a)
    write_task_file(priority_b_path, priority_b)
    write_task_file(combined_path, combined)

    launch_examples = {
        "priority_a": "bash scripts/launch_split_group_binary_wave.sh reports/official_level1_quick_win_reruns_2026-07-29/priority_a_format_and_submission_recovery.tasks.md official_level1_quick_win_priority_a_2026-07-29",
        "priority_b": "bash scripts/launch_split_group_binary_wave.sh reports/official_level1_quick_win_reruns_2026-07-29/priority_b_mismatch_triage.tasks.md official_level1_quick_win_priority_b_2026-07-29",
        "all_quick_wins": "bash scripts/launch_split_group_binary_wave.sh reports/official_level1_quick_win_reruns_2026-07-29/all_quick_wins.tasks.md official_level1_quick_win_all_2026-07-29",
    }

    export_payload = {
        "generated_at": now_utc(),
        "quick_wins_path": str(args.quick_wins.resolve()),
        "per_queue": per_queue,
        "priority_groups": {
            "priority_a_format_and_submission_recovery": {
                "task_count": len(priority_a),
                "task_ids": priority_a,
                "task_file": str(priority_a_path.resolve()),
            },
            "priority_b_mismatch_triage": {
                "task_count": len(priority_b),
                "task_ids": priority_b,
                "task_file": str(priority_b_path.resolve()),
            },
            "all_quick_wins": {
                "task_count": len(combined),
                "task_ids": combined,
                "task_file": str(combined_path.resolve()),
            },
        },
        "launch_examples": launch_examples,
    }

    json_path = output_dir / "summary.json"
    md_path = output_dir / "README.md"
    selection_json_path = output_dir / "task_selection.json"
    json_path.write_text(json.dumps(export_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    selection_json_path.write_text(
        json.dumps(
            {
                "selection_policy": "official_quick_win_priority_groups",
                "generated_at": export_payload["generated_at"],
                "priority_groups": {
                    key: {
                        "task_count": value["task_count"],
                        "task_ids": value["task_ids"],
                    }
                    for key, value in export_payload["priority_groups"].items()
                },
                "queue_counts": {
                    key: value["task_count"] for key, value in per_queue.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Official Level1 Quick-Win Reruns",
        "",
        f"- Generated at: `{export_payload['generated_at']}`",
        f"- Source quick wins: `{export_payload['quick_wins_path']}`",
        "",
        "These files are operator-side retry queues. They do not authorize the",
        "agent to read other run directories, saved PoCs, or prior-run candidate",
        "artifacts during an official Level1 attempt.",
        "",
        "## Priority groups",
        "",
        f"- `priority_a_format_and_submission_recovery`: `{len(priority_a)}` tasks | file=`{priority_a_path.resolve()}`",
        f"- `priority_b_mismatch_triage`: `{len(priority_b)}` tasks | file=`{priority_b_path.resolve()}`",
        f"- `all_quick_wins`: `{len(combined)}` tasks | file=`{combined_path.resolve()}`",
        "",
        "Priority A is the first clean recovery lane: likely missed submissions, one crash-evidence gap, and watchdog-driven formatting losses.",
        "",
        "## Per-queue files",
        "",
    ]
    for queue_name in QUEUE_PRIORITY:
        queue_payload = per_queue[queue_name]
        lines.append(
            f"- `{queue_name}`: `{queue_payload['task_count']}` tasks | file=`{queue_payload['task_file']}`"
        )
    lines.extend(
        [
            "",
            "## Launch examples",
            "",
            f"- Priority A: `{launch_examples['priority_a']}`",
            f"- Priority B: `{launch_examples['priority_b']}`",
            f"- All quick wins: `{launch_examples['all_quick_wins']}`",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "output_json": str(json_path.resolve()),
                "output_md": str(md_path.resolve()),
                "priority_a_task_count": len(priority_a),
                "priority_b_task_count": len(priority_b),
                "all_quick_wins_task_count": len(combined),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
