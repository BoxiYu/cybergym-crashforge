from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import codex_rescue_runner as rescue
import rescue_queue_launcher as launcherq

CODEX_TASK_PATTERN = re.compile(r"\bcodex exec -C (\S+)")
RUN_ROOT_TASK_PATTERN = re.compile(r"(?P<family>arvo|oss-fuzz)_(?P<subid>[^-]+)-codex-rescue-attempt(?P<attempt>\d+)")
RUN_ROOT_TIMESTAMP_PATTERN = re.compile(r"^(?P<stamp>\d{8}-\d{6})-")
ARTIFACT_RELATIVE_PATHS = {
    "run_root": ".",
    "prompt": "prompt.txt",
    "codex_events": "codex_events.jsonl",
    "codex_last_message": "codex_last_message.md",
    "summary": "summary.md",
    "result": "result.json",
    "codex_stderr": "codex_stderr.txt",
    "task_generation_stdout": "task_generation.stdout.txt",
    "task_generation_stderr": "task_generation.stderr.txt",
    "verify_output": "verify_output.txt",
    "task_dir": "task",
}
RUN_ROOT_MARKER_FILES = {"result.json", "codex_events.jsonl", "prompt.txt", "codex_last_message.md", "verify_output.txt"}
RUN_ROOT_PRUNE_DIR_NAMES = {
    ".auto_submit_materialized",
    ".git",
    "__pycache__",
    "dist",
    "extracted",
    "node_modules",
    "out",
    "repo",
    "repo-fix",
    "repo-vul",
    "retained_task_files",
    "src",
    "src-fix",
    "src-vul",
    "target",
    "task",
    "unpacked",
}
RUN_ROOT_PRUNE_PREFIXES = ("afl_", "fixed-", "fresh-", "repo_", "repo-")


def now_utc() -> datetime:
    return datetime.now(UTC)


def append_log(log_path: Path, message: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_utc().isoformat()} {message}\n")


def try_read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except UnicodeDecodeError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not raw.strip():
        return None, "empty_file"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"JSONDecodeError: {exc.msg} (line {exc.lineno}, column {exc.colno})"


