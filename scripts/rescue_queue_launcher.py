from __future__ import annotations

import argparse
import fcntl
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import codex_rescue_runner as rescue

RUNNER_MARKER = "scripts/codex_rescue_runner.py run"
CODEX_TASK_MARKER = "codex exec -C "
RECENT_RUN_ACTIVITY_SECONDS = 120
CODEX_TASK_PATTERN = re.compile(r"codex exec -C (\S+)")
MANIFEST_PATH_PATTERN = re.compile(r"--manifest (\S+)")
DEFAULT_SKIP_CUMULATIVE_NO_VUL_THRESHOLD = 8
DEFAULT_USAGE_LIMIT_SCAN_FILES = 200
USAGE_LIMIT_MESSAGE_FRAGMENT = "usage limit"
USAGE_LIMIT_RESET_PATTERN = re.compile(
    r"try again at ([A-Za-z]{3,9} \d{1,2}(?:st|nd|rd|th), \d{4} \d{1,2}:\d{2} [AP]M)",
    re.IGNORECASE,
)


@dataclass
class QueueItem:
    manifest: str
    output_root: str
    name: str
    ready_json_path: str | None = None
    ready_json_field: str | None = None
    ready_json_value: str | None = None


@dataclass
class QueueLauncherState:
    processed_names: set[str]
    queue_complete: bool = False


@dataclass(frozen=True)
class UsageLimitBlock:
    run_root: Path
    events_path: Path
    message: str
    reset_at: datetime | None = None


def now_utc() -> datetime:
    return datetime.now(UTC)


def load_queue(queue_file: Path) -> list[QueueItem]:
    payload: Any = json.loads(queue_file.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("items", [])
    if not isinstance(payload, list):
        raise ValueError("queue file must be a JSON list or an object with an 'items' list")

    items: list[QueueItem] = []
    for index, raw in enumerate(payload, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"queue item #{index} is not an object")
        manifest = raw.get("manifest")
        output_root = raw.get("output_root")
        if not manifest or not output_root:
            raise ValueError(f"queue item #{index} missing manifest or output_root")
        name = raw.get("name") or Path(manifest).stem or Path(output_root).name or f"queue_item_{index}"
        items.append(
            QueueItem(
                manifest=str(manifest),
                output_root=str(output_root),
                name=str(name),
                ready_json_path=raw.get("ready_json_path"),
                ready_json_field=raw.get("ready_json_field"),
                ready_json_value=str(raw.get("ready_json_value")) if raw.get("ready_json_value") is not None else None,
            )
        )
    return items


def default_state_file_path(queue_file: Path) -> Path:
    return queue_file.with_suffix(queue_file.suffix + ".state.json")


def load_queue_state(state_file: Path) -> QueueLauncherState:
    if not state_file.exists():
        return QueueLauncherState(processed_names=set(), queue_complete=False)
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    processed_raw = payload.get("processed_names") if isinstance(payload, dict) else None
    processed_names = {str(item) for item in (processed_raw or []) if item}
    queue_complete = bool(payload.get("queue_complete")) if isinstance(payload, dict) else False
    return QueueLauncherState(processed_names=processed_names, queue_complete=queue_complete)


def write_queue_state(
    state_file: Path,
    *,
    processed_names: set[str],
    queue_complete: bool,
) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "processed_names": sorted(processed_names),
        "queue_complete": bool(queue_complete),
        "updated_at": now_utc().isoformat(),
    }
    state_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def count_active_runners(ps_output: str) -> int:
    runner_parent_count = sum(1 for line in ps_output.splitlines() if is_active_runner_parent_command(line))
    codex_task_dirs: set[str] = set()
    for line in ps_output.splitlines():
        if CODEX_TASK_MARKER not in line:
            continue
        match = CODEX_TASK_PATTERN.search(line)
        if match is None:
            continue
        task_dir = Path(match.group(1))
        if task_dir.name != "task":
            continue
        result_path = task_dir.parent / "result.json"
        if result_path.exists():
            continue
        codex_task_dirs.add(str(task_dir))
    codex_child_count = len(codex_task_dirs)
    return max(runner_parent_count, codex_child_count)


