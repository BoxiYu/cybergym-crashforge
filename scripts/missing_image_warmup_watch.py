from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import build_single_manifest_queue as buildq
import codex_rescue_runner as rescue
import refresh_missing_image_runnable_queue as refresh


PRIOR_STATUS_PRIORITY = {
    "no_submission": 0,
    "step_limit": 1,
    "fix_also_crashes": 2,
    "codex_failed": 3,
    "no_vul_crash": 4,
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def append_log(log_path: Path, payload: dict[str, object]):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_utc().isoformat()} {json.dumps(payload, ensure_ascii=False)}\n")


def warmup_task_sort_key(row: dict[str, Any]) -> tuple[int, int, int, str]:
    task_id = str(row.get("task_id") or "")
    family, _, _subid = task_id.partition(":")
    family_priority = 0 if family == "arvo" else 1
    prior_status = str(row.get("prior_status") or row.get("failure_category") or "")
    status_priority = PRIOR_STATUS_PRIORITY.get(prior_status, 9)
    missing_count = int(row.get("missing_count") or 0)
    return (status_priority, missing_count, family_priority, task_id)


def collect_hot_task_ids(results_root: Path, max_candidates: int) -> set[str]:
    if max_candidates <= 0:
        return set()
    rows = rescue.select_auto_submit_backfill_runs(results_root=results_root, max_candidates=max_candidates)
    task_ids: set[str] = set()
    for _run_root, result in rows:
        task_id = result.get("task_id")
        if task_id:
            task_ids.add(str(task_id))
    return task_ids


def load_excluded_task_ids(paths: Path | list[Path] | tuple[Path, ...] | None) -> set[str]:
    if paths is None:
        return set()
    if isinstance(paths, Path):
        candidate_paths = [paths]
    else:
        candidate_paths = [path for path in paths if path is not None]
    task_ids: set[str] = set()
    for path in candidate_paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            task_id = payload.get("task_id")
            if task_id:
                task_ids.add(str(task_id))
    return task_ids


def select_warmup_tasks(
    *,
    manifest_path: Path,
    results_root: Path,
    max_tasks: int,
    exclude_hot_candidates: int,
    skip_cumulative_no_vul_threshold: int,
    exclude_task_jsonl: Path | list[Path] | tuple[Path, ...] | None = None,
    selection_offset: int = 0,
    selection_stride: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    entries = buildq.load_manifest_entries(manifest_path)
    filtered_entries, counts = refresh.filter_runnable_entries(
        entries,
        results_root=results_root,
        skip_cumulative_no_vul_threshold=skip_cumulative_no_vul_threshold,
    )
    hot_task_ids = collect_hot_task_ids(results_root, exclude_hot_candidates)
    explicitly_excluded_task_ids = load_excluded_task_ids(exclude_task_jsonl)
    local_images = rescue.list_local_docker_images()
    candidates: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    for entry in filtered_entries:
        task_id = str(entry.get("task_id") or "")
        if (
            not task_id
            or task_id in seen_task_ids
            or task_id in hot_task_ids
            or task_id in explicitly_excluded_task_ids
        ):
            continue
        probe = rescue.inspect_runtime_assets(task_id, local_images=local_images)
        missing_images = probe.get("missing_images") or []
        issues = probe.get("issues") or []
        if not missing_images or issues:
            continue
        seen_task_ids.add(task_id)
        candidates.append(
            {
                "task_id": task_id,
                "prior_status": entry.get("prior_status"),
                "failure_category": entry.get("failure_category"),
                "missing_count": len(missing_images),
                "missing_images": list(missing_images),
            }
        )
    candidates.sort(key=warmup_task_sort_key)
    if selection_stride > 1:
        normalized_offset = max(0, selection_offset)
        candidates = candidates[normalized_offset::selection_stride]
    return candidates[:max_tasks], counts


def write_missing_jsonl(path: Path, blocked_tasks: list[dict[str, object]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"task_id": row["task_id"]}, ensure_ascii=False) for row in blocked_tasks]
    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    path.write_text(payload, encoding="utf-8")


def choose_pull_max_workers(output_dir: Path, configured_max_workers: int, stale_progress_seconds: int) -> int:
    if configured_max_workers <= 1 or stale_progress_seconds <= 0:
        return configured_max_workers
    summary_path = output_dir / "summary.json"
    if not summary_path.exists():
        return configured_max_workers
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return configured_max_workers
    if summary.get("status") != "running":
        return configured_max_workers
    last_progress_time = summary.get("last_progress_time")
    if not isinstance(last_progress_time, (int, float)):
        return configured_max_workers
    if time.time() - float(last_progress_time) < stale_progress_seconds:
        return configured_max_workers
    return configured_max_workers


