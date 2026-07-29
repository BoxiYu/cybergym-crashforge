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


def read_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return {}
    return read_json(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export a compact control panel for official Level1 monitoring."
    )
    parser.add_argument(
        "--scoreboard",
        type=Path,
        default=Path("reports/official_level1_scoreboard_2026-07-28.json"),
    )
    parser.add_argument(
        "--template-rollout",
        type=Path,
        default=Path("reports/official_level1_template_rollout_2026-07-29.json"),
    )
    parser.add_argument(
        "--post-template-watch",
        type=Path,
        default=Path("reports/official_level1_post_template_watch_2026-07-29.json"),
    )
    parser.add_argument(
        "--post-template-mismatches",
        type=Path,
        default=Path("reports/official_level1_post_template_mismatches_2026-07-29.json"),
    )
    parser.add_argument(
        "--post-template-details",
        type=Path,
        default=Path("reports/official_level1_post_template_details_2026-07-29.json"),
    )
    parser.add_argument(
        "--post-template-incomplete",
        type=Path,
        default=Path("reports/official_level1_post_template_incomplete_2026-07-29.json"),
    )
    parser.add_argument(
        "--quick-wins",
        type=Path,
        default=Path("reports/official_level1_quick_wins_2026-07-29.json"),
    )
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=Path("reports/official_level1_control_panel_2026-07-29"),
    )
    parser.add_argument("--target-rate", type=float, default=0.85)
    args = parser.parse_args()

    scoreboard = read_json(args.scoreboard)
    rollout = read_json(args.template_rollout)
    post_watch = read_json(args.post_template_watch)
    mismatches = read_json(args.post_template_mismatches)
    post_details = read_json(args.post_template_details)
    post_incomplete = read_json(args.post_template_incomplete)
    quick_wins = read_json_if_exists(args.quick_wins)

    summary = dict(scoreboard.get("summary") or {})
    success = int(summary.get("final_submission_success_task_count") or 0)
    submitted = int(summary.get("exactly_one_submit_task_count") or 0)
    counted_ready = int(summary.get("counted_success_ready_task_count") or 0)
    current_rate = (success / submitted) if submitted else 0.0
    needed_static = max(0, ceil(args.target_rate * submitted) - success) if submitted else 0

    payload = {
        "generated_at": now_utc(),
        "target_rate": args.target_rate,
        "scoreboard_path": str(args.scoreboard.resolve()),
        "template_rollout_path": str(args.template_rollout.resolve()),
        "post_template_watch_path": str(args.post_template_watch.resolve()),
        "post_template_mismatches_path": str(args.post_template_mismatches.resolve()),
        "post_template_details_path": str(args.post_template_details.resolve()),
        "post_template_incomplete_path": str(args.post_template_incomplete.resolve()),
        "quick_wins_path": str(args.quick_wins.resolve()),
        "official_overall": {
            "final_submission_success_task_count": success,
            "exactly_one_submit_task_count": submitted,
            "counted_success_ready_task_count": counted_ready,
            "current_rate": current_rate,
            "additional_successes_needed_if_denominator_static": needed_static,
        },
        "historical_debt": {
            "historical_pre_template_task_count": int(summary.get("historical_pre_template_task_count") or 0),
            "pre_template_first_line_blocker_task_count": int(summary.get("pre_template_first_line_blocker_task_count") or 0),
            "pre_template_missing_local_verification_task_count": int(summary.get("pre_template_missing_local_verification_task_count") or 0),
            "tasks_with_forbidden_access_detected": int(summary.get("tasks_with_forbidden_access_detected") or 0),
            "tasks_with_network_command_detected": int(summary.get("tasks_with_network_command_detected") or 0),
        },
        "post_template": {
            "template_present_task_count": int(rollout.get("coverage", {}).get("template_present", 0)),
            "template_missing_task_count": int(rollout.get("coverage", {}).get("template_missing", 0)),
            "first_line_blockers_with_template": int(rollout.get("first_line_blockers_by_template_presence", {}).get("template_present", 0)),
            "missing_local_verification_with_template": int(rollout.get("submitted_missing_local_verification_by_template_presence", {}).get("template_present", 0)),
            "watch_status": str(post_watch.get("post_template_watch", {}).get("status") or "unknown"),
            "gold_path_success_task_count": int(post_watch.get("post_template_watch", {}).get("gold_path_success_task_count", 0)),
            "baseline_compliant_unsuccessful_task_count": int(post_watch.get("post_template_watch", {}).get("baseline_compliant_unsuccessful_task_count", 0)),
            "baseline_task_ids": list(post_watch.get("post_template_watch", {}).get("baseline_task_ids") or []),
            "local_vs_server_mismatch_task_count": int(mismatches.get("mismatch_task_count") or 0),
            "local_vs_server_mismatch_patterns": dict(mismatches.get("pattern_counts") or {}),
            "failure_composition": dict(post_details.get("blocker_counts") or {}),
            "incomplete_subcauses": dict(post_incomplete.get("subcause_counts") or {}),
            "incomplete_patterns": dict(post_incomplete.get("pattern_counts") or {}),
            "quick_win_queue_counts": dict(quick_wins.get("queue_counts") or {}),
            "quick_win_actionable_task_upper_bound": int(
                quick_wins.get("actionable_post_template_task_count_upper_bound") or 0
            ),
        },
    }

    json_path = args.output_prefix.with_suffix(".json")
    md_path = args.output_prefix.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# Official Level1 Control Panel",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Official final-submission success: `{success}` / `{submitted}` (`{current_rate:.2%}`)",
        f"- Additional successes needed at static denominator for `{args.target_rate:.0%}`: `{needed_static}`",
        f"- Counted-success-ready tasks: `{counted_ready}`",
        "",
        "## Historical debt",
        "",
        f"- Historical pre-template tasks: `{payload['historical_debt']['historical_pre_template_task_count']}`",
        f"- Pre-template first-line blockers: `{payload['historical_debt']['pre_template_first_line_blocker_task_count']}`",
        f"- Pre-template missing local verification lines: `{payload['historical_debt']['pre_template_missing_local_verification_task_count']}`",
        f"- Forbidden-access tasks: `{payload['historical_debt']['tasks_with_forbidden_access_detected']}`",
        f"- Network-flagged tasks: `{payload['historical_debt']['tasks_with_network_command_detected']}`",
        "",
        "## Post-template watch",
        "",
        f"- Watch status: `{payload['post_template']['watch_status']}`",
        f"- Template-present tasks: `{payload['post_template']['template_present_task_count']}`",
        f"- Template-present first-line blockers: `{payload['post_template']['first_line_blockers_with_template']}`",
        f"- Template-present missing local verification lines: `{payload['post_template']['missing_local_verification_with_template']}`",
        f"- Post-template gold-path successes: `{payload['post_template']['gold_path_success_task_count']}`",
        f"- Post-template compliant-but-unsuccessful baselines: `{payload['post_template']['baseline_compliant_unsuccessful_task_count']}`",
        f"- Post-template local-vs-server mismatches: `{payload['post_template']['local_vs_server_mismatch_task_count']}`",
        f"- Post-template quick-win task upper bound: `{payload['post_template']['quick_win_actionable_task_upper_bound']}`",
    ]
    failure_composition = payload["post_template"]["failure_composition"]
    if failure_composition:
        lines.extend(["", "## Post-template failure composition", ""])
        for blocker, count in sorted(failure_composition.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{blocker}`: `{count}`")
    incomplete_subcauses = payload["post_template"]["incomplete_subcauses"]
    if incomplete_subcauses:
        lines.extend(["", "## Post-template incomplete subcauses", ""])
        for blocker, count in sorted(incomplete_subcauses.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{blocker}`: `{count}`")
    incomplete_patterns = payload["post_template"]["incomplete_patterns"]
    if incomplete_patterns:
        lines.extend(["", "## Post-template incomplete patterns", ""])
        for key, count in sorted(incomplete_patterns.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{key}`: `{count}`")
    mismatch_patterns = payload["post_template"]["local_vs_server_mismatch_patterns"]
    if mismatch_patterns:
        lines.extend(["", "## Post-template mismatch patterns", ""])
        for key, count in sorted(mismatch_patterns.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{key}`: `{count}`")
    quick_win_counts = payload["post_template"]["quick_win_queue_counts"]
    if quick_win_counts:
        lines.extend(["", "## Post-template quick-win queues", ""])
        for key, count in sorted(quick_win_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{key}`: `{count}`")
    baseline_ids = payload["post_template"]["baseline_task_ids"]
    if baseline_ids:
        lines.extend(["", "## Baseline task ids", ""])
        for task_id in baseline_ids:
            lines.append(f"- `{task_id}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "output_json": str(json_path.resolve()),
                "output_md": str(md_path.resolve()),
                "current_rate": current_rate,
                "needed_static": needed_static,
                "watch_status": payload["post_template"]["watch_status"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
