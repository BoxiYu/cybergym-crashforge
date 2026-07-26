from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
import time
from typing import Any

import docker

BASE_RUNNER_IMAGE = "cybergym/oss-fuzz-base-runner:latest"
TARGET_PULL_IMAGE_PREFIXES = (
    "cybergym/oss-fuzz:",
    "n132/arvo:",
    "cybergym/oss-fuzz-base-runner:",
)
TRANSIENT_PULL_ERROR_SUBSTRINGS = (
    "tls handshake timeout",
    "client.timeout exceeded while awaiting headers",
    "context deadline exceeded",
    "unexpected eof",
    "connection reset by peer",
    "temporary failure in name resolution",
    "no route to host",
    "i/o timeout",
    "net/http: timeout awaiting response headers",
)


def load_missing_entries(jsonl_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def extract_unique_task_ids(entries: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in entries:
        task_id = entry["task_id"]
        if task_id not in seen:
            seen.add(task_id)
            ordered.append(task_id)
    return ordered


def build_pull_plan(task_ids: list[str], include_base_runner: bool = True) -> dict[str, Any]:
    repos: dict[str, set[str]] = defaultdict(set)
    unsupported_task_ids: list[str] = []
    ordered_images: list[str] = []
    seen_images: set[str] = set()

    def add_image(repo: str, tag: str):
        repos[repo].add(tag)
        image = f"{repo}:{tag}"
        if image not in seen_images:
            seen_images.add(image)
            ordered_images.append(image)

    if include_base_runner:
        add_image("cybergym/oss-fuzz-base-runner", "latest")

    for task_id in task_ids:
        family, subid = task_id.split(":", 1)
        if family == "arvo":
            add_image("n132/arvo", f"{subid}-fix")
            add_image("n132/arvo", f"{subid}-vul")
        elif family == "oss-fuzz":
            add_image("cybergym/oss-fuzz", f"{subid}-fix")
            add_image("cybergym/oss-fuzz", f"{subid}-vul")
        else:
            unsupported_task_ids.append(task_id)

    return {
        "task_count": len(task_ids),
        "image_count": sum(len(tags) for tags in repos.values()),
        "ordered_images": ordered_images,
        "repos": {repo: sorted(tags) for repo, tags in sorted(repos.items())},
        "unsupported_task_ids": unsupported_task_ids,
    }


def filter_tasks_metadata(tasks: list[dict[str, Any]], allowed_task_ids: set[str]) -> list[dict[str, Any]]:
    return [task for task in tasks if task.get("task_id") in allowed_task_ids]


def flatten_images(plan: dict[str, Any]) -> list[str]:
    ordered_images = plan.get("ordered_images")
    if isinstance(ordered_images, list) and ordered_images:
        return [str(image) for image in ordered_images]
    images: list[str] = []
    for repo, tags in plan["repos"].items():
        images.extend(f"{repo}:{tag}" for tag in tags)
    return images


def get_local_images() -> set[str]:
    client = docker.from_env()
    local_images: set[str] = set()
    for image in client.images.list():
        for tag in image.tags:
            if tag:
                local_images.add(tag)
    return local_images


def filter_missing_images(images: list[str], existing_images: set[str]) -> list[str]:
    return [image for image in images if image not in existing_images]


def is_transient_pull_error(status: str, error: str | None) -> bool:
    if not error:
        return False
    normalized = error.lower()
    if status == "timeout":
        return True
    return any(fragment in normalized for fragment in TRANSIENT_PULL_ERROR_SUBSTRINGS)


def write_json(path: Path, payload: Any):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]):
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def summarize_output(text: str, max_lines: int = 20) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


def coerce_output_text(part: str | bytes | None) -> str:
    if part is None:
        return ""
    if isinstance(part, bytes):
        return part.decode("utf-8", errors="replace")
    return part


def write_shell_script(images: list[str], output_path: Path):
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
    ]
    lines.extend(f"docker pull {image}" for image in images)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output_path.chmod(0o755)


def acquire_output_lock(output_dir: Path):
    lock_path = output_dir / ".download_missing_images.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} started={time.time():.6f}\n")
    handle.flush()
    return handle


