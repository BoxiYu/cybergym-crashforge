from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import build_single_manifest_queue as buildq
import refresh_missing_image_runnable_queue as refresh


DEFAULT_ALLOWED_FAILURE_CATEGORIES = ("no_submission", "step_limit")
FAILURE_CATEGORY_PRIORITY = {
    "no_submission": 0,
    "step_limit": 1,
    "fix_also_crashes": 2,
    "codex_failed": 3,
    "no_vul_crash": 4,
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def append_log(log_path: Path, message: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_utc().isoformat()} {message}\n")


def easy_entry_sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    category = str(entry.get("failure_category") or entry.get("prior_status") or "")
    task_id = str(entry.get("task_id") or "")
    return (FAILURE_CATEGORY_PRIORITY.get(category, 999), task_id)


def select_easy_first_entries(
    *,
    source_manifest: Path,
    results_root: Path,
    skip_cumulative_no_vul_threshold: int,
    allowed_failure_categories: set[str],
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    entries = buildq.load_manifest_entries(source_manifest)
    filtered_entries, filter_counts = refresh.filter_runnable_entries(
        entries,
        results_root=results_root,
        skip_cumulative_no_vul_threshold=skip_cumulative_no_vul_threshold,
    )

    selected: list[dict[str, Any]] = []
    selected_counts = {category: 0 for category in sorted(allowed_failure_categories)}
    seen_task_ids: set[str] = set()
    task_history_index = refresh.build_task_result_history_index(Path(results_root))
    for entry in filtered_entries:
        task_id = str(entry.get("task_id") or "")
        if not task_id or task_id in seen_task_ids:
            continue
        category = str(entry.get("failure_category") or entry.get("prior_status") or "")
        latest_status = str((task_history_index.get(task_id) or {}).get("latest_status") or "")
        effective_category = latest_status or category
        if latest_status and latest_status not in allowed_failure_categories:
            continue
        if effective_category not in allowed_failure_categories:
            continue
        selected_entry = dict(entry)
        selected_entry["queue_name"] = refresh.stable_queue_name(selected_entry)
        if latest_status:
            selected_entry["effective_failure_category"] = latest_status
        selected.append(selected_entry)
        selected_counts[effective_category] = selected_counts.get(effective_category, 0) + 1
        seen_task_ids.add(task_id)

    selected.sort(key=easy_entry_sort_key)
    return selected, filter_counts, selected_counts


def selected_counts_text(selected_counts: dict[str, int]) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(selected_counts.items()))


def run_cycle(args: argparse.Namespace, log_path: Path) -> dict[str, Any]:
    allowed_failure_categories = set(args.allowed_failure_category)
    selected_entries, filter_counts, selected_counts = select_easy_first_entries(
        source_manifest=args.source_manifest,
        results_root=Path(args.results_root),
        skip_cumulative_no_vul_threshold=args.skip_cumulative_no_vul_threshold,
        allowed_failure_categories=allowed_failure_categories,
    )
    refresh.write_manifest_entries(args.selected_manifest, selected_entries)
    append_log(
        log_path,
        (
            f"selected_manifest_refreshed source_entries={len(buildq.load_manifest_entries(args.source_manifest))} "
            f"selected_entries={len(selected_entries)} selected_counts={selected_counts_text(selected_counts)} "
            f"filtered_already_success={filter_counts['already_success']} "
            f"filtered_exhausted_no_vul={filter_counts['exhausted_no_vul']} "
            f"filtered_active_elsewhere={filter_counts['active_elsewhere']}"
        ),
    )

    refresh_args = argparse.Namespace(**vars(args))
    refresh_args.manifest = args.selected_manifest
    summary = refresh.run_partition_cycle(refresh_args, log_path)
    summary["selected_entries"] = len(selected_entries)
    summary["selected_counts"] = selected_counts
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuously refresh an easy-first missing-image queue from retryable manifests."
    )
    parser.add_argument(
        "--source-manifest",
        type=Path,
        default=Path("/tmp/codex_rescue_partition_july25/missing_image_retryable.jsonl"),
    )
    parser.add_argument(
        "--selected-manifest",
        type=Path,
        default=Path("/tmp/codex_rescue_partition_july25/easy_first_missing_image_retryable.jsonl"),
    )
    parser.add_argument(
        "--allowed-failure-category",
        action="append",
        default=list(DEFAULT_ALLOWED_FAILURE_CATEGORIES),
    )
    parser.add_argument("--partition-output-dir", type=Path, required=True)
    parser.add_argument("--output-manifest-dir", type=Path, required=True)
    parser.add_argument("--output-queue-file", type=Path, required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--results-root", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--pocdb-path", required=True)
    parser.add_argument("--scheduler-log", default="codex_rescue_runs_local/easy_first_queue.log")
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--max-active-runners", type=int, default=8)
    parser.add_argument("--log-path", default="codex_rescue_runs_local/easy_first_refresh.log")
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--dedupe-task-id", action="store_true")
    parser.add_argument(
        "--skip-cumulative-no-vul-threshold",
        type=int,
        default=refresh.launcherq.DEFAULT_SKIP_CUMULATIVE_NO_VUL_THRESHOLD,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_path = Path(args.log_path)
    append_log(
        log_path,
        (
            f"refresh_easy_first_missing_image_queue started source_manifest={args.source_manifest} "
            f"selected_manifest={args.selected_manifest} "
            f"allowed_failure_categories={','.join(args.allowed_failure_category)}"
        ),
    )
    while True:
        run_cycle(args, log_path)
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
