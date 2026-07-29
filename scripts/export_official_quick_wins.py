#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def queue_row(
    *,
    queue: str,
    task_id: str | None,
    run_root: str | None,
    next_action: str,
    notes: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "queue": queue,
        "task_id": task_id,
        "run_root": run_root,
        "next_action": next_action,
        "notes": notes or [],
    }
    row.update(extra)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export operator-facing quick-win queues for official Level1 remediation."
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
        "--post-template-mismatches",
        type=Path,
        default=Path("reports/official_level1_post_template_mismatches_2026-07-29.json"),
    )
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=Path("reports/official_level1_quick_wins_2026-07-29"),
    )
    args = parser.parse_args()

    details = read_json(args.post_template_details)
    incomplete = read_json(args.post_template_incomplete)
    mismatches = read_json(args.post_template_mismatches)

    detail_rows = list(details.get("rows") or [])
    incomplete_rows = list(incomplete.get("rows") or [])
    mismatch_rows = list(mismatches.get("rows") or [])

    watchdog_rows = [
        queue_row(
            queue="watchdog_format_loss",
            task_id=row.get("task_id"),
            run_root=row.get("run_root"),
            next_action="Rerun under the current template/watchdog rules and verify the final answer starts with the two official lines.",
            notes=[
                f"executor_status={row.get('executor_status')}",
                f"submission_reason={row.get('official_final_submission_reason')}",
            ],
        )
        for row in detail_rows
        if row.get("post_template_blocker") == "candidate_not_first_line"
        and row.get("executor_status") == "watchdog_aborted"
    ]

    likely_missed_rows = [
        queue_row(
            queue="likely_missed_candidate",
            task_id=row.get("task_id"),
            run_root=row.get("run_root"),
            next_action="Treat this as an operator hint that the prior run likely stopped short; rerun from clean Level1 inputs and try to regenerate a defensible candidate before any intentional no-submission.",
            notes=[
                f"candidate_artifact_count={row.get('candidate_artifact_count')}",
                "operator_note=do_not_read_other_run_candidate_files_in_official_lane",
            ],
            candidate_artifacts=list(row.get("candidate_artifacts") or []),
        )
        for row in incomplete_rows
        if row.get("likely_missed_candidate_no_submission")
    ]

    crash_evidence_gap_rows = [
        queue_row(
            queue="crash_evidence_gap",
            task_id=row.get("task_id"),
            run_root=row.get("run_root"),
            next_action="Rerun and require the final local-verification line to include concrete vulnerable-side crash evidence, not only a generic repro claim.",
            notes=[
                f"executor_status={row.get('executor_status')}",
                f"submission_reason={row.get('official_final_submission_reason')}",
            ],
        )
        for row in detail_rows
        if row.get("post_template_blocker") == "local_verification_crash_evidence_missing"
    ]

    rebuilt_mismatch_rows = [
        queue_row(
            queue="rebuilt_vs_server_mismatch",
            task_id=row.get("task_id"),
            run_root=row.get("run_root"),
            next_action="Prioritize reruns that validate on the task's bundled validator/fuzzer instead of only a rebuilt local binary.",
            notes=[
                f"candidate_path={row.get('candidate_path')}",
                f"local_verification_references_build_dir={row.get('local_verification_references_build_dir')}",
                f"submit_stdout_looks_like_server_fuzzer={row.get('submit_stdout_looks_like_server_fuzzer')}",
            ],
            candidate_path=row.get("candidate_path"),
            likely_bundled_validation_targets=list(row.get("likely_bundled_validation_targets") or []),
        )
        for row in mismatch_rows
        if row.get("local_verification_references_build_dir")
        and row.get("submit_stdout_looks_like_server_fuzzer")
    ]

    bundled_target_mismatch_rows = [
        queue_row(
            queue="bundled_target_mismatch",
            task_id=row.get("task_id"),
            run_root=row.get("run_root"),
            next_action="Treat as harder solver-quality mismatches; keep for deeper triage after the formatting and likely-missed-candidate pools.",
            notes=[
                f"candidate_path={row.get('candidate_path')}",
                f"local_verification_references_likely_target={row.get('local_verification_references_likely_target')}",
            ],
            candidate_path=row.get("candidate_path"),
            likely_bundled_validation_targets=list(row.get("likely_bundled_validation_targets") or []),
        )
        for row in mismatch_rows
        if row.get("local_verification_references_likely_target")
    ]

    queues = {
        "likely_missed_candidate": likely_missed_rows,
        "crash_evidence_gap": crash_evidence_gap_rows,
        "watchdog_format_loss": watchdog_rows,
        "rebuilt_vs_server_mismatch": rebuilt_mismatch_rows,
        "bundled_target_mismatch": bundled_target_mismatch_rows,
    }

    actionable_task_ids = sorted(
        {
            str(row.get("task_id"))
            for rows in queues.values()
            for row in rows
            if row.get("task_id")
        }
    )

    payload = {
        "generated_at": now_utc(),
        "post_template_details_path": str(args.post_template_details.resolve()),
        "post_template_incomplete_path": str(args.post_template_incomplete.resolve()),
        "post_template_mismatches_path": str(args.post_template_mismatches.resolve()),
        "queue_counts": {name: len(rows) for name, rows in queues.items()},
        "actionable_post_template_task_count_upper_bound": len(actionable_task_ids),
        "actionable_post_template_task_ids": actionable_task_ids,
        "queues": queues,
    }

    json_path = args.output_prefix.with_suffix(".json")
    md_path = args.output_prefix.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# Official Level1 Quick Wins",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Actionable post-template task upper bound: `{payload['actionable_post_template_task_count_upper_bound']}`",
        "",
        "These queues are operator-facing remediation pools under the official-only lane.",
        "",
    ]
    for queue_name in (
        "likely_missed_candidate",
        "crash_evidence_gap",
        "watchdog_format_loss",
        "rebuilt_vs_server_mismatch",
        "bundled_target_mismatch",
    ):
        rows = queues[queue_name]
        lines.append(f"## {queue_name}")
        lines.append("")
        lines.append(f"- Task count: `{len(rows)}`")
        if not rows:
            lines.append("- none")
            lines.append("")
            continue
        for row in rows:
            lines.append(
                f"- `{row['task_id']}` | run=`{row['run_root']}` | next=`{row['next_action']}`"
            )
            if row.get("notes"):
                lines.append("  - " + " | ".join(row["notes"]))
            if row.get("candidate_artifacts"):
                lines.append(
                    "  - candidate_artifacts: "
                    + ", ".join(f"`{item}`" for item in row["candidate_artifacts"][:8])
                )
            if row.get("likely_bundled_validation_targets"):
                lines.append(
                    "  - likely_targets: "
                    + ", ".join(f"`{item}`" for item in row["likely_bundled_validation_targets"][:8])
                )
        lines.append("")
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "output_json": str(json_path.resolve()),
                "output_md": str(md_path.resolve()),
                "queue_counts": payload["queue_counts"],
                "actionable_post_template_task_count_upper_bound": payload[
                    "actionable_post_template_task_count_upper_bound"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
