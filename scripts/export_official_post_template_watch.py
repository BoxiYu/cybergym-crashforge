#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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
        description="Export a compact watch view for the official Level1 post-template bucket."
    )
    parser.add_argument(
        "--scoreboard",
        type=Path,
        default=Path("reports/official_level1_scoreboard_2026-07-28.json"),
    )
    parser.add_argument(
        "--post-template",
        type=Path,
        default=Path("reports/official_level1_post_template_bucket_2026-07-29.json"),
    )
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=Path("reports/official_level1_post_template_watch_2026-07-29"),
    )
    parser.add_argument("--target-rate", type=float, default=0.85)
    args = parser.parse_args()

    scoreboard = read_json(args.scoreboard)
    post_template = read_json(args.post_template)
    summary = dict(scoreboard.get("summary") or {})
    submitted = int(summary.get("exactly_one_submit_task_count") or 0)
    success = int(summary.get("final_submission_success_task_count") or 0)
    needed_static = max(0, ceil(args.target_rate * submitted) - success) if submitted else 0

    rows = list(post_template.get("rows") or [])
    baseline_rows = [row for row in rows if row.get("monitoring_status") == "baseline_compliant_unsuccessful"]
    gold_rows = [row for row in rows if row.get("post_template_classification") == "gold_path_success"]
    incomplete_rows = [row for row in rows if row.get("post_template_classification") == "template_present_incomplete"]

    if gold_rows:
        watch_status = "post_template_gold_path_present"
    elif baseline_rows:
        watch_status = "awaiting_first_post_template_gold_path_success"
    elif incomplete_rows:
        watch_status = "template_present_runs_need_format_or_evidence_repair"
    else:
        watch_status = "no_post_template_runs_yet"

    payload = {
        "generated_at": now_utc(),
        "scoreboard_path": str(args.scoreboard.resolve()),
        "post_template_path": str(args.post_template.resolve()),
        "target_rate": args.target_rate,
        "official_overall": {
            "final_submission_success_task_count": success,
            "exactly_one_submit_task_count": submitted,
            "current_rate": (success / submitted) if submitted else 0.0,
            "additional_successes_needed_if_denominator_static": needed_static,
        },
        "post_template_watch": {
            "status": watch_status,
            "post_template_task_count": len(rows),
            "gold_path_success_task_count": len(gold_rows),
            "baseline_compliant_unsuccessful_task_count": len(baseline_rows),
            "template_present_incomplete_task_count": len(incomplete_rows),
            "baseline_task_ids": [str(row.get("task_id")) for row in baseline_rows],
            "gold_path_task_ids": [str(row.get("task_id")) for row in gold_rows],
        },
    }

    json_path = args.output_prefix.with_suffix(".json")
    md_path = args.output_prefix.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# Official Level1 Post-Template Watch",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Overall official rate: `{payload['official_overall']['current_rate']:.2%}`",
        f"- Additional successes needed at static denominator for `{args.target_rate:.0%}`: `{needed_static}`",
        f"- Watch status: `{payload['post_template_watch']['status']}`",
        f"- Post-template tasks: `{payload['post_template_watch']['post_template_task_count']}`",
        f"- Post-template gold-path successes: `{payload['post_template_watch']['gold_path_success_task_count']}`",
        f"- Post-template compliant-but-unsuccessful baselines: `{payload['post_template_watch']['baseline_compliant_unsuccessful_task_count']}`",
        f"- Post-template incomplete tasks: `{payload['post_template_watch']['template_present_incomplete_task_count']}`",
        "",
    ]
    if baseline_rows:
        lines.append("## Current baseline task ids")
        lines.append("")
        for task_id in payload["post_template_watch"]["baseline_task_ids"]:
            lines.append(f"- `{task_id}`")
    if gold_rows:
        lines.extend(["", "## Current gold-path task ids", ""])
        for task_id in payload["post_template_watch"]["gold_path_task_ids"]:
            lines.append(f"- `{task_id}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "output_json": str(json_path.resolve()),
                "output_md": str(md_path.resolve()),
                "watch_status": watch_status,
                "post_template_task_count": len(rows),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
