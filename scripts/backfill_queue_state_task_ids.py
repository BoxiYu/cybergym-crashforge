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


def load_queue_items(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path)
    if isinstance(payload, dict):
        items = payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def build_processed_task_ids(queue_items: list[dict[str, Any]], processed_names: list[str]) -> list[str]:
    processed_name_set = {str(item) for item in processed_names if item}
    processed_task_ids: list[str] = []
    seen: set[str] = set()
    for item in queue_items:
        queue_name = str(item.get("name") or "")
        task_id = str(item.get("task_id") or "")
        if not queue_name or queue_name not in processed_name_set or not task_id or task_id in seen:
            continue
        seen.add(task_id)
        processed_task_ids.append(task_id)
    return processed_task_ids


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill processed_task_ids into a rescue queue state file using the current queue mapping.")
    parser.add_argument("--queue-file", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args()

    if not args.queue_file.exists():
        print(
            json.dumps(
                {
                    "state_updated": False,
                    "reason": "missing_queue",
                    "queue_file": str(args.queue_file.resolve()),
                    "state_file": str(args.state_file.resolve()),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    queue_items = load_queue_items(args.queue_file.resolve())
    if args.state_file.exists():
        state_payload = read_json(args.state_file.resolve())
    else:
        state_payload = {
            "processed_names": [],
            "processed_task_ids": [],
            "queue_complete": False,
            "updated_at": None,
        }
    processed_names = list(state_payload.get("processed_names") or [])
    processed_task_ids = build_processed_task_ids(queue_items, processed_names)
    existing_task_ids = [str(item) for item in (state_payload.get("processed_task_ids") or []) if item]

    updated = sorted(set(existing_task_ids) | set(processed_task_ids))
    changed = (
        updated != sorted(set(existing_task_ids))
        or not args.state_file.exists()
    )
    state_payload["processed_task_ids"] = updated
    state_payload.setdefault("processed_names", processed_names)
    state_payload.setdefault("queue_complete", False)
    if changed:
        if state_payload.get("updated_at") is None:
            state_payload["updated_at"] = None
        write_json(args.state_file.resolve(), state_payload)

    print(
        json.dumps(
            {
                "state_updated": changed,
                "processed_names": len(processed_names),
                "processed_task_ids": len(updated),
                "queue_file": str(args.queue_file.resolve()),
                "state_file": str(args.state_file.resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