def is_active_runner_parent_command(line: str) -> bool:
    argv = tokenize_process_command(line)
    if not argv:
        return False
    executable = Path(argv[0]).name
    if "python" not in executable:
        return False
    for index, token in enumerate(argv):
        if token != "scripts/codex_rescue_runner.py":
            continue
        if index + 1 >= len(argv):
            return False
        return argv[index + 1] == "run"
    return False


def tokenize_process_command(line: str) -> list[str]:
    try:
        argv = shlex.split(line)
    except ValueError:
        return []
    while argv and argv[0].isdigit():
        argv = argv[1:]
    return argv


def extract_argv_flag_value(argv: list[str], flag: str) -> str | None:
    for index, token in enumerate(argv[:-1]):
        if token == flag:
            return argv[index + 1]
    return None


def get_ps_output() -> str:
    result = subprocess.run(["ps", "-eo", "args="], check=True, capture_output=True, text=True)
    return result.stdout


def get_active_runner_count(ps_output: str | None = None) -> int:
    return count_active_runners(ps_output if ps_output is not None else get_ps_output())


def load_manifest_entries(manifest_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_single_task_manifest(manifest_path: Path) -> dict[str, Any] | None:
    entries = load_manifest_entries(manifest_path)
    if len(entries) != 1:
        return None
    entry = entries[0]
    if not isinstance(entry, dict):
        return None
    return entry


def extract_manifest_task_id(manifest_path: Path) -> str | None:
    entry = load_single_task_manifest(manifest_path)
    task_id = entry.get("task_id") if entry else None
    return str(task_id) if task_id else None


def parse_result_timestamp(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00").replace(" ", "T")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def result_order_key(payload: dict[str, Any], result_path: Path) -> tuple[float, float, float]:
    started_at = parse_result_timestamp(payload.get("started_at"))
    ended_at = parse_result_timestamp(payload.get("ended_at")) or parse_result_timestamp(payload.get("finished_at"))
    file_mtime = result_path.stat().st_mtime
    primary = started_at or ended_at or file_mtime
    secondary = ended_at or file_mtime
    return (primary, secondary, file_mtime)


def pending_queue_items(
    queue_items: list[QueueItem],
    processed_names: set[str],
    deferred_names: set[str] | None = None,
) -> list[QueueItem]:
    pending = [item for item in queue_items if item.name not in processed_names]
    if not deferred_names:
        return pending
    undeferred = [item for item in pending if item.name not in deferred_names]
    return undeferred or pending


def has_recent_run_activity(candidate: Path, now_ts: float | None = None) -> bool:
    events_path = candidate / "codex_events.jsonl"
    if not events_path.exists():
        return False
    current = time.time() if now_ts is None else now_ts
    return current - events_path.stat().st_mtime <= RECENT_RUN_ACTIVITY_SECONDS


def has_active_runner_for_manifest(item: QueueItem, ps_output: str | None = None) -> bool:
    ps_text = ps_output if ps_output is not None else get_ps_output()
    target_manifest = str(item.manifest)
    for line in ps_text.splitlines():
        if not is_active_runner_parent_command(line):
            continue
        argv = tokenize_process_command(line)
        for index, token in enumerate(argv[:-1]):
            if token == "--manifest" and argv[index + 1] == target_manifest:
                return True
    return False


def find_latest_incomplete_item_run(item: QueueItem) -> Path | None:
    manifest_entry = load_single_task_manifest(Path(item.manifest))
    if manifest_entry is None:
        return None
    task_id = manifest_entry.get("task_id")
    if not task_id:
        return None
    attempt = int(manifest_entry.get("attempt", 1))
    output_root = Path(item.output_root)
    if not output_root.exists():
        return None
    task_token = str(task_id).replace(":", "_")
    pattern = f"*{task_token}-codex-rescue-attempt{attempt}-*"
    for candidate in sorted(output_root.rglob(pattern), reverse=True):
        if not candidate.is_dir():
            continue
        if (candidate / "result.json").exists():
            continue
        if (
            (candidate / "prompt.txt").exists()
            or (candidate / "task").exists()
            or (candidate / "codex_events.jsonl").exists()
            or (candidate / "codex_stderr.txt").exists()
        ):
            return candidate
    return None


def active_runner_parent_items(ps_output: str | None = None) -> list[QueueItem]:
    ps_text = ps_output if ps_output is not None else get_ps_output()
    items: list[QueueItem] = []
    for line in ps_text.splitlines():
        if not is_active_runner_parent_command(line):
            continue
        argv = tokenize_process_command(line)
        manifest = extract_argv_flag_value(argv, "--manifest")
        output_root = extract_argv_flag_value(argv, "--output-root")
        if not manifest or not output_root:
            continue
        items.append(
            QueueItem(
                manifest=manifest,
                output_root=output_root,
                name=Path(manifest).stem or "runner_parent",
            )
        )
    return items


def collect_active_runner_parent_roots(ps_output: str | None = None) -> set[Path]:
    ps_text = ps_output if ps_output is not None else get_ps_output()
    roots: set[Path] = set()
    for item in active_runner_parent_items(ps_text):
        run_root = find_existing_item_run(item, ps_output=ps_text)
        if run_root is None or run_root == Path(item.manifest):
            run_root = find_latest_incomplete_item_run(item)
        if run_root is None or run_root == Path(item.manifest):
            continue
        roots.add(run_root.resolve())
    return roots


def find_existing_item_run(item: QueueItem, ps_output: str | None = None) -> Path | None:
    manifest_entry = load_single_task_manifest(Path(item.manifest))
    if manifest_entry is None:
        return None
    task_id = manifest_entry.get("task_id")
    if not task_id:
        return None
    attempt = int(manifest_entry.get("attempt", 1))
    output_root = Path(item.output_root)
    ps_text = ps_output if ps_output is not None else get_ps_output()
    manifest_process_active = has_active_runner_for_manifest(item, ps_output=ps_text)
    if not output_root.exists():
        return Path(item.manifest) if manifest_process_active else None
    task_token = str(task_id).replace(":", "_")
    pattern = f"*{task_token}-codex-rescue-attempt{attempt}-*"
    task_matches: list[Path] = []
    manifest_matches: list[Path] = []
    for candidate in sorted(output_root.rglob(pattern), reverse=True):
        if not candidate.is_dir():
            continue
        if str(candidate / "task") in ps_text:
            task_matches.append(candidate)
            continue
        if item.manifest in ps_text:
            manifest_matches.append(candidate)
    for bucket in (task_matches, manifest_matches):
        if bucket:
            return bucket[0]
    return Path(item.manifest) if manifest_process_active else None


def find_successful_task_run(task_id: str, results_root: Path) -> Path | None:
    if not task_id or not results_root.exists():
        return None
    task_token = str(task_id).replace(":", "_")
    pattern = f"*{task_token}-codex-rescue-attempt*-*"
    success_matches: list[Path] = []
    for candidate in sorted(results_root.rglob(pattern), reverse=True):
        if not candidate.is_dir():
            continue
        result_path = candidate / "result.json"
        if not result_path.exists():
            continue
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("task_id") != task_id:
            continue
        if payload.get("status") == "success" or any(
            record.get("verdict") == "verified_success" for record in (payload.get("records") or [])
        ):
            success_matches.append(candidate)
    return success_matches[0] if success_matches else None


def load_task_result_history_summary(task_id: str, results_root: Path) -> dict[str, Any]:
    summary = {
        "task_id": task_id,
        "latest_status": None,
        "cumulative_no_vul_records": 0,
        "latest_run_root": None,
    }
    if not task_id or not results_root.exists():
        return summary
    task_token = str(task_id).replace(":", "_")
    pattern = f"*{task_token}-codex-rescue-attempt*-*"
    latest_order: tuple[float, float, float] | None = None
    for candidate in sorted(results_root.rglob(pattern), reverse=True):
        if not candidate.is_dir():
            continue
        result_path = candidate / "result.json"
        if not result_path.exists():
            continue
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("task_id") != task_id:
            continue
        summary["cumulative_no_vul_records"] += sum(
            1 for record in (payload.get("records") or []) if record.get("verdict") == "no_vul_crash"
        )
        order_key = result_order_key(payload, result_path)
        if latest_order is None or order_key > latest_order:
            latest_order = order_key
            summary["latest_status"] = payload.get("status")
            summary["latest_run_root"] = candidate
    return summary


def find_active_task_run(task_id: str, results_root: Path, ps_output: str | None = None) -> Path | None:
    if not task_id or not results_root.exists():
        return None
    ps_text = ps_output if ps_output is not None else get_ps_output()
    task_token = str(task_id).replace(":", "_")
    pattern = f"*{task_token}-codex-rescue-attempt*-*"
    manifest_paths: set[Path] = set()
    for line in ps_text.splitlines():
        if RUNNER_MARKER not in line:
            continue
        match = MANIFEST_PATH_PATTERN.search(line)
        if match is None:
            continue
        manifest_paths.add(Path(match.group(1)))
    active_manifest_paths_by_task_id: dict[str, list[Path]] = {}
    for manifest_path in manifest_paths:
        try:
            manifest_task_id = extract_manifest_task_id(manifest_path)
        except Exception:
            continue
        if manifest_task_id:
            active_manifest_paths_by_task_id.setdefault(manifest_task_id, []).append(manifest_path)
    candidate_runs = sorted(results_root.rglob(pattern), reverse=True)
    for candidate in candidate_runs:
        if not candidate.is_dir():
            continue
        if (candidate / "result.json").exists():
            continue
        if str(candidate / "task") in ps_text:
            return candidate
    active_manifest_paths = active_manifest_paths_by_task_id.get(task_id) or []
    if active_manifest_paths:
        for candidate in candidate_runs:
            if not candidate.is_dir():
                continue
            if (candidate / "result.json").exists():
                continue
            if (
                (candidate / "prompt.txt").exists()
                or (candidate / "task").exists()
                or (candidate / "task_generation.stdout.txt").exists()
                or (candidate / "task_generation.stderr.txt").exists()
                or (candidate / "codex_events.jsonl").exists()
                or (candidate / "codex_stderr.txt").exists()
            ):
                return candidate
        return sorted(active_manifest_paths, reverse=True)[0]
    return None


def build_runner_command(item: QueueItem, args: argparse.Namespace) -> list[str]:
    return [
        args.python_bin,
        "-u",
        "scripts/codex_rescue_runner.py",
        "run",
        "--manifest",
        item.manifest,
        "--output-root",
        item.output_root,
        "--server",
        args.server,
        "--data-dir",
        args.data_dir,
        "--pocdb-path",
        args.pocdb_path,
    ]


def resolve_json_field(payload: Any, field_path: str) -> Any:
    value = payload
    for part in field_path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(field_path)
        value = value[part]
    return value


def is_item_ready(item: QueueItem) -> tuple[bool, str | None]:
    if not item.ready_json_path:
        return True, None

    json_path = Path(item.ready_json_path)
    if not json_path.exists():
        return False, f"ready_json_path_missing:{json_path}"

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if not item.ready_json_field:
        return True, None

    try:
        value = resolve_json_field(payload, item.ready_json_field)
    except KeyError:
        return False, f"ready_json_field_missing:{item.ready_json_field}"

    if item.ready_json_value is None:
        return bool(value), f"ready_json_field_false:{item.ready_json_field}"

    actual_value = str(value)
    if actual_value != item.ready_json_value:
        return False, f"ready_json_mismatch:{item.ready_json_field}={actual_value}"
    return True, None


def is_item_runtime_ready(item: QueueItem, local_images: set[str] | None) -> tuple[bool, dict[str, Any] | None]:
    try:
        task_id = extract_manifest_task_id(Path(item.manifest))
    except Exception:
        task_id = None
    if not task_id or local_images is None:
        return True, None
    probe = rescue.inspect_runtime_assets(task_id, local_images=local_images)
    if probe["issues"] or probe["missing_images"]:
        return False, probe
    return True, probe


def append_log(log_path: Path, message: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_utc().isoformat()} {message}\n")


def parse_usage_limit_reset_at(message: str) -> datetime | None:
    if not message:
        return None
    match = USAGE_LIMIT_RESET_PATTERN.search(message)
    if match is None:
        return None
    raw_value = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", match.group(1), flags=re.IGNORECASE)
    local_tz = datetime.now().astimezone().tzinfo or UTC
    for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            parsed = datetime.strptime(raw_value, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=local_tz).astimezone(UTC)
    return None


def extract_usage_limit_message(events_path: Path) -> str | None:
    try:
        lines = events_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:  # noqa: BLE001
        return None
    for line in reversed(lines):
        if USAGE_LIMIT_MESSAGE_FRAGMENT not in line.lower():
            continue
        try:
            payload = json.loads(line)
        except Exception:  # noqa: BLE001
            payload = None
        if isinstance(payload, dict):
            candidates: list[Any] = [payload.get("message")]
            error_payload = payload.get("error")
            if isinstance(error_payload, dict):
                candidates.append(error_payload.get("message"))
            item_payload = payload.get("item")
            if isinstance(item_payload, dict):
                candidates.append(item_payload.get("message"))
                nested_error = item_payload.get("error")
                if isinstance(nested_error, dict):
                    candidates.append(nested_error.get("message"))
            for candidate in candidates:
                if isinstance(candidate, str) and USAGE_LIMIT_MESSAGE_FRAGMENT in candidate.lower():
                    return candidate
        if USAGE_LIMIT_MESSAGE_FRAGMENT in line.lower():
            return line
    return None


def find_usage_limit_block(
    results_root: Path,
    *,
    queue_started_at: datetime | None,
    scan_files: int = DEFAULT_USAGE_LIMIT_SCAN_FILES,
    current_time: datetime | None = None,
) -> UsageLimitBlock | None:
    if scan_files <= 0 or not results_root.exists():
        return None
    queue_start_ts = queue_started_at.timestamp() if queue_started_at is not None else None
    event_paths: list[Path] = []
    for events_path in results_root.rglob("codex_events.jsonl"):
        if not events_path.is_file():
            continue
        event_paths.append(events_path)
    event_paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for events_path in event_paths[:scan_files]:
        try:
            mtime = events_path.stat().st_mtime
        except OSError:
            continue
        message = extract_usage_limit_message(events_path)
        if message is None:
            continue
        reset_at = parse_usage_limit_reset_at(message)
        if queue_start_ts is not None and mtime >= queue_start_ts:
            return UsageLimitBlock(run_root=events_path.parent, events_path=events_path, message=message, reset_at=reset_at)
    return None


def acquire_queue_lock(queue_file: Path, scheduler_log: Path):
    lock_path = queue_file.with_suffix(queue_file.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        append_log(scheduler_log, f"lock_busy queue_file={queue_file} lock_path={lock_path}")
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(now_utc().isoformat()) + "\n")
    handle.flush()
    return handle


def acquire_task_lock(task_id: str, results_root: Path, scheduler_log: Path):
    task_slug = str(task_id).replace(":", "_")
    lock_dir = results_root / ".task_launch_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{task_slug}.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        append_log(scheduler_log, f"task_lock_busy task_id={task_id} lock_path={lock_path}")
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(now_utc().isoformat()) + "\n")
    handle.flush()
    return handle


def release_inactive_task_locks(
    launched_task_locks: dict[str, Any],
    *,
    results_root: Path,
    ps_output: str | None,
    scheduler_log: Path,
) -> int:
    released = 0
    for task_id, handle in list(launched_task_locks.items()):
        active_task_run = find_active_task_run(task_id, results_root, ps_output=ps_output)
        if active_task_run is not None:
            continue
        append_log(scheduler_log, f"task_lock_released task_id={task_id}")
        handle.close()
        del launched_task_locks[task_id]
        released += 1
    return released


def launch_manifest(item: QueueItem, args: argparse.Namespace, scheduler_log: Path) -> int:
    run_log_dir = Path(args.run_log_dir)
    run_log_dir.mkdir(parents=True, exist_ok=True)
    run_log = run_log_dir / f"{item.name}.log"
    command = build_runner_command(item, args)
    append_log(scheduler_log, f"launching {item.manifest} -> {item.output_root}")
    with run_log.open("a", encoding="utf-8") as handle:
        process = subprocess.Popen(command, stdout=handle, stderr=handle, text=True)  # noqa: S603
    append_log(scheduler_log, f"launched pid={process.pid} log={run_log}")
    return process.pid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch queued rescue manifests when runner slots free up.")
    parser.add_argument("--queue-file", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--pocdb-path", required=True)
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--max-active-runners", type=int, default=4)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--scheduler-log", default="codex_rescue_runs_local/rescue_queue_launcher.log")
    parser.add_argument("--run-log-dir", default="codex_rescue_runs_local/queue_logs")
    parser.add_argument("--results-root", default="codex_rescue_runs_local")
    parser.add_argument("--state-file")
    parser.add_argument("--skip-cumulative-no-vul-threshold", type=int, default=DEFAULT_SKIP_CUMULATIVE_NO_VUL_THRESHOLD)
    parser.add_argument("--usage-limit-scan-files", type=int, default=DEFAULT_USAGE_LIMIT_SCAN_FILES)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scheduler_log = Path(args.scheduler_log)
    queue_file = Path(args.queue_file)
    raw_state_file = getattr(args, "state_file", None)
    state_file = Path(raw_state_file) if raw_state_file else default_state_file_path(queue_file)
    lock_handle = acquire_queue_lock(queue_file, scheduler_log)
    if lock_handle is None:
        return 0
    queue_started_at = now_utc()
    initial_queue_items = load_queue(queue_file)
    append_log(scheduler_log, f"starting queue with {len(initial_queue_items)} items")
    state = load_queue_state(state_file)
    processed_names: set[str] = set(state.processed_names)
    if processed_names:
        append_log(
            scheduler_log,
            f"loaded_state processed_names={len(processed_names)} queue_complete={state.queue_complete} state_file={state_file}",
        )
    else:
        write_queue_state(state_file, processed_names=processed_names, queue_complete=False)
    deferred_names: set[str] = set()
    launched_task_locks: dict[str, Any] = {}
    reserved_item_name: str | None = None
    reserved_task_id: str | None = None
    reserved_task_lock: Any | None = None

    while True:
        ps_output = get_ps_output()
        results_root = Path(args.results_root)
        release_inactive_task_locks(
            launched_task_locks,
            results_root=results_root,
            ps_output=ps_output,
            scheduler_log=scheduler_log,
        )
        queue_items = load_queue(queue_file)
        pending_items = pending_queue_items(queue_items, processed_names, deferred_names)
        if not pending_items:
            if launched_task_locks:
                append_log(
                    scheduler_log,
                    f"waiting: launched_task_locks={len(launched_task_locks)} pending_items=0",
                )
                time.sleep(args.poll_seconds)
                continue
            write_queue_state(state_file, processed_names=processed_names, queue_complete=True)
            append_log(scheduler_log, "queue complete")
            return 0
        write_queue_state(state_file, processed_names=processed_names, queue_complete=False)
        usage_limit_block = find_usage_limit_block(
            results_root,
            queue_started_at=queue_started_at,
            scan_files=args.usage_limit_scan_files,
        )
        if usage_limit_block is not None:
            reset_at = usage_limit_block.reset_at.isoformat() if usage_limit_block.reset_at is not None else "unknown"
            append_log(
                scheduler_log,
                (
                    "halting: usage_limit_detected "
                    f"pending_items={len(pending_items)} "
                    f"run_root={usage_limit_block.run_root} "
                    f"reset_at={reset_at} "
                    f"events_path={usage_limit_block.events_path}"
                ),
            )
            return 0
        try:
            local_images = rescue.list_local_docker_images()
        except Exception:  # noqa: BLE001
            local_images = None
        waited = False
        made_progress = False
        for item in pending_items:
            task_id = extract_manifest_task_id(Path(item.manifest))
            if reserved_item_name != item.name:
                if reserved_task_lock is not None:
                    reserved_task_lock.close()
                    reserved_task_lock = None
                    reserved_task_id = None
                    reserved_item_name = None
                if task_id:
                    reserved_task_lock = acquire_task_lock(task_id, results_root, scheduler_log)
                    if reserved_task_lock is None:
                        append_log(scheduler_log, f"deferring: task_locked_elsewhere next={item.name} task_id={task_id}")
                        deferred_names.add(item.name)
                        continue
                    reserved_item_name = item.name
                    reserved_task_id = task_id
            existing_run = find_existing_item_run(item, ps_output=ps_output)
            if existing_run is not None:
                append_log(scheduler_log, f"skipping: already_launched next={item.name} run_root={existing_run}")
                if reserved_task_lock is not None:
                    reserved_task_lock.close()
                    reserved_task_lock = None
                    reserved_task_id = None
                    reserved_item_name = None
                processed_names.add(item.name)
                deferred_names.discard(item.name)
                write_queue_state(state_file, processed_names=processed_names, queue_complete=False)
                made_progress = True
                continue
            if task_id:
                success_run = find_successful_task_run(task_id, results_root)
                if success_run is not None:
                    append_log(
                        scheduler_log,
                        f"skipping: task_already_success next={item.name} task_id={task_id} run_root={success_run}",
                    )
                    if reserved_task_lock is not None:
                        reserved_task_lock.close()
                        reserved_task_lock = None
                        reserved_task_id = None
                        reserved_item_name = None
                    processed_names.add(item.name)
                    deferred_names.discard(item.name)
                    write_queue_state(state_file, processed_names=processed_names, queue_complete=False)
                    made_progress = True
                    continue
                task_history_summary = load_task_result_history_summary(task_id, results_root)
                if (
                    int(task_history_summary.get("cumulative_no_vul_records") or 0) >= args.skip_cumulative_no_vul_threshold
                    and task_history_summary.get("latest_status") != "success"
                ):
                    append_log(
                        scheduler_log,
                        (
                            f"skipping: task_exhausted_no_vul next={item.name} task_id={task_id} "
                            f"cumulative_no_vul={task_history_summary['cumulative_no_vul_records']} "
                            f"latest_status={task_history_summary.get('latest_status')} "
                            f"run_root={task_history_summary.get('latest_run_root')}"
                        ),
                    )
                    if reserved_task_lock is not None:
                        reserved_task_lock.close()
                        reserved_task_lock = None
                        reserved_task_id = None
                        reserved_item_name = None
                    processed_names.add(item.name)
                    deferred_names.discard(item.name)
                    write_queue_state(state_file, processed_names=processed_names, queue_complete=False)
                    made_progress = True
                    continue
                active_task_run = find_active_task_run(task_id, results_root, ps_output=ps_output)
                if active_task_run is not None:
                    append_log(
                        scheduler_log,
                        f"deferring: task_active_elsewhere next={item.name} task_id={task_id} run_root={active_task_run}",
                    )
                    if reserved_task_lock is not None:
                        reserved_task_lock.close()
                        reserved_task_lock = None
                        reserved_task_id = None
                        reserved_item_name = None
                    deferred_names.add(item.name)
                    continue
            runtime_ready, runtime_probe = is_item_runtime_ready(item, local_images)
            if not runtime_ready:
                append_log(
                    scheduler_log,
                    (
                        f"deferring: runtime_not_ready next={item.name} task_id={task_id} "
                        f"missing_images={runtime_probe.get('missing_images')} issues={runtime_probe.get('issues')}"
                    ),
                )
                if reserved_task_lock is not None:
                    reserved_task_lock.close()
                    reserved_task_lock = None
                    reserved_task_id = None
                    reserved_item_name = None
                deferred_names.add(item.name)
                continue
            ready, reason = is_item_ready(item)
            if not ready:
                append_log(scheduler_log, f"waiting: item_not_ready next={item.name} reason={reason}")
                waited = True
                break
            active = get_active_runner_count(ps_output)
            if active < args.max_active_runners:
                launch_manifest(item, args, scheduler_log)
                if reserved_task_id and reserved_task_lock is not None:
                    launched_task_locks[reserved_task_id] = reserved_task_lock
                    reserved_task_lock = None
                    reserved_task_id = None
                    reserved_item_name = None
                processed_names.add(item.name)
                deferred_names.discard(item.name)
                write_queue_state(state_file, processed_names=processed_names, queue_complete=False)
                made_progress = True
                break
            append_log(scheduler_log, f"waiting: active_runners={active} max={args.max_active_runners} next={item.name}")
            waited = True
            break
        if made_progress:
            continue
        if not waited:
            append_log(scheduler_log, f"waiting: deferred_items={len(deferred_names)} pending_items={len(pending_items)}")
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
