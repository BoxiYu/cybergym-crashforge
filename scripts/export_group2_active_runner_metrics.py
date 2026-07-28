from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import export_group2_intervention_watch as watch


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def build_runner_entry(
    *,
    active_runner: dict[str, Any],
    run_root: Path | None,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    metrics = {
        "completed_items": 0,
        "submit_started": 0,
        "submit_completed": 0,
    }
    events_mtime = None
    events_size = None
    last_submit_output_excerpt = ""
    last_event_excerpt = ""

    if run_root is not None:
        events_path = run_root / "codex_events.jsonl"
        if events_path.exists():
            stat = events_path.stat()
            events_mtime = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
            events_size = stat.st_size
            metrics, last_submit_completed = watch.collect_event_metrics(run_root)
            raw_lines = events_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for raw_line in reversed(raw_lines):
                if raw_line.strip():
                    last_event_excerpt = raw_line[:400]
                    break
            if last_submit_completed is not None:
                last_submit_output_excerpt = str(
                    (last_submit_completed.get("item") or {}).get("aggregated_output") or ""
                )[:400]

    verdict_counts: dict[str, int] = {}
    for record in records:
        verdict = str(record.get("verdict") or "unknown")
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    submit_record_gap = max(0, metrics["submit_completed"] - len(records))

    return {
        **active_runner,
        "run_root": str(run_root) if run_root is not None else None,
        "events_mtime": events_mtime,
        "events_size": events_size,
        "event_metrics": metrics,
        "submit_record_gap": submit_record_gap,
        "db_task_summary": {
            "total_records": len(records),
            "total_no_vul_records": verdict_counts.get("no_vul_crash", 0),
            "total_verified_success_records": verdict_counts.get("verified_success", 0),
            "total_non_differential_records": verdict_counts.get("non_differential", 0),
            "total_other_records": sum(
                count
                for verdict, count in verdict_counts.items()
                if verdict not in {"no_vul_crash", "verified_success", "non_differential"}
            ),
        },
        "last_submit_output_excerpt": last_submit_output_excerpt,
        "last_event_excerpt": last_event_excerpt,
    }


def build_payload(*, wave_dir: Path, runs_dir: Path, pocdb_path: Path) -> dict[str, Any]:
    active_runners = watch.collect_active_runners(wave_dir)
    run_roots = watch.collect_run_roots(runs_dir)
    task_records = watch.load_task_records([item["task_id"] for item in active_runners], pocdb_path)
    return {
        "generated_at": now_utc(),
        "active_runners": [
            build_runner_entry(
                active_runner=item,
                run_root=run_roots.get(item["task_id"]),
                records=task_records.get(item["task_id"], []),
            )
            for item in active_runners
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export detailed active runner metrics for the group2 hard wave.")
    parser.add_argument("--wave-dir", type=Path, required=True)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--pocdb-path", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = build_payload(
        wave_dir=args.wave_dir.resolve(),
        runs_dir=args.runs_dir.resolve(),
        pocdb_path=args.pocdb_path.resolve(),
    )
    write_json(args.output_json.resolve(), payload)
    print(
        json.dumps(
            {
                "active_runner_count": len(payload["active_runners"]),
                "output_json": str(args.output_json.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
