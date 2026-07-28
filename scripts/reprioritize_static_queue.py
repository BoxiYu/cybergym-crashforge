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


def load_processed_state(path: Path) -> tuple[set[str], set[str]]:
    if not path.exists():
        return set(), set()
    payload = read_json(path)
    if not isinstance(payload, dict):
        return set(), set()
    processed_names = {str(item) for item in (payload.get("processed_names") or []) if item}
    processed_task_ids = {str(item) for item in (payload.get("processed_task_ids") or []) if item}
    return processed_names, processed_task_ids


def load_task_statuses(path: Path) -> dict[str, str]:
    hard_tasks = ((read_json(path).get("hard") or {}).get("tasks") or {})
    statuses: dict[str, str] = {}
    for task_id, payload in hard_tasks.items():
        if not isinstance(payload, dict):
            continue
        status = payload.get("latest_status")
        if status:
            statuses[str(task_id)] = str(status)
    return statuses


def reprioritize_queue_items(
    queue_items: list[dict[str, Any]],
    *,
    processed_names: set[str],
    processed_task_ids: set[str],
    task_statuses: dict[str, str],
    status_priority: tuple[str, ...],
) -> list[dict[str, Any]]:
    priority_index = {status: index for index, status in enumerate(status_priority)}
    indexed_items = list(enumerate(queue_items))
    processed: list[tuple[int, dict[str, Any]]] = []
    pending: list[tuple[int, dict[str, Any]]] = []

    for original_index, item in indexed_items:
        name = str(item.get("name") or "")
        task_id = str(item.get("task_id") or "")
        if name in processed_names or task_id in processed_task_ids:
            processed.append((original_index, item))
        else:
            pending.append((original_index, item))

    pending.sort(
        key=lambda entry: (
            priority_index.get(task_statuses.get(str(entry[1].get("task_id") or ""), ""), len(priority_index)),
            entry[0],
        )
    )
    return [item for _, item in processed] + [item for _, item in pending]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reprioritize pending items in a static retry queue without disturbing processed entries.")
    parser.add_argument("--queue-file", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--live-summary", type=Path, required=True)
    parser.add_argument(
        "--status-priority",
        action="append",
        default=[],
        help="Repeatable or comma-separated. Earlier statuses are placed first among pending items.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    priority_parts: list[str] = []
    for raw in args.status_priority:
        priority_parts.extend(part.strip() for part in str(raw).split(","))
    status_priority = tuple(part for part in priority_parts if part)
    if not status_priority:
        raise SystemExit("missing --status-priority")

    queue_items = load_queue_items(args.queue_file.resolve())
    processed_names, processed_task_ids = load_processed_state(args.state_file.resolve())
    task_statuses = load_task_statuses(args.live_summary.resolve())
    reordered_items = reprioritize_queue_items(
        queue_items,
        processed_names=processed_names,
        processed_task_ids=processed_task_ids,
        task_statuses=task_statuses,
        status_priority=status_priority,
    )
    write_json(args.queue_file.resolve(), {"items": reordered_items})
    print(
        json.dumps(
            {
                "queue_file": str(args.queue_file.resolve()),
                "item_count": len(reordered_items),
                "processed_names": len(processed_names),
                "processed_task_ids": len(processed_task_ids),
                "status_priority": list(status_priority),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
