from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["queue_name\ttask_id\tprior_status\tattempt\tsource_run_id\tsource_run_root"]
    for row in rows:
        lines.append(
            "\t".join(
                [
                    str(row.get("queue_name") or ""),
                    str(row.get("task_id") or ""),
                    str(row.get("prior_status") or ""),
                    str(row.get("attempt") or ""),
                    str(row.get("source_run_id") or ""),
                    str(row.get("source_run_root") or ""),
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export exact pending followup tasks for the group2 hard wave.")
    parser.add_argument("--queue-file", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--summary-file", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-tsv", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    queue_payload = read_json(args.queue_file.resolve())
    state_payload = read_json(args.state_file.resolve())
    summary_payload = read_json(args.summary_file.resolve())

    items = queue_payload.get("items") or []
    processed_names = set(str(item) for item in (state_payload.get("processed_names") or []) if item)
    processed_task_ids = set(str(item) for item in (state_payload.get("processed_task_ids") or []) if item)
    for item in items:
        if not isinstance(item, dict):
            continue
        queue_name = str(item.get("name") or "")
        task_id = str(item.get("task_id") or "")
        if queue_name and queue_name in processed_names and task_id:
            processed_task_ids.add(task_id)
    summary_rows = summary_payload.get("tasks") or []
    summary_by_name = {str(row.get("queue_name")): row for row in summary_rows if row.get("queue_name")}

    pending_rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        queue_name = str(item.get("name") or "")
        task_id = str(item.get("task_id") or "")
        if not queue_name or queue_name in processed_names or (task_id and task_id in processed_task_ids):
            continue
        summary_row = summary_by_name.get(queue_name, {})
        pending_rows.append(
            {
                "queue_name": queue_name,
                "task_id": item.get("task_id") or summary_row.get("task_id"),
                "prior_status": summary_row.get("prior_status"),
                "attempt": summary_row.get("attempt"),
                "source_run_id": summary_row.get("source_run_id"),
                "source_run_root": summary_row.get("source_run_root"),
            }
        )

    payload = {
        "queue_file": str(args.queue_file.resolve()),
        "state_file": str(args.state_file.resolve()),
        "summary_file": str(args.summary_file.resolve()),
        "pending_count": len(pending_rows),
        "pending_tasks": pending_rows,
    }
    write_json(args.output_json.resolve(), payload)
    write_tsv(args.output_tsv.resolve(), pending_rows)
    print(
        json.dumps(
            {
                "pending_count": len(pending_rows),
                "output_json": str(args.output_json.resolve()),
                "output_tsv": str(args.output_tsv.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