def run_warmup_pull(args: argparse.Namespace, blocked_tasks: list[dict[str, object]]) -> dict[str, object]:
    write_missing_jsonl(args.missing_jsonl, blocked_tasks)
    effective_max_workers = choose_pull_max_workers(args.output_dir, args.max_workers, args.stale_progress_seconds)
    command = [
        args.python_bin,
        "scripts/server_data/download_missing_images.py",
        "--missing-jsonl",
        str(args.missing_jsonl),
        "--output-dir",
        str(args.output_dir),
        "--execute",
        "--max-workers",
        str(effective_max_workers),
        "--pull-timeout-seconds",
        str(args.pull_timeout_seconds),
        "--stale-progress-seconds",
        str(args.stale_progress_seconds),
    ]
    if args.tasks_file:
        command.extend(["--tasks-file", str(args.tasks_file)])
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    stdout_lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    stderr_lines = [line for line in (result.stderr or "").splitlines() if line.strip()]
    summary_payload: dict[str, object] | None = None
    summary_path = args.output_dir / "summary.json"
    if summary_path.exists():
        try:
            parsed = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                summary_payload = parsed
        except json.JSONDecodeError:
            summary_payload = None
    return {
        "event": "warmup_poll",
        "returncode": result.returncode,
        "effective_max_workers": effective_max_workers,
        "blocked_task_count": len(blocked_tasks),
        "blocked_task_ids": [row["task_id"] for row in blocked_tasks],
        "summary": summary_payload,
        "stdout_tail": stdout_lines[-5:],
        "stderr_tail": stderr_lines[-5:],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pre-warm a small window of missing-image rescue tasks so wave9 has ready work sooner."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/tmp/codex_rescue_partition_july25/missing_image_retryable.jsonl"),
    )
    parser.add_argument("--results-root", type=Path, default=Path("codex_rescue_runs_local"))
    parser.add_argument(
        "--missing-jsonl",
        type=Path,
        default=Path("/tmp/codex_rescue_partition_july25/missing_image_warmup.jsonl"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/tmp/codex_rescue_partition_july25/pull_missing_warmup_exec"),
    )
    parser.add_argument("--tasks-file", type=Path, default=Path("cybergym_data/tasks.json"))
    parser.add_argument("--max-tasks", type=int, default=16)
    parser.add_argument("--exclude-hot-candidates", type=int, default=5)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--pull-timeout-seconds", type=int, default=900)
    parser.add_argument("--stale-progress-seconds", type=int, default=420)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument(
        "--skip-cumulative-no-vul-threshold",
        type=int,
        default=9,
    )
    parser.add_argument("--exclude-task-jsonl", type=Path, action="append", default=None)
    parser.add_argument("--selection-offset", type=int, default=0)
    parser.add_argument("--selection-stride", type=int, default=1)
    parser.add_argument("--log-path", type=Path, default=Path("codex_rescue_runs_local/missing_image_warmup_watch.log"))
    parser.add_argument("--python-bin", default=sys.executable)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    append_log(
        args.log_path,
        {
            "event": "watch_started",
            "manifest": str(args.manifest),
            "results_root": str(args.results_root),
            "missing_jsonl": str(args.missing_jsonl),
            "output_dir": str(args.output_dir),
            "max_tasks": args.max_tasks,
            "exclude_hot_candidates": args.exclude_hot_candidates,
            "max_workers": args.max_workers,
            "pull_timeout_seconds": args.pull_timeout_seconds,
            "stale_progress_seconds": args.stale_progress_seconds,
            "poll_seconds": args.poll_seconds,
        },
    )
    while True:
        blocked_tasks, filter_counts = select_warmup_tasks(
            manifest_path=args.manifest,
            results_root=args.results_root,
            max_tasks=args.max_tasks,
            exclude_hot_candidates=args.exclude_hot_candidates,
            skip_cumulative_no_vul_threshold=args.skip_cumulative_no_vul_threshold,
            exclude_task_jsonl=args.exclude_task_jsonl,
            selection_offset=args.selection_offset,
            selection_stride=args.selection_stride,
        )
        if blocked_tasks:
            payload = run_warmup_pull(args, blocked_tasks)
            payload["filter_counts"] = filter_counts
            append_log(args.log_path, payload)
        else:
            append_log(
                args.log_path,
                {
                    "event": "warmup_poll",
                    "blocked_task_count": 0,
                    "blocked_task_ids": [],
                    "summary": None,
                    "stdout_tail": [],
                    "stderr_tail": [],
                    "filter_counts": filter_counts,
                },
            )
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
