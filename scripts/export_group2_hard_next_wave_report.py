from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

STATUS_PRIORITY = (
    "fix_also_crashes",
    "no_submission",
    "invalid_result",
    "codex_failed",
    "no_vul_crash",
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def priority_ordered_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority_index = {status: index for index, status in enumerate(STATUS_PRIORITY)}
    return sorted(
        rows,
        key=lambda row: (
            priority_index.get(str(row.get("prior_status") or ""), len(priority_index)),
            str(row.get("task_id") or ""),
            str(row.get("queue_name") or ""),
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a human-readable next-wave report for the group2 hard campaign.")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--pending-followup", type=Path, required=True)
    parser.add_argument("--post-followup-summary", type=Path)
    parser.add_argument("--consistency-check", type=Path)
    parser.add_argument("--output-md", type=Path, required=True)
    return parser


def render_report(
    snapshot: dict[str, Any],
    pending: dict[str, Any],
    post_followup_summary: dict[str, Any] | None,
    consistency_check: dict[str, Any] | None,
) -> str:
    halt = snapshot.get("followup_usage_limit_halt") or {}
    monitor = snapshot.get("monitor") or {}
    followup_launcher = snapshot.get("followup_launcher") or {}
    post_followup_launcher = snapshot.get("post_followup_launcher") or {}
    automation = snapshot.get("automation") or {}
    latest_counts = snapshot.get("hard_latest_status_counts") or {}
    pending_rows = pending.get("pending_tasks") or []
    post_followup_rows = (post_followup_summary or {}).get("tasks") or []
    post_followup_counts = (post_followup_summary or {}).get("prior_status_counts") or {}
    hot_followup_rows = priority_ordered_rows(list(pending_rows))[:5]
    hot_post_followup_rows = list(post_followup_rows)[:7]

    lines: list[str] = []
    lines.append("# Group2 Hard Next-Wave Status")
    lines.append("")
    lines.append(f"Generated at: `{snapshot.get('generated_at')}`")
    lines.append("")
    lines.append("## Current Score")
    lines.append("")
    lines.append(f"- Total hard tasks: `{snapshot.get('hard_total')}`")
    lines.append(f"- Attempted: `{snapshot.get('hard_attempted')}`")
    lines.append(f"- Latest success: `{snapshot.get('hard_latest_success')}`")
    lines.append(f"- Latest status counts: `{json.dumps(latest_counts, sort_keys=True)}`")
    lines.append("")
    lines.append("## Followup Queue")
    lines.append("")
    lines.append(f"- Processed followup items: `{(snapshot.get('followup_queue') or {}).get('processed_count')}`")
    lines.append(f"- Remaining followup items: `{(snapshot.get('followup_queue') or {}).get('queue_remaining_count')}`")
    lines.append(f"- Next followup task: `{(snapshot.get('followup_queue') or {}).get('next_queue_task_id')}`")
    lines.append("")
    lines.append("## Usage Limit Gate")
    lines.append("")
    lines.append(f"- Pending items blocked by usage limit: `{halt.get('pending_items')}`")
    lines.append(f"- Reset time: `{halt.get('reset_at')}`")
    lines.append("")
    lines.append("## Automation")
    lines.append("")
    lines.append(f"- Monitor running: `{bool(monitor.get('running'))}`")
    lines.append(f"- Monitor pid: `{monitor.get('pid')}`")
    lines.append(f"- Followup launcher running: `{bool(followup_launcher.get('running'))}`")
    lines.append(f"- Post-followup launcher running: `{bool(post_followup_launcher.get('running'))}`")
    lines.append(f"- Post-followup processed: `{snapshot.get('post_followup_processed')}`")
    lines.append(f"- Post-followup remaining: `{snapshot.get('post_followup_remaining')}`")
    lines.append(f"- Followup usage-limit blocked: `{bool(automation.get('followup_usage_limit_blocked'))}`")
    lines.append(f"- Followup ready to resume: `{bool(automation.get('followup_ready_to_resume'))}`")
    lines.append(f"- Post-followup waiting on followup completion: `{bool(automation.get('post_followup_waiting_on_followup_completion'))}`")
    lines.append(f"- Post-followup ready to start: `{bool(automation.get('post_followup_ready_to_start'))}`")
    lines.append(f"- Next auto action: `{automation.get('next_auto_action')}`")
    lines.append("")
    if consistency_check is not None:
        lines.append("## Consistency Check")
        lines.append("")
        lines.append(f"- Consistency ok: `{bool(consistency_check.get('ok'))}`")
        lines.append(f"- Followup remaining verified: `{consistency_check.get('followup_remaining')}`")
        lines.append(f"- Post-followup remaining verified: `{consistency_check.get('post_followup_remaining')}`")
        lines.append(f"- Errors: `{json.dumps(consistency_check.get('errors', []), sort_keys=True)}`")
        lines.append("")
    lines.append("## Resume Commands")
    lines.append("")
    lines.append("```bash")
    lines.append("bash scripts/control_group2_hard_monitor.sh status")
    lines.append("bash scripts/control_group2_hard_monitor.sh resume")
    lines.append("")
    lines.append("bash scripts/control_group2_hard_followup.sh refresh")
    lines.append("bash scripts/control_group2_hard_followup.sh status")
    lines.append("bash scripts/control_group2_hard_followup.sh resume")
    lines.append("```")
    lines.append("")
    if hot_followup_rows:
        lines.append("## High-Signal Followup After Reset")
        lines.append("")
        lines.append(
            "- Prioritized for quickest signal: "
            "`fix_also_crashes` first, then `no_submission`, then `no_vul_crash`."
        )
        lines.append("")
        lines.append("| task_id | prior_status | attempt | current_queue_name |")
        lines.append("|---|---|---:|---|")
        for row in hot_followup_rows:
            lines.append(
                f"| `{row.get('task_id')}` | `{row.get('prior_status')}` | `{row.get('attempt')}` | `{row.get('queue_name')}` |"
            )
        lines.append("")
    lines.append("## Remaining Followup Tasks")
    lines.append("")
    lines.append("| queue_name | task_id | prior_status | attempt |")
    lines.append("|---|---|---|---:|")
    for row in pending_rows:
        lines.append(
            f"| `{row.get('queue_name')}` | `{row.get('task_id')}` | `{row.get('prior_status')}` | `{row.get('attempt')}` |"
        )
    lines.append("")
    if post_followup_summary is not None:
        lines.append("## Post-Followup Queue Prepared")
        lines.append("")
        lines.append(f"- Ready third-pass tasks: `{post_followup_summary.get('task_count')}`")
        lines.append(f"- Prior-status mix: `{json.dumps(post_followup_counts, sort_keys=True)}`")
        lines.append(f"- Queue file: `{post_followup_summary.get('queue_file')}`")
        lines.append("")
        if hot_post_followup_rows:
            lines.append("### First Third-Pass Batch")
            lines.append("")
            lines.append("- Queue already reordered for higher signal before broader `no_vul_crash` retries.")
            lines.append("")
            lines.append("| queue_name | task_id | prior_status | attempt |")
            lines.append("|---|---|---|---:|")
            for row in hot_post_followup_rows:
                lines.append(
                    f"| `{row.get('queue_name')}` | `{row.get('task_id')}` | `{row.get('prior_status')}` | `{row.get('attempt')}` |"
                )
            lines.append("")
        lines.append("### Full Third-Pass Queue")
        lines.append("")
        lines.append("| queue_name | task_id | prior_status | attempt |")
        lines.append("|---|---|---|---:|")
        for row in post_followup_rows:
            lines.append(
                f"| `{row.get('queue_name')}` | `{row.get('task_id')}` | `{row.get('prior_status')}` | `{row.get('attempt')}` |"
            )
        lines.append("")
        lines.append("## Post-Followup Launch")
        lines.append("")
        lines.append("```bash")
        lines.append("bash scripts/control_group2_hard_post_followup.sh status")
        lines.append("bash scripts/control_group2_hard_post_followup.sh resume")
        lines.append("```")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = build_parser().parse_args()
    snapshot = read_json(args.snapshot.resolve())
    pending = read_json(args.pending_followup.resolve())
    post_followup_summary = read_json(args.post_followup_summary.resolve()) if args.post_followup_summary else None
    consistency_check = read_json(args.consistency_check.resolve()) if args.consistency_check else None
    report = render_report(snapshot, pending, post_followup_summary, consistency_check)
    write_text(args.output_md.resolve(), report)
    print(str(args.output_md.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
