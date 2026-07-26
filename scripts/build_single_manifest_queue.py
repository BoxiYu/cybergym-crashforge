from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def load_manifest_entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def task_slug(task_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(task_id)).strip("_")


def build_item_name(index: int, width: int, entry: dict[str, Any]) -> str:
    slug = task_slug(entry.get("task_id") or entry.get("source_run_id") or f"item_{index}")
    return f"{index:0{width}d}_{slug}"


def ensure_unique_name(name: str, used_names: set[str]) -> str:
    if name not in used_names:
        used_names.add(name)
        return name
    suffix = 2
    while True:
        candidate = f"{name}__{suffix}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        suffix += 1


def write_single_manifest_queue(
    *,
    manifest_path: Path,
    output_manifest_dir: Path,
    output_queue_file: Path,
    output_root: str,
    dedupe_task_id: bool,
) -> dict[str, Any]:
    entries = load_manifest_entries(manifest_path)
    if dedupe_task_id:
        deduped: list[dict[str, Any]] = []
        seen_task_ids: set[str] = set()
        for entry in entries:
            task_id = str(entry.get("task_id") or "")
            if task_id and task_id in seen_task_ids:
                continue
            if task_id:
                seen_task_ids.add(task_id)
            deduped.append(entry)
        entries = deduped

    output_manifest_dir.mkdir(parents=True, exist_ok=True)
    output_queue_file.parent.mkdir(parents=True, exist_ok=True)

    width = max(2, len(str(len(entries))))
    queue_items: list[dict[str, str]] = []
    used_names: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        requested_name = str(entry.get("queue_name") or build_item_name(index, width, entry))
        name = ensure_unique_name(requested_name, used_names)
        manifest_file = output_manifest_dir / f"{name}.jsonl"
        manifest_file.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
        queue_items.append(
            {
                "manifest": str(manifest_file),
                "output_root": output_root,
                "name": name,
            }
        )

    payload = {"items": queue_items}
    output_queue_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "input_entries": len(load_manifest_entries(manifest_path)),
        "output_entries": len(entries),
        "output_manifest_dir": str(output_manifest_dir),
        "output_queue_file": str(output_queue_file),
        "output_root": output_root,
        "dedupe_task_id": dedupe_task_id,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split a rescue manifest JSONL into single-entry manifests plus a queue file.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-manifest-dir", type=Path, required=True)
    parser.add_argument("--output-queue-file", type=Path, required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--dedupe-task-id", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = write_single_manifest_queue(
        manifest_path=args.manifest,
        output_manifest_dir=args.output_manifest_dir,
        output_queue_file=args.output_queue_file,
        output_root=args.output_root,
        dedupe_task_id=args.dedupe_task_id,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