def write_json(path: Path, payload: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def safe_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def parse_run_root_metadata(run_root: Path) -> dict[str, Any]:
    match = RUN_ROOT_TASK_PATTERN.search(run_root.name)
    if not match:
        return {"task_id": None, "attempt": None, "task_family": None}
    family = match.group("family")
    subid = match.group("subid")
    return {
        "task_id": f"{family}:{subid}",
        "attempt": int(match.group("attempt")),
        "task_family": family,
    }


def infer_started_at(run_root: Path, *, prompt_path: Path, events_path: Path) -> str | None:
    match = RUN_ROOT_TIMESTAMP_PATTERN.match(run_root.name)
    if match:
        try:
            stamp = datetime.strptime(match.group("stamp"), "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            stamp = None
        else:
            return stamp.isoformat()
    for candidate in (prompt_path, events_path, run_root):
        if not candidate.exists():
            continue
        try:
            stat = candidate.stat()
        except OSError:
            continue
        return datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
    return None


def collect_active_run_roots() -> set[Path]:
    result = subprocess.run(["ps", "-eo", "args="], check=True, capture_output=True, text=True)
    active: set[Path] = set()
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if "codex exec -C " not in line:
            continue
        match = CODEX_TASK_PATTERN.search(line)
        if not match:
            continue
        task_dir = Path(match.group(1))
        if task_dir.name != "task":
            continue
        active.add(task_dir.parent.resolve())
    active.update(launcherq.collect_active_runner_parent_roots(ps_output=result.stdout))
    return active


def collect_run_roots(results_root: Path) -> list[Path]:
    roots: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(results_root, topdown=True, onerror=lambda _exc: None):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in RUN_ROOT_PRUNE_DIR_NAMES and not any(name.startswith(prefix) for prefix in RUN_ROOT_PRUNE_PREFIXES)
        ]
        if RUN_ROOT_MARKER_FILES.intersection(filenames):
            roots.add(Path(dirpath))
    return sorted(roots)


def verdict_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter()
    for record in records:
        verdict = str(record.get("verdict") or "unknown")
        counts[verdict] += 1
    return dict(sorted(counts.items()))


def build_artifact_paths(run_root: Path, results_root: Path, result_payload: dict[str, Any] | None) -> dict[str, str]:
    default_paths = {key: safe_relpath(run_root / rel_path, results_root) for key, rel_path in ARTIFACT_RELATIVE_PATHS.items() if key != "run_root"}
    default_paths["run_root"] = safe_relpath(run_root, results_root)
    result_paths = (result_payload or {}).get("paths") or {}
    merged = dict(default_paths)
    for key, value in result_paths.items():
        merged[key] = str(value)
    return merged


def path_from_recorded_value(value: str, results_root: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    workspace_root = results_root.parent
    if path.parts and path.parts[0] == results_root.name:
        return workspace_root / path
    results_candidate = results_root / path
    workspace_candidate = workspace_root / path
    if results_candidate.exists() or not workspace_candidate.exists():
        return results_candidate
    return workspace_candidate


def build_artifact_meta(artifact_paths: dict[str, str], results_root: Path) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for key, raw_path in artifact_paths.items():
        path = path_from_recorded_value(raw_path, results_root)
        exists = path.exists()
        kind = "missing"
        size_bytes = None
        modified_at = None
        if exists:
            stat = path.stat()
            kind = "directory" if path.is_dir() else "file"
            size_bytes = stat.st_size
            modified_at = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
        metadata[key] = {
            "path": raw_path,
            "exists": exists,
            "kind": kind,
            "size_bytes": size_bytes,
            "modified_at": modified_at,
        }
    return metadata


def build_run_record(run_root: Path, *, results_root: Path, active_run_roots: set[Path]) -> dict[str, Any]:
    result_path = run_root / "result.json"
    events_path = run_root / "codex_events.jsonl"
    prompt_path = run_root / "prompt.txt"
    last_message_path = run_root / "codex_last_message.md"
    summary_path = run_root / "summary.md"
    codex_stderr_path = run_root / "codex_stderr.txt"
    verify_output_path = run_root / "verify_output.txt"

    result_payload, result_error = try_read_json(result_path) if result_path.exists() else (None, None)
    parsed = parse_run_root_metadata(run_root)
    records = list((result_payload or {}).get("records") or [])
    rel_parts = Path(safe_relpath(run_root, results_root)).parts
    lane = rel_parts[0] if len(rel_parts) >= 1 else None
    campaign_dir = rel_parts[1] if len(rel_parts) >= 2 else None
    metrics = rescue.collect_codex_event_metrics(events_path) if events_path.exists() else {
        "completed_items": 0,
        "submit_started": 0,
        "submit_completed": 0,
    }
    counts = verdict_counts(records)
    resolved_run_root = run_root.resolve()
    is_active_run = resolved_run_root in active_run_roots
    status = (result_payload or {}).get("status")
    if status is None:
        if result_error:
            status = "active_invalid_result" if is_active_run else "invalid_result"
        else:
            status = "active_no_result" if is_active_run else "missing_result"
    artifact_paths = build_artifact_paths(run_root, results_root, result_payload)
    artifact_meta = build_artifact_meta(artifact_paths, results_root)
    has_core_trajectory = events_path.exists() and prompt_path.exists() and result_path.exists() and summary_path.exists()
    started_at = (result_payload or {}).get("started_at") or infer_started_at(
        run_root,
        prompt_path=prompt_path,
        events_path=events_path,
    )

    record = {
        "run_root": safe_relpath(run_root, results_root),
        "lane": lane,
        "campaign_dir": campaign_dir,
        "run_id": (result_payload or {}).get("run_id") or run_root.name,
        "task_id": (result_payload or {}).get("task_id") or parsed["task_id"],
        "task_family": (result_payload or {}).get("task_id", "").split(":", 1)[0] if (result_payload or {}).get("task_id") else parsed["task_family"],
        "attempt": (result_payload or {}).get("attempt") or parsed["attempt"],
        "agent_id": (result_payload or {}).get("agent_id"),
        "status": status,
        "solution_status": (result_payload or {}).get("solution_status"),
        "executor_status": (result_payload or {}).get("executor_status"),
        "failure_category": (result_payload or {}).get("failure_category"),
        "retryable": (result_payload or {}).get("retryable"),
        "started_at": started_at,
        "ended_at": (result_payload or {}).get("ended_at"),
        "has_result": result_payload is not None,
        "result_error": result_error,
        "has_events": events_path.exists(),
        "has_prompt": prompt_path.exists(),
        "has_last_message": last_message_path.exists(),
        "has_summary": summary_path.exists(),
        "has_codex_stderr": codex_stderr_path.exists(),
        "has_verify_output": verify_output_path.exists(),
        "has_core_trajectory": has_core_trajectory,
        "active_process": is_active_run,
        "record_count": len(records),
        "verdict_counts": counts,
        "has_verified_success": counts.get("verified_success", 0) > 0,
        "event_metrics": metrics,
        "codex": {
            "returncode": ((result_payload or {}).get("codex") or {}).get("returncode"),
            "timed_out": ((result_payload or {}).get("codex") or {}).get("timed_out"),
            "watchdog_abort_reason": ((result_payload or {}).get("codex") or {}).get("watchdog_abort_reason"),
            "elapsed_seconds": ((result_payload or {}).get("codex") or {}).get("elapsed_seconds"),
            "watchdog_metrics": ((result_payload or {}).get("codex") or {}).get("watchdog_metrics"),
        },
        "artifact_paths": artifact_paths,
        "artifact_meta": artifact_meta,
    }
    return record


def sort_key(record: dict[str, Any]) -> tuple[str, str]:
    return (str(record.get("started_at") or ""), str(record.get("run_root") or ""))


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter()
    lane_counts = Counter()
    family_counts = Counter()
    verdict_totals = Counter()
    artifact_presence_counts = Counter()
    verified_success_tasks: set[str] = set()
    core_trajectory_missing_run_roots: list[str] = []
    active_runs = 0
    missing_result_runs = 0
    for record in records:
        status_counts[str(record.get("status") or "unknown")] += 1
        if record.get("lane"):
            lane_counts[str(record["lane"])] += 1
        if record.get("task_family"):
            family_counts[str(record["task_family"])] += 1
        if record.get("active_process"):
            active_runs += 1
        if not record.get("has_result"):
            missing_result_runs += 1
        for key in (
            "has_result",
            "has_events",
            "has_prompt",
            "has_summary",
            "has_last_message",
            "has_codex_stderr",
            "has_verify_output",
            "has_core_trajectory",
        ):
            if record.get(key):
                artifact_presence_counts[key] += 1
        if not record.get("has_core_trajectory") and record.get("run_root"):
            core_trajectory_missing_run_roots.append(str(record["run_root"]))
        if record.get("has_verified_success") and record.get("task_id"):
            verified_success_tasks.add(str(record["task_id"]))
        for verdict, count in (record.get("verdict_counts") or {}).items():
            verdict_totals[str(verdict)] += int(count)
    return {
        "generated_at": now_utc().isoformat(),
        "total_runs": len(records),
        "active_runs": active_runs,
        "missing_result_runs": missing_result_runs,
        "completed_result_runs": sum(1 for record in records if record.get("has_result")),
        "verified_success_runs": sum(1 for record in records if record.get("has_verified_success")),
        "verified_success_tasks": len(verified_success_tasks),
        "artifact_presence_counts": dict(sorted(artifact_presence_counts.items())),
        "core_trajectory_complete_runs": sum(1 for record in records if record.get("has_core_trajectory")),
        "core_trajectory_missing_run_roots": core_trajectory_missing_run_roots,
        "status_counts": dict(sorted(status_counts.items())),
        "lane_counts": dict(sorted(lane_counts.items())),
        "task_family_counts": dict(sorted(family_counts.items())),
        "verdict_totals": dict(sorted(verdict_totals.items())),
    }


def build_trajectory_index(results_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    active_run_roots = collect_active_run_roots()
    records = [build_run_record(run_root, results_root=results_root, active_run_roots=active_run_roots) for run_root in collect_run_roots(results_root)]
    records.sort(key=sort_key)
    return records, build_summary(records)


def run_refresh_cycle(args: argparse.Namespace, log_path: Path) -> dict[str, Any]:
    records, summary = build_trajectory_index(args.results_root)
    write_jsonl(args.output_jsonl, records)
    write_json(args.summary_json, summary)
    append_log(
        log_path,
        (
            f"trajectory_index_refreshed total_runs={summary['total_runs']} active_runs={summary['active_runs']} "
            f"verified_success_tasks={summary['verified_success_tasks']} output={args.output_jsonl}"
        ),
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and optionally refresh a unified rescue trajectory index.")
    parser.add_argument("--results-root", type=Path, default=Path("codex_rescue_runs_local"))
    parser.add_argument("--output-jsonl", type=Path, default=Path("codex_rescue_runs_local/trajectory_index.jsonl"))
    parser.add_argument("--summary-json", type=Path, default=Path("codex_rescue_runs_local/trajectory_summary.json"))
    parser.add_argument("--log-path", type=Path, default=Path("codex_rescue_runs_local/trajectory_index_refresh.log"))
    parser.add_argument("--poll-seconds", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_path = args.log_path
    append_log(log_path, "refresh_rescue_trajectory_index started")
    if args.poll_seconds <= 0:
        summary = run_refresh_cycle(args, log_path)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    while True:
        run_refresh_cycle(args, log_path)
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
