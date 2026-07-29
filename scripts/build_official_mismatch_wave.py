#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_active_wave_tasks(state_paths: list[Path]) -> set[str]:
    active: set[str] = set()
    for path in state_paths:
        if not path.exists():
            continue
        try:
            payload = read_json(path)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if bool(payload.get("queue_complete")):
            continue
        for key in ("processed_task_ids", "pending_task_ids_preview", "deferred_task_ids"):
            for task_id in payload.get(key, []) or []:
                if isinstance(task_id, str) and task_id:
                    active.add(task_id)
    return active


def collect_active_state_paths(
    reports_root: Path,
    explicit_paths: list[Path],
    auto_exclude_official_wave_state: bool,
) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            return
        seen.add(resolved)
        ordered.append(resolved)

    for path in explicit_paths:
        add(path)

    if auto_exclude_official_wave_state:
        patterns = (
            "official_level1*/combined_queue.binary.state.json",
            "official_level1_fast_wave_*/combined_queue.binary.state.json",
        )
        for pattern in patterns:
            for path in sorted(reports_root.glob(pattern)):
                add(path)

    return ordered


def row_priority(row: dict[str, Any]) -> tuple[float, str]:
    likely_targets = int(row.get("likely_bundled_validation_target_count") or 0)
    build_dir = 1 if bool(row.get("local_verification_references_build_dir")) else 0
    likely_target_used = 1 if bool(row.get("local_verification_references_likely_target")) else 0
    server_fuzzer = 1 if bool(row.get("submit_stdout_looks_like_server_fuzzer")) else 0
    score = (
        (likely_targets * 100.0)
        + (build_dir * 10.0)
        + (server_fuzzer * 5.0)
        - (likely_target_used * 25.0)
    )
    return (-score, str(row.get("task_id") or ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an official mismatch-focused rerun wave.")
    parser.add_argument(
        "--mismatch-json",
        type=Path,
        default=ROOT / "reports" / "official_level1_post_template_mismatches_2026-07-29.json",
    )
    parser.add_argument("--reports-root", type=Path, default=ROOT / "reports")
    parser.add_argument("--exclude-wave-state", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument(
        "--auto-exclude-official-wave-state",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically exclude tasks that are still pending or active in official wave state files under reports/.",
    )
    args = parser.parse_args()

    mismatch = read_json(args.mismatch_json.resolve())
    rows = list(mismatch.get("rows") or [])
    reports_root = args.reports_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    active_state_paths = collect_active_state_paths(
        reports_root,
        [path.resolve() for path in args.exclude_wave_state],
        args.auto_exclude_official_wave_state,
    )
    active_tasks = load_active_wave_tasks(active_state_paths)
    explicit_task_ids = {value for value in args.task_id if value}

    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        if active_tasks and task_id in active_tasks:
            continue
        if explicit_task_ids and task_id not in explicit_task_ids:
            continue
        candidates.append(row)

    candidates.sort(key=row_priority)
    selected_rows = candidates[: max(args.count, 0)]
    selected_tasks = [str(row["task_id"]) for row in selected_rows]
    (output_dir / "tasks.md").write_text("\n".join(selected_tasks) + ("\n" if selected_tasks else ""), encoding="utf-8")
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "selection_policy": "post-template mismatch rerun queue ranked by likely bundled-target recovery value",
        "selected_count": len(selected_tasks),
        "selected_tasks": selected_tasks,
        "selection_filters": {
            "mismatch_json": str(args.mismatch_json.resolve()),
            "explicit_task_ids": sorted(explicit_task_ids),
            "excluded_active_wave_state_files": [str(path) for path in active_state_paths],
            "auto_exclude_official_wave_state": args.auto_exclude_official_wave_state,
        },
        "excluded_active_task_count": len(active_tasks),
        "selected_rows": selected_rows,
    }
    (output_dir / "task_selection.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"selected_count": len(selected_tasks), "tasks": selected_tasks}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