def path_age_seconds(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def read_lock_owner_pid(lock_path: Path) -> int | None:
    if not lock_path.exists():
        return None
    try:
        for raw_line in lock_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line.startswith("pid="):
                continue
            pid_text = line.split()[0].removeprefix("pid=")
            return int(pid_text)
    except (OSError, ValueError):
        return None
    return None


def parse_process_rows(ps_output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_line in ps_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=3)
        if len(parts) != 4:
            continue
        pid_text, ppid_text, elapsed_text, args = parts
        try:
            rows.append(
                {
                    "pid": int(pid_text),
                    "ppid": int(ppid_text),
                    "elapsed_seconds": int(elapsed_text),
                    "args": args,
                }
            )
        except ValueError:
            continue
    return rows


def is_target_pull_command(args: str) -> bool:
    if not args.startswith("docker pull "):
        return False
    image = args.removeprefix("docker pull ").strip()
    return image.startswith(TARGET_PULL_IMAGE_PREFIXES)


def list_active_pull_images(ps_output: str | None = None) -> set[str]:
    if ps_output is None:
        result = subprocess.run(["ps", "-eo", "pid=,ppid=,etimes=,args="], check=True, capture_output=True, text=True)
        ps_text = result.stdout
    else:
        ps_text = ps_output
    images: set[str] = set()
    for row in parse_process_rows(ps_text):
        args = str(row["args"])
        if not is_target_pull_command(args):
            continue
        image = args.removeprefix("docker pull ").strip()
        if image:
            images.add(image)
    return images


def list_stale_orphan_pull_pids(
    *,
    min_elapsed_seconds: int,
    ps_output: str | None = None,
) -> list[int]:
    if ps_output is None:
        result = subprocess.run(["ps", "-eo", "pid=,ppid=,etimes=,args="], check=True, capture_output=True, text=True)
        ps_text = result.stdout
    else:
        ps_text = ps_output
    pids: list[int] = []
    for row in parse_process_rows(ps_text):
        if row["ppid"] != 1:
            continue
        if row["elapsed_seconds"] < min_elapsed_seconds:
            continue
        if not is_target_pull_command(str(row["args"])):
            continue
        pids.append(int(row["pid"]))
    return pids


def terminate_pid(pid: int):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(1)
    try:
        os.kill(pid, 0)
    except OSError:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def list_process_tree_pids(root_pid: int, ps_output: str | None = None) -> list[int]:
    if ps_output is None:
        result = subprocess.run(["ps", "-eo", "pid=,ppid=,etimes=,args="], check=True, capture_output=True, text=True)
        ps_text = result.stdout
    else:
        ps_text = ps_output
    rows = parse_process_rows(ps_text)
    children_by_ppid: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        children_by_ppid[int(row["ppid"])].append(int(row["pid"]))
    ordered: list[int] = []
    seen: set[int] = set()

    def visit(pid: int):
        if pid in seen:
            return
        seen.add(pid)
        ordered.append(pid)
        for child_pid in children_by_ppid.get(pid, []):
            visit(child_pid)

    visit(root_pid)
    return ordered


def owner_process_has_active_pull_descendants(owner_pid: int, ps_output: str | None = None) -> bool:
    if owner_pid <= 0:
        return False
    if ps_output is None:
        result = subprocess.run(["ps", "-eo", "pid=,ppid=,etimes=,args="], check=True, capture_output=True, text=True)
        ps_text = result.stdout
    else:
        ps_text = ps_output
    rows = parse_process_rows(ps_text)
    rows_by_pid = {int(row["pid"]): row for row in rows}
    if owner_pid not in rows_by_pid:
        return False
    for pid in list_process_tree_pids(owner_pid, ps_output=ps_text):
        row = rows_by_pid.get(pid)
        if row is None:
            continue
        if is_target_pull_command(str(row["args"])):
            return True
    return False


def terminate_process_tree(root_pid: int, ps_output: str | None = None) -> list[int]:
    ordered = list_process_tree_pids(root_pid, ps_output=ps_output)
    for pid in reversed(ordered):
        terminate_pid(pid)
    return ordered


def acquire_output_lock_with_stale_takeover(
    output_dir: Path,
    *,
    stale_progress_seconds: int = 0,
    progress_path: Path | None = None,
):
    handle = acquire_output_lock(output_dir)
    if handle is not None:
        return handle, None, []
    if stale_progress_seconds <= 0 or progress_path is None:
        return None, None, []
    age_seconds = path_age_seconds(progress_path)
    if age_seconds is None or age_seconds < stale_progress_seconds:
        return None, None, []

    lock_path = output_dir / ".download_missing_images.lock"
    owner_pid = read_lock_owner_pid(lock_path)
    if owner_pid is not None and owner_process_has_active_pull_descendants(owner_pid):
        return None, owner_pid, []
    terminated_pids: list[int] = []
    if owner_pid is not None:
        terminated_pids = terminate_process_tree(owner_pid)

    deadline = time.time() + 5
    while time.time() < deadline:
        handle = acquire_output_lock(output_dir)
        if handle is not None:
            return handle, owner_pid, terminated_pids
        time.sleep(0.2)
    return None, owner_pid, terminated_pids


def reap_stale_orphan_pull_processes(min_elapsed_seconds: int) -> list[int]:
    pids = list_stale_orphan_pull_pids(min_elapsed_seconds=min_elapsed_seconds)
    for pid in pids:
        terminate_pid(pid)
    return pids


def execute_pull_plan(
    images: list[str],
    max_workers: int,
    pull_timeout_seconds: int,
    max_retries: int = 2,
    retry_backoff_seconds: int = 5,
    progress_path: Path | None = None,
    summary_path: Path | None = None,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    results: dict[str, Any] = {
        "pulled": [],
        "failed": [],
        "timed_out": [],
        "failed_details": [],
    }
    total = len(images)
    started = 0
    completed = 0
    state_lock = Lock()

    def _write_running_summary():
        if summary_path is None or summary is None:
            return
        inflight_count = max(started - completed, 0)
        unfinished_count = max(total - completed, 0)
        running_summary = dict(summary)
        running_summary.update(
            {
                "status": "running",
                "started_count": started,
                "completed_count": completed,
                "inflight_count": inflight_count,
                "active_pull_image_count": inflight_count,
                "pending_image_count": unfinished_count,
                "pulled_count": len(results["pulled"]),
                "failed_count": len(results["failed"]),
                "timed_out_count": len(results["timed_out"]),
                "last_progress_time": time.time(),
            }
        )
        write_json(summary_path, running_summary)

    def _append_retry_event(image: str, *, attempt: int, next_attempt: int, elapsed: float, error: str):
        if progress_path is None:
            return
        with state_lock:
            append_jsonl(
                progress_path,
                {
                    "started_count": started,
                    "completed_count": completed,
                    "total_count": total,
                    "image": image,
                    "status": "retrying",
                    "attempt": attempt,
                    "next_attempt": next_attempt,
                    "seconds": round(elapsed, 3),
                    "error": error,
                    "event_time": time.time(),
                },
            )
            _write_running_summary()

    def _pull(image: str) -> tuple[str, str, float, str | None, int]:
        total_attempts = max(max_retries, 0) + 1
        for attempt in range(1, total_attempts + 1):
            started = time.monotonic()
            print(f"Pulling {image} (attempt {attempt}/{total_attempts})...", flush=True)
            try:
                proc = subprocess.run(
                    ["docker", "pull", image],
                    capture_output=True,
                    text=True,
                    timeout=pull_timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                elapsed = time.monotonic() - started
                error_text = summarize_output(
                    "\n".join(
                        text_part
                        for text_part in (coerce_output_text(exc.stdout), coerce_output_text(exc.stderr))
                        if text_part
                    )
                )
                message = f"timed out after {pull_timeout_seconds}s"
                if error_text:
                    message = f"{message}; output tail:\n{error_text}"
                print(f"Timed out {image} after {elapsed:.1f}s", flush=True)
                status = "timeout"
                error = message
            else:
                elapsed = time.monotonic() - started
                if proc.returncode == 0:
                    print(f"Pulled {image} in {elapsed:.1f}s", flush=True)
                    return image, "pulled", elapsed, None, attempt
                error_text = summarize_output("\n".join(part for part in [proc.stdout, proc.stderr] if part))
                if not error_text:
                    error_text = f"docker pull exited with code {proc.returncode}"
                print(f"Failed {image} in {elapsed:.1f}s", flush=True)
                status = "failed"
                error = error_text

            if attempt >= total_attempts or not is_transient_pull_error(status, error):
                return image, status, elapsed, error, attempt

            backoff = max(retry_backoff_seconds, 0) * attempt
            print(
                f"Retrying {image} after transient {status} on attempt {attempt}/{total_attempts}: "
                f"sleeping {backoff}s",
                flush=True,
            )
            _append_retry_event(
                image,
                attempt=attempt,
                next_attempt=attempt + 1,
                elapsed=elapsed,
                error=error,
            )
            if backoff > 0:
                time.sleep(backoff)

        raise AssertionError("unreachable")

    def _record_start(image: str):
        nonlocal started
        with state_lock:
            started += 1
            if progress_path is not None:
                append_jsonl(
                    progress_path,
                    {
                        "started_count": started,
                        "completed_count": completed,
                        "total_count": total,
                        "image": image,
                        "status": "started",
                        "event_time": time.time(),
                    },
                )
            _write_running_summary()

    def _record(image: str, status: str, elapsed: float, error: str | None, attempts: int):
        nonlocal completed
        with state_lock:
            completed += 1
            if status == "pulled":
                results["pulled"].append(image)
            else:
                results["failed"].append(image)
                if status == "timeout":
                    results["timed_out"].append(image)
                results["failed_details"].append(
                    {
                        "image": image,
                        "status": status,
                        "seconds": round(elapsed, 3),
                        "attempts": attempts,
                        "error": error,
                    }
                )

            if progress_path is not None:
                entry = {
                    "started_count": started,
                    "completed_count": completed,
                    "total_count": total,
                    "image": image,
                    "status": status,
                    "seconds": round(elapsed, 3),
                    "attempts": attempts,
                    "event_time": time.time(),
                }
                if error:
                    entry["error"] = error
                append_jsonl(progress_path, entry)
            _write_running_summary()

    if max_workers <= 1:
        for image in images:
            _record_start(image)
            _record(*_pull(image))
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for image in images:
            _record_start(image)
            futures[executor.submit(_pull, image)] = image
        for future in as_completed(futures):
            _record(*future.result())

    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and optionally execute a docker pull plan for missing rescue images.")
    parser.add_argument("--missing-jsonl", type=Path, required=True, help="Path to missing_image.jsonl from rescue partition output.")
    parser.add_argument("--tasks-file", type=Path, default=Path("cybergym_data/tasks.json"), help="Full tasks metadata JSON.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory to write the pull plan artifacts.")
    parser.add_argument("--max-workers", type=int, default=4, help="Concurrent docker pull workers when --execute is used.")
    parser.add_argument("--no-base-runner", action="store_true", help="Do not include cybergym/oss-fuzz-base-runner:latest.")
    parser.add_argument("--no-skip-existing", action="store_true", help="Attempt to pull images even if they already exist locally.")
    parser.add_argument(
        "--pull-timeout-seconds",
        type=int,
        default=1800,
        help="Timeout for a single docker pull attempt when --execute is used.",
    )
    parser.add_argument(
        "--stale-progress-seconds",
        type=int,
        default=0,
        help="When the output-dir progress file is older than this threshold, terminate the lock owner and take over.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Retry transient docker pull failures up to this many additional attempts.",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=int,
        default=5,
        help="Base backoff in seconds between transient docker pull retries; multiplied by the attempt number.",
    )
    parser.add_argument("--execute", action="store_true", help="Actually run docker pull for every planned image.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = None
    stale_lock_owner_pid = None
    stale_lock_terminated_pids: list[int] = []
    if args.execute:
        lock_handle, stale_lock_owner_pid, stale_lock_terminated_pids = acquire_output_lock_with_stale_takeover(
            args.output_dir,
            stale_progress_seconds=args.stale_progress_seconds,
            progress_path=args.output_dir / "pull_progress.jsonl",
        )
        if lock_handle is None:
            print(f"download_missing_images lock busy: {args.output_dir}", flush=True)
            return 0

    entries = load_missing_entries(args.missing_jsonl)
    task_ids = extract_unique_task_ids(entries)
    plan = build_pull_plan(task_ids, include_base_runner=not args.no_base_runner)

    tasks = json.loads(args.tasks_file.read_text(encoding="utf-8"))
    filtered_tasks = filter_tasks_metadata(tasks, set(task_ids))
    images = flatten_images(plan)
    existing_images: set[str] = set()
    active_pull_images: set[str] = set()
    if not args.no_skip_existing:
        existing_images = get_local_images()
        active_pull_images = list_active_pull_images()
    pending_images = images if args.no_skip_existing else filter_missing_images(
        filter_missing_images(images, existing_images),
        active_pull_images,
    )

    (args.output_dir / "task_ids.txt").write_text("\n".join(task_ids) + ("\n" if task_ids else ""), encoding="utf-8")
    (args.output_dir / "tasks_subset.json").write_text(json.dumps(filtered_tasks, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_json(args.output_dir / "pull_plan.json", plan)
    (args.output_dir / "images.txt").write_text("\n".join(images) + ("\n" if images else ""), encoding="utf-8")
    (args.output_dir / "pending_images.txt").write_text(
        "\n".join(pending_images) + ("\n" if pending_images else ""), encoding="utf-8"
    )
    write_shell_script(pending_images, args.output_dir / "docker_pull_commands.sh")

    summary = {
        "missing_entries": len(entries),
        "unique_task_ids": len(task_ids),
        "tasks_subset_count": len(filtered_tasks),
        "image_count": len(images),
        "existing_image_count": len(existing_images),
        "active_pull_image_count": len(active_pull_images),
        "pending_image_count": len(pending_images),
        "skip_existing": not args.no_skip_existing,
        "execute": args.execute,
        "max_workers": args.max_workers,
        "pull_timeout_seconds": args.pull_timeout_seconds,
        "max_retries": args.max_retries,
        "retry_backoff_seconds": args.retry_backoff_seconds,
        "stale_progress_seconds": args.stale_progress_seconds,
        "status": "planned",
        "unsupported_task_ids": plan["unsupported_task_ids"],
    }

    summary_path = args.output_dir / "summary.json"
    try:
        if args.execute:
            reaped_orphan_pids = reap_stale_orphan_pull_processes(args.pull_timeout_seconds)
            summary["reaped_orphan_pull_pids"] = reaped_orphan_pids
            summary["stale_lock_owner_pid"] = stale_lock_owner_pid
            summary["stale_lock_terminated_pids"] = stale_lock_terminated_pids
            progress_path = args.output_dir / "pull_progress.jsonl"
            progress_path.write_text("", encoding="utf-8")
            summary["status"] = "running"
            write_json(summary_path, summary)
            pull_results = execute_pull_plan(
                pending_images,
                max_workers=args.max_workers,
                pull_timeout_seconds=args.pull_timeout_seconds,
                max_retries=args.max_retries,
                retry_backoff_seconds=args.retry_backoff_seconds,
                progress_path=progress_path,
                summary_path=summary_path,
                summary=summary,
            )
            summary["pulled_count"] = len(pull_results["pulled"])
            summary["failed_count"] = len(pull_results["failed"])
            summary["timed_out_count"] = len(pull_results["timed_out"])
            summary["started_count"] = len(pending_images)
            summary["completed_count"] = len(pending_images)
            summary["inflight_count"] = 0
            summary["active_pull_image_count"] = 0
            summary["pending_image_count"] = 0
            summary["status"] = "completed"
            write_json(args.output_dir / "pull_results.json", pull_results)
        else:
            write_json(summary_path, summary)

        write_json(summary_path, summary)
        return 0
    finally:
        if lock_handle is not None:
            lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
