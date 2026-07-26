from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import build_single_manifest_queue as buildq
import rescue_queue_launcher as launcherq

RUN_ROOT_TASK_PATTERN = re.compile(r"(?P<family>arvo|oss-fuzz)_(?P<subid>[^-]+)-codex-rescue-attempt")
FAILURE_CATEGORY_PRIORITY = {
    "no_submission": 0,
    "step_limit": 1,
    "fix_also_crashes": 2,
    "codex_failed": 3,
    "task_assets_failed": 4,
    "task_generation_failed": 5,
    "no_vul_crash": 6,
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def append_log(log_path: Path, message: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_utc().isoformat()} {message}\n")


def get_process_rows() -> list[tuple[int, str]]:
    result = subprocess.run(["ps", "-eo", "pid=,args="], check=True, capture_output=True, text=True)
    rows: list[tuple[int, str]] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            pid_text, args = line.split(None, 1)
            rows.append((int(pid_text), args))
        except ValueError:
            continue
    return rows


def has_active_queue_launcher(queue_file: Path, process_rows: list[tuple[int, str]] | None = None) -> bool:
    rows = process_rows if process_rows is not None else get_process_rows()
    queue_text = str(queue_file)
    for _pid, args in rows:
        if "scripts/rescue_queue_launcher.py" not in args:
            continue
        if "queue_autostart_watch.py" in args:
            continue
        if f"--queue-file {queue_text}" in args:
            return True
    return False


def write_empty_queue(queue_file: Path):
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    queue_file.write_text(json.dumps({"items": []}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def queue_item_count(queue_file: Path) -> int:
    if not queue_file.exists():
        return 0
    payload = json.loads(queue_file.read_text(encoding="utf-8"))
    items = payload["items"] if isinstance(payload, dict) else payload
    return len(items)


def stable_queue_name(entry: dict[str, Any]) -> str:
    existing = entry.get("queue_name")
    if existing:
        return str(existing)
    source = entry.get("task_id") or entry.get("source_run_id") or entry.get("id") or "item"
    slug = buildq.task_slug(str(source))
    return slug or "item"


def runnable_entry_sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    category = str(entry.get("failure_category") or entry.get("prior_status") or "")
    task_id = str(entry.get("task_id") or "")
    return (FAILURE_CATEGORY_PRIORITY.get(category, 999), task_id)


def write_manifest_entries(path: Path, entries: list[dict[str, Any]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries)
    path.write_text(content, encoding="utf-8")


def build_task_result_history_index(results_root: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not results_root.exists():
        return index
    for result_path in results_root.rglob("result.json"):
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        task_id = payload.get("task_id")
        if not task_id:
            continue
        record = index.setdefault(
            str(task_id),
            {
                "task_id": str(task_id),
                "has_success": False,
                "latest_status": None,
                "cumulative_no_vul_records": 0,
                "_latest_order": None,
            },
        )
        record["cumulative_no_vul_records"] += sum(
            1 for item in (payload.get("records") or []) if item.get("verdict") == "no_vul_crash"
        )
        if payload.get("status") == "success" or any(
            item.get("verdict") == "verified_success" for item in (payload.get("records") or [])
        ):
            record["has_success"] = True
        order_key = launcherq.result_order_key(payload, result_path)
        if record["_latest_order"] is None or order_key > record["_latest_order"]:
            record["_latest_order"] = order_key
            record["latest_status"] = payload.get("status")
    for record in index.values():
        record.pop("_latest_order", None)
    return index


def task_id_from_run_root(run_root: Path) -> str | None:
    match = RUN_ROOT_TASK_PATTERN.search(run_root.name)
    if match is None:
        return None
    return f"{match.group('family')}:{match.group('subid')}"


def collect_active_task_ids(results_root: Path, ps_output: str | None = None) -> set[str]:
    ps_text = ps_output if ps_output is not None else launcherq.get_ps_output()
    active_task_ids: set[str] = set()
    for item in launcherq.active_runner_parent_items(ps_text):
        try:
            task_id = launcherq.extract_manifest_task_id(Path(item.manifest))
        except Exception:
            task_id = None
        if task_id:
            active_task_ids.add(str(task_id))
    for run_root in launcherq.collect_active_runner_parent_roots(ps_text):
        task_id = task_id_from_run_root(run_root)
        if task_id:
            active_task_ids.add(task_id)
    return active_task_ids


def filter_runnable_entries(
    entries: list[dict[str, Any]],
    *,
    results_root: Path,
    skip_cumulative_no_vul_threshold: int,
    skip_latest_no_vul: bool,
    ps_output: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    ps_text = ps_output if ps_output is not None else launcherq.get_ps_output()
    results_root = Path(results_root)
    task_history_index = build_task_result_history_index(results_root)
    active_task_ids = collect_active_task_ids(results_root, ps_output=ps_text)
    counts = {
        "input_entries": len(entries),
        "already_success": 0,
        "exhausted_no_vul": 0,
        "latest_no_vul": 0,
        "active_elsewhere": 0,
        "kept": 0,
    }
    filtered: list[dict[str, Any]] = []
    for entry in entries:
        task_id = str(entry.get("task_id") or "")
        if task_id:
            task_history_summary = task_history_index.get(
                task_id,
                {
                    "task_id": task_id,
                    "has_success": False,
                    "latest_status": None,
                    "cumulative_no_vul_records": 0,
                },
            )
            if task_history_summary.get("has_success"):
                counts["already_success"] += 1
                continue
            if (
                int(task_history_summary.get("cumulative_no_vul_records") or 0) >= skip_cumulative_no_vul_threshold
                and task_history_summary.get("latest_status") != "success"
            ):
                counts["exhausted_no_vul"] += 1
                continue
            if skip_latest_no_vul and task_history_summary.get("latest_status") == "no_vul_crash":
                counts["latest_no_vul"] += 1
                continue
            if task_id in active_task_ids:
                counts["active_elsewhere"] += 1
                continue
        filtered_entry = dict(entry)
        filtered_entry["queue_name"] = stable_queue_name(filtered_entry)
        filtered.append(filtered_entry)
    filtered.sort(key=runnable_entry_sort_key)
    counts["kept"] = len(filtered)
    return filtered, counts


def build_partition_command(args: argparse.Namespace) -> list[str]:
    return [
        args.python_bin,
        "scripts/codex_rescue_runner.py",
        "partition",
        "--manifest",
        str(args.manifest),
        "--output-dir",
        str(args.partition_output_dir),
        "--server",
        args.server,
        "--data-dir",
        args.data_dir,
        "--pocdb-path",
        args.pocdb_path,
        "--exclude-success-root",
        args.results_root,
    ]


def build_launcher_command(args: argparse.Namespace) -> list[str]:
    return [
        args.python_bin,
        "scripts/rescue_queue_launcher.py",
        "--queue-file",
        str(args.output_queue_file),
        "--results-root",
        args.results_root,
        "--server",
        args.server,
        "--data-dir",
        args.data_dir,
        "--pocdb-path",
        args.pocdb_path,
        "--max-active-runners",
        str(args.max_active_runners),
        "--scheduler-log",
        args.scheduler_log,
    ]


def run_partition_cycle(args: argparse.Namespace, log_path: Path) -> dict[str, Any]:
    partition_cmd = build_partition_command(args)
    result = subprocess.run(partition_cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        append_log(
            log_path,
            f"partition_failed rc={result.returncode} stderr_tail={(result.stderr or '').splitlines()[-5:]}",
        )
        return {
            "partition_rc": result.returncode,
            "queue_items": 0,
            "runnable_entries": 0,
            "launcher_active": has_active_queue_launcher(args.output_queue_file),
        }

    runnable_manifest = args.partition_output_dir / "runnable.jsonl"
    runnable_entries = 0
    filter_summary = {
        "input_entries": 0,
        "already_success": 0,
        "exhausted_no_vul": 0,
        "latest_no_vul": 0,
        "active_elsewhere": 0,
        "kept": 0,
    }
    if runnable_manifest.exists():
        runnable_entries_payload = buildq.load_manifest_entries(runnable_manifest)
        runnable_entries = len(runnable_entries_payload)
        filtered_entries, filter_summary = filter_runnable_entries(
            runnable_entries_payload,
            results_root=Path(args.results_root),
            skip_cumulative_no_vul_threshold=args.skip_cumulative_no_vul_threshold,
            skip_latest_no_vul=args.skip_latest_no_vul,
        )
        filtered_manifest = args.partition_output_dir / "runnable_filtered.jsonl"
        write_manifest_entries(filtered_manifest, filtered_entries)
        buildq.write_single_manifest_queue(
            manifest_path=filtered_manifest,
            output_manifest_dir=args.output_manifest_dir,
            output_queue_file=args.output_queue_file,
            output_root=args.output_root,
            dedupe_task_id=args.dedupe_task_id,
        )
    else:
        write_empty_queue(args.output_queue_file)

    process_rows = get_process_rows()
    launcher_active = has_active_queue_launcher(args.output_queue_file, process_rows=process_rows)
    queue_items = queue_item_count(args.output_queue_file)
    if queue_items > 0 and not launcher_active:
        subprocess.Popen(  # noqa: S603
            build_launcher_command(args),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        append_log(log_path, f"launcher_started queue_items={queue_items} queue_file={args.output_queue_file}")
        launcher_active = True

    summary = {
        "partition_rc": result.returncode,
        "runnable_entries": runnable_entries,
        "filtered_entries": filter_summary["kept"],
        "filtered_already_success": filter_summary["already_success"],
        "filtered_exhausted_no_vul": filter_summary["exhausted_no_vul"],
        "filtered_latest_no_vul": filter_summary["latest_no_vul"],
        "filtered_active_elsewhere": filter_summary["active_elsewhere"],
        "queue_items": queue_items,
        "launcher_active": launcher_active,
    }
    append_log(
        log_path,
        (
            f"cycle_complete partition_rc={summary['partition_rc']} runnable_entries={summary['runnable_entries']} "
            f"filtered_entries={summary['filtered_entries']} filtered_already_success={summary['filtered_already_success']} "
            f"filtered_exhausted_no_vul={summary['filtered_exhausted_no_vul']} "
            f"filtered_latest_no_vul={summary['filtered_latest_no_vul']} "
            f"filtered_active_elsewhere={summary['filtered_active_elsewhere']} "
            f"queue_items={summary['queue_items']} launcher_active={summary['launcher_active']}"
        ),
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Periodically repartition retryable missing-image entries and launch runnable tasks as images arrive."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--partition-output-dir", type=Path, required=True)
    parser.add_argument("--output-manifest-dir", type=Path, required=True)
    parser.add_argument("--output-queue-file", type=Path, required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--results-root", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--pocdb-path", required=True)
    parser.add_argument("--scheduler-log", default="codex_rescue_runs_local/missing_image_queue.log")
    parser.add_argument("--poll-seconds", type=int, default=120)
    parser.add_argument("--max-active-runners", type=int, default=16)
    parser.add_argument("--log-path", default="codex_rescue_runs_local/missing_image_refresh.log")
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--dedupe-task-id", action="store_true")
    parser.add_argument(
        "--skip-cumulative-no-vul-threshold",
        type=int,
        default=launcherq.DEFAULT_SKIP_CUMULATIVE_NO_VUL_THRESHOLD,
    )
    parser.add_argument(
        "--skip-latest-no-vul",
        action="store_true",
        help="Exclude tasks whose latest known status is no_vul_crash so fresh coverage prioritizes untouched tasks.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_path = Path(args.log_path)
    append_log(log_path, "refresh_missing_image_runnable_queue started")
    while True:
        run_partition_cycle(args, log_path)
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
