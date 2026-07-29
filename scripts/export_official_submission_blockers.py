#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export blocker reports from the official Level1 scoreboard."
    )
    parser.add_argument(
        "--scoreboard",
        type=Path,
        default=Path("reports/official_level1_scoreboard_2026-07-28.json"),
    )
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=Path("reports/official_level1_submission_blockers_2026-07-29"),
    )
    parser.add_argument("--target-rate", type=float, default=0.85)
    args = parser.parse_args()

    scoreboard = read_json(args.scoreboard)
    tasks = list(scoreboard.get("tasks") or [])
    summary = dict(scoreboard.get("summary") or {})

    final_success = int(summary.get("final_submission_success_task_count") or 0)
    submitted = int(summary.get("exactly_one_submit_task_count") or 0)
    needed_for_target = max(0, ceil(args.target_rate * submitted) - final_success) if submitted else 0

    forbidden_rows = [row for row in tasks if row.get("forbidden_access_detected")]
    forbidden_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in forbidden_rows:
        categories = row.get("forbidden_access_categories") or ["unknown"]
        for category in categories:
            forbidden_by_category[str(category)].append(row)

    reason_counter = Counter(str(row.get("official_final_submission_reason") or "unknown") for row in tasks)
    blocker_rows = [
        row
        for row in tasks
        if not row.get("counted_success_ready")
    ]
    blocker_examples_by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in blocker_rows:
        reason = str(row.get("official_final_submission_reason") or "unknown")
        if len(blocker_examples_by_reason[reason]) < 10:
            blocker_examples_by_reason[reason].append(row)

    payload = {
        "generated_at": now_utc(),
        "scoreboard_path": str(args.scoreboard.resolve()),
        "target_rate": args.target_rate,
        "submitted_task_count": submitted,
        "final_submission_success_task_count": final_success,
        "current_rate": (final_success / submitted) if submitted else 0.0,
        "additional_successes_needed_if_denominator_static": needed_for_target,
        "forbidden_access_summary": {
            "task_count": len(forbidden_rows),
            "categories": {
                category: {
                    "task_count": len(rows),
                    "tasks": [
                        {
                            "task_id": row.get("task_id"),
                            "run_root": row.get("run_root"),
                            "final_submission_success": row.get("final_submission_success"),
                            "official_final_submission_reason": row.get("official_final_submission_reason"),
                        }
                        for row in rows
                    ],
                }
                for category, rows in sorted(forbidden_by_category.items())
            },
        },
        "final_submission_reason_counts": dict(reason_counter),
        "blocker_examples_by_reason": {
            reason: [
                {
                    "task_id": row.get("task_id"),
                    "run_root": row.get("run_root"),
                    "final_submission_success": row.get("final_submission_success"),
                    "clean_local_only": row.get("clean_local_only"),
                    "evidence_complete": row.get("evidence_complete"),
                    "forbidden_access_detected": row.get("forbidden_access_detected"),
                }
                for row in rows
            ]
            for reason, rows in sorted(blocker_examples_by_reason.items())
        },
    }

    json_path = args.output_prefix.with_suffix(".json")
    md_path = args.output_prefix.with_suffix(".md")
    csv_path = args.output_prefix.with_suffix(".csv")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# Official Level1 Submission Blockers",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Scoreboard: `{payload['scoreboard_path']}`",
        f"- Final-submission success: `{final_success}`",
        f"- Submitted tasks: `{submitted}`",
        f"- Current rate: `{payload['current_rate']:.2%}`",
        f"- Additional successes needed at static denominator for `{args.target_rate:.0%}`: `{needed_for_target}`",
        "",
        "## Final-submission reason counts",
        "",
    ]
    for reason, count in sorted(reason_counter.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{reason}`: `{count}`")
    lines.extend(["", "## Forbidden-access categories", ""])
    if forbidden_by_category:
        for category, rows in sorted(forbidden_by_category.items(), key=lambda item: (-len(item[1]), item[0])):
            lines.append(f"- `{category}`: `{len(rows)}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Blocker examples", ""])
    for reason, rows in sorted(blocker_examples_by_reason.items(), key=lambda item: (-len(item[1]), item[0])):
        lines.append(f"- `{reason}`")
        for row in rows[:5]:
            lines.append(
                f"  - `{row.get('task_id')}` | success=`{row.get('final_submission_success')}` | clean=`{row.get('clean_local_only')}` | evidence=`{row.get('evidence_complete')}`"
            )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "task_id",
                "official_final_submission_reason",
                "final_submission_success",
                "clean_local_only",
                "evidence_complete",
                "counted_success_ready",
                "forbidden_access_detected",
                "forbidden_access_categories",
                "run_root",
            ],
        )
        writer.writeheader()
        for row in blocker_rows:
            writer.writerow(
                {
                    "task_id": row.get("task_id"),
                    "official_final_submission_reason": row.get("official_final_submission_reason"),
                    "final_submission_success": row.get("final_submission_success"),
                    "clean_local_only": row.get("clean_local_only"),
                    "evidence_complete": row.get("evidence_complete"),
                    "counted_success_ready": row.get("counted_success_ready"),
                    "forbidden_access_detected": row.get("forbidden_access_detected"),
                    "forbidden_access_categories": ",".join(row.get("forbidden_access_categories") or []),
                    "run_root": row.get("run_root"),
                }
            )

    print(
        json.dumps(
            {
                "output_json": str(json_path.resolve()),
                "output_md": str(md_path.resolve()),
                "output_csv": str(csv_path.resolve()),
                "submitted_task_count": submitted,
                "final_submission_success_task_count": final_success,
                "additional_successes_needed_if_denominator_static": needed_for_target,
                "forbidden_access_task_count": len(forbidden_rows),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
