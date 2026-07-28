from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from build_static_retry_queue import write_static_retry_queue
from export_static_retry_queue_summary import build_payload as build_queue_summary

GROUP2_HARD_POST_FOLLOWUP_STATUS_PRIORITY = (
    "fix_also_crashes",
    "no_submission",
    "invalid_result",
    "codex_failed",
    "no_vul_crash",
)


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["task_id\tlatest_status\tlatest_success\tever_success"]
    for row in rows:
        lines.append(
            "\t".join(
                [
                    str(row.get("task_id") or ""),
                    str(row.get("latest_status") or ""),
                    str(int(bool(row.get("latest_success")))),
                    str(int(bool(row.get("ever_success")))),
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export group2 hard tasks that remain unsolved after the current followup queue, and build a ready next-pass retry queue."
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--snapshot-label", default=None)
    parser.add_argument("--pending-followup", type=Path, required=True)
    parser.add_argument("--trajectory-index", type=Path, required=True)
    parser.add_argument("--results-root", type=Path, default=Path("codex_rescue_runs_local"))
    parser.add_argument("--live-summary", type=Path, required=True)
    parser.add_argument("--output-exhausted-json", type=Path, required=True)
    parser.add_argument("--output-exhausted-tsv", type=Path, required=True)
    parser.add_argument("--output-manifest-dir", type=Path, required=True)
    parser.add_argument("--output-queue-file", type=Path, required=True)
    parser.add_argument("--output-summary-json", type=Path, required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--difficulty", default="level1")
    parser.add_argument("--campaign", default="group2_hard_post_followup_static_retry")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-timeout-seconds", type=int, default=5400)
    parser.add_argument("--api-key-env", default="CYBERGYM_API_KEY")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    snapshot = read_json(args.snapshot.resolve())
    pending = read_json(args.pending_followup.resolve())
    pending_ids = {
        str(row.get("task_id"))
        for row in (pending.get("pending_tasks") or [])
        if isinstance(row, dict) and row.get("task_id")
    }

    exhausted_rows = [
        row
        for row in (snapshot.get("tasks") or [])
        if isinstance(row, dict)
        and row.get("latest_status") != "success"
        and str(row.get("task_id") or "") not in pending_ids
    ]
    exhausted_rows.sort(key=lambda row: str(row.get("task_id") or ""))

    exhausted_payload = {
        "snapshot": args.snapshot_label or str(args.snapshot.resolve()),
        "pending_followup": str(args.pending_followup.resolve()),
        "generated_at": now_utc(),
        "exhausted_count": len(exhausted_rows),
        "tasks": exhausted_rows,
    }
    write_json(args.output_exhausted_json.resolve(), exhausted_payload)
    write_tsv(args.output_exhausted_tsv.resolve(), exhausted_rows)

    queue_summary = write_static_retry_queue(
        trajectory_index=args.trajectory_index.resolve(),
        task_source=args.output_exhausted_json.resolve(),
        results_root=args.results_root.resolve(),
        output_manifest_dir=args.output_manifest_dir.resolve(),
        output_queue_file=args.output_queue_file.resolve(),
        output_root=args.output_root,
        server=args.server,
        data_dir=args.data_dir,
        difficulty=args.difficulty,
        campaign=args.campaign,
        retryable_statuses={"no_vul_crash", "fix_also_crashes", "no_submission", "invalid_result", "codex_failed"},
        status_priority=GROUP2_HARD_POST_FOLLOWUP_STATUS_PRIORITY,
        codex_bin=args.codex_bin,
        codex_timeout_seconds=args.codex_timeout_seconds,
        api_key_env=args.api_key_env,
    )
    retry_summary = build_queue_summary(
        queue_file=args.output_queue_file.resolve(),
        live_summary_file=args.live_summary.resolve(),
    )
    write_json(args.output_summary_json.resolve(), retry_summary)

    print(
        json.dumps(
            {
                "exhausted_count": len(exhausted_rows),
                "retry_queue_task_count": retry_summary.get("task_count"),
                "output_exhausted_json": str(args.output_exhausted_json.resolve()),
                "output_queue_file": str(args.output_queue_file.resolve()),
                "output_summary_json": str(args.output_summary_json.resolve()),
                "queue_build_summary": queue_summary,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
