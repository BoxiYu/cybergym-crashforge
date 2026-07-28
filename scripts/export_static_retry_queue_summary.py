from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_queue_items(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path)
    if isinstance(payload, dict):
        items = payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def build_payload(*, queue_file: Path, live_summary_file: Path) -> dict[str, Any]:
    queue_items = load_queue_items(queue_file)
    hard_tasks = ((read_json(live_summary_file).get("hard") or {}).get("tasks") or {})

    rows = []
    status_counts = Counter()
    for item in queue_items:
        manifest_path = Path(str(item["manifest"]))
        manifest_entry = read_json(manifest_path)
        task_id = str(item["task_id"])
        prior_status = (hard_tasks.get(task_id) or {}).get("latest_status")
        status_counts[str(prior_status)] += 1
        rows.append(
            {
                "task_id": task_id,
                "queue_name": item.get("name"),
                "prior_status": prior_status,
                "attempt": manifest_entry.get("attempt"),
                "source_run_id": manifest_entry.get("source_run_id"),
                "source_run_root": manifest_entry.get("source_run_root"),
            }
        )

    return {
        "generated_at": now_utc(),
        "queue_file": str(queue_file),
        "task_count": len(queue_items),
        "prior_status_counts": dict(status_counts),
        "tasks": rows,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a summary for a static retry queue.")
    parser.add_argument("--queue-file", type=Path, required=True)
    parser.add_argument("--live-summary", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = build_payload(queue_file=args.queue_file.resolve(), live_summary_file=args.live_summary.resolve())
    write_json(args.output_json.resolve(), payload)
    print(
        json.dumps(
            {
                "task_count": payload["task_count"],
                "prior_status_counts": payload["prior_status_counts"],
                "output_json": str(args.output_json.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
