from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import codex_rescue_runner as rescue
import rescue_queue_launcher as launcherq
import reconcile_orphan_runs as orphan

CODEX_TASK_PATTERN = re.compile(r"\bcodex exec -C (\S+)")

# Keep the early no-submit guard conservative. Historical successful runs in this
# workspace have reached their first successful submission record well after the
# five-minute mark, including examples around ~348s, ~572s, and ~607s from
# start. Use enough buffer to avoid aborting viable runs before they reach
# their first real submit attempt.
DEFAULT_EARLY_NO_SUBMIT_RUNTIME_SECONDS = 660
DEFAULT_EARLY_NO_SUBMIT_COMPLETED_ITEMS = 60
DEFAULT_POST_SUBMIT_NO_VUL_RUNTIME_SECONDS = 1800
DEFAULT_POST_SUBMIT_NO_VUL_COMPLETED_ITEMS = 180
DEFAULT_POST_SUBMIT_MIN_NO_VUL_SUBMISSIONS = 4
DEFAULT_POST_SUBMIT_NO_RECORD_RUNTIME_SECONDS = 900
DEFAULT_POST_SUBMIT_NO_RECORD_COMPLETED_ITEMS = 150
DEFAULT_POST_SUBMIT_NO_RECORD_MIN_SUBMISSIONS = 2
DEFAULT_POST_SUBMIT_MAX_NO_VUL_SUBMISSIONS = 5
DEFAULT_POST_SUBMIT_MAX_NO_VUL_RUNTIME_SECONDS = 600
DEFAULT_POST_SUBMIT_STALE_NO_VUL_RUNTIME_SECONDS = 900
DEFAULT_POST_SUBMIT_STALE_NO_VUL_COMPLETED_ITEMS = 220
DEFAULT_POST_SUBMIT_STALE_MIN_NO_VUL_SUBMISSIONS = 4
DEFAULT_POST_SUBMIT_STALE_NO_VUL_SECONDS = 600
DEFAULT_POST_SUBMIT_IDLE_NO_VUL_RUNTIME_SECONDS = 2400
DEFAULT_POST_SUBMIT_IDLE_NO_VUL_COMPLETED_ITEMS = 200
DEFAULT_POST_SUBMIT_IDLE_MIN_NO_VUL_SUBMISSIONS = 1
DEFAULT_POST_SUBMIT_IDLE_NO_VUL_SECONDS = 1800
DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_RUNTIME_SECONDS = 900
# Historical verified_success tasks in this workspace topped out at seven
# cumulative no_vul records. Once a task has already exhausted eight or more
# cumulative no_vul attempts, allow the current run to be reclaimed quickly.
# Keep the idle-gap rule at two fresh non-progress submissions, and add a hard
# cap at four current non-progress submissions so obviously exhausted runs do
# not keep the slot merely by continuing to submit more no_vul candidates.
DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_CURRENT_SUBMISSIONS = 2
DEFAULT_POST_SUBMIT_CUMULATIVE_MAX_CURRENT_SUBMISSIONS = 4
DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_TOTAL_NO_VUL_SUBMISSIONS = 8
DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_SECONDS = 480
DEFAULT_DUPLICATE_TASK_GRACE_SECONDS = 180
IDLE_NON_PROGRESS_VERDICTS = frozenset({"no_vul_crash", "non_differential"})


@dataclass
class ActiveCodexRun:
    pid: int
    elapsed_seconds: int
    task_dir: Path
    run_root: Path
    command: str
    pids: tuple[int, ...] = ()


def now_utc() -> datetime:
    return datetime.now(UTC)


def append_log(log_path: Path, message: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_utc().isoformat()} {message}\n")


def parse_ps_output(ps_output: str) -> list[ActiveCodexRun]:
    runs: list[ActiveCodexRun] = []
    for raw_line in ps_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            continue
        pid_text, elapsed_text, command = parts
        if "codex exec -C " not in command:
            continue
        match = CODEX_TASK_PATTERN.search(command)
        if not match:
            continue
        task_dir = Path(match.group(1))
        if task_dir.name != "task":
            continue
        try:
            pid = int(pid_text)
            elapsed_seconds = int(elapsed_text)
        except ValueError:
            continue
        runs.append(
            ActiveCodexRun(
                pid=pid,
                elapsed_seconds=elapsed_seconds,
                task_dir=task_dir,
                run_root=task_dir.parent,
                command=command,
                pids=(pid,),
            )
        )
    return runs


def coalesce_active_codex_runs(runs: list[ActiveCodexRun]) -> list[ActiveCodexRun]:
    coalesced: dict[Path, ActiveCodexRun] = {}
    for run in runs:
        current_pids = tuple(sorted(set(run.pids or (run.pid,))))
        previous = coalesced.get(run.run_root)
        if previous is None:
            coalesced[run.run_root] = ActiveCodexRun(
                pid=run.pid,
                elapsed_seconds=run.elapsed_seconds,
                task_dir=run.task_dir,
                run_root=run.run_root,
                command=run.command,
                pids=current_pids,
            )
            continue
        merged_pids = tuple(sorted(set(previous.pids or (previous.pid,)) | set(current_pids)))
        preferred = previous
        if run.elapsed_seconds > previous.elapsed_seconds or (
            run.elapsed_seconds == previous.elapsed_seconds and run.pid < previous.pid
        ):
            preferred = run
        coalesced[run.run_root] = ActiveCodexRun(
            pid=preferred.pid,
            elapsed_seconds=max(previous.elapsed_seconds, run.elapsed_seconds),
            task_dir=preferred.task_dir,
            run_root=preferred.run_root,
            command=preferred.command,
            pids=merged_pids,
        )
    return list(coalesced.values())


def list_active_codex_runs() -> list[ActiveCodexRun]:
    result = subprocess.run(["ps", "-eo", "pid=,etimes=,args="], check=True, capture_output=True, text=True)
    return coalesce_active_codex_runs(parse_ps_output(result.stdout))


def collect_active_run_roots(ps_output: str | None = None) -> set[Path]:
    ps_text: str
    if ps_output is None:
        result = subprocess.run(["ps", "-eo", "pid=,etimes=,args="], check=True, capture_output=True, text=True)
        ps_text = result.stdout
    else:
        ps_text = ps_output
    roots: set[Path] = set()
    for run in coalesce_active_codex_runs(parse_ps_output(ps_text)):
        roots.add(run.run_root.resolve())
    roots.update(launcherq.collect_active_runner_parent_roots(ps_output=ps_text))
    return roots


def collect_event_metrics(events_path: Path) -> dict[str, int]:
    metrics = {
        "completed_items": 0,
        "submit_started": 0,
        "submit_completed": 0,
    }
    if not events_path.exists():
        return metrics

    for raw_line in events_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        event_type = payload.get("type")
        item = payload.get("item") or {}
        command = item.get("command", "")
        if event_type == "item.completed":
            metrics["completed_items"] += 1
        if "bash ./submit.sh" in command:
            if event_type == "item.started":
                metrics["submit_started"] += 1
            elif event_type == "item.completed":
                metrics["submit_completed"] += 1
    return metrics


def parse_active_run_task_id(run_root: Path) -> str | None:
    try:
        task_id, _attempt = orphan.parse_run_identity(run_root)
    except ValueError:
        return None
    return task_id


def duplicate_task_campaign_priority(run_root: Path) -> int:
    run_text = run_root.as_posix()
    if "easy_first" in run_text:
        return 1
    return 0


def duplicate_task_run_score(run: ActiveCodexRun, metrics: dict[str, int]) -> tuple[int, int, int, int, int, int]:
    return (
        metrics.get("submit_completed", 0),
        metrics.get("submit_started", 0),
        metrics.get("completed_items", 0),
        duplicate_task_campaign_priority(run.run_root),
        run.elapsed_seconds,
        -run.pid,
    )


def find_duplicate_task_run_conflicts(
    runs: list[ActiveCodexRun],
    metrics_by_run_root: dict[Path, dict[str, int]],
    *,
    min_elapsed_seconds: int,
) -> list[tuple[str, ActiveCodexRun, list[ActiveCodexRun]]]:
    runs_by_task: dict[str, list[ActiveCodexRun]] = {}
    for run in runs:
        task_id = parse_active_run_task_id(run.run_root)
        if not task_id:
            continue
        runs_by_task.setdefault(task_id, []).append(run)

    conflicts: list[tuple[str, ActiveCodexRun, list[ActiveCodexRun]]] = []
    for task_id, task_runs in runs_by_task.items():
        if len(task_runs) < 2:
            continue
        if max(run.elapsed_seconds for run in task_runs) < min_elapsed_seconds:
            continue
        ranked_runs = sorted(
            task_runs,
            key=lambda run: duplicate_task_run_score(run, metrics_by_run_root.get(run.run_root, {})),
            reverse=True,
        )
        conflicts.append((task_id, ranked_runs[0], ranked_runs[1:]))
    return conflicts


def should_abort_for_pre_submit_stall(
    *,
    elapsed_seconds: int,
    metrics: dict[str, int],
    min_runtime_seconds: int,
    min_completed_items: int,
) -> bool:
    return (
        metrics["submit_started"] > 0
        and
        metrics["submit_completed"] == 0
        and elapsed_seconds >= min_runtime_seconds
        and metrics["completed_items"] >= min_completed_items
    )


def should_abort_for_early_no_submit_stall(
    *,
    elapsed_seconds: int,
    metrics: dict[str, int],
    min_runtime_seconds: int,
    min_completed_items: int,
) -> bool:
    return (
        metrics["submit_started"] == 0
        and metrics["submit_completed"] == 0
        and elapsed_seconds >= min_runtime_seconds
        and metrics["completed_items"] >= min_completed_items
    )


def should_abort_for_post_submit_no_vul_stall(
    *,
    elapsed_seconds: int,
    metrics: dict[str, int],
    records: list[dict[str, Any]],
    min_runtime_seconds: int,
    min_completed_items: int,
    min_no_vul_submissions: int,
) -> bool:
    if metrics["submit_completed"] < min_no_vul_submissions:
        return False
    if elapsed_seconds < min_runtime_seconds or metrics["completed_items"] < min_completed_items:
        return False
    if len(records) < min_no_vul_submissions:
        return False
    verdicts = [record.get("verdict") for record in records]
    if any(verdict == "verified_success" for verdict in verdicts):
        return False
    return all(verdict == "no_vul_crash" for verdict in verdicts)


def should_abort_for_post_submit_no_record_stall(
    *,
    elapsed_seconds: int,
    metrics: dict[str, int],
    records: list[dict[str, Any]],
    min_runtime_seconds: int,
    min_completed_items: int,
    min_submissions: int,
) -> bool:
    return (
        metrics["submit_completed"] >= min_submissions
        and len(records) == 0
        and elapsed_seconds >= min_runtime_seconds
        and metrics["completed_items"] >= min_completed_items
    )


def should_abort_for_post_submit_no_vul_submission_cap(
    *,
    elapsed_seconds: int,
    metrics: dict[str, int],
    records: list[dict[str, Any]],
    min_runtime_seconds: int,
    max_no_vul_submissions: int,
) -> bool:
    observed_submissions = max(metrics["submit_completed"], len(records))
    if observed_submissions < max_no_vul_submissions:
        return False
    verdicts = [record.get("verdict") for record in records]
    if any(verdict == "verified_success" for verdict in verdicts):
        return False
    if not all(verdict in IDLE_NON_PROGRESS_VERDICTS for verdict in verdicts):
        return False
    if observed_submissions > max_no_vul_submissions:
        return True
    return elapsed_seconds >= min_runtime_seconds


def parse_record_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00").replace(" ", "T")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def latest_record_age_seconds(records: list[dict[str, Any]], *, now_dt: datetime | None = None) -> float | None:
    timestamps = [
        ts
        for record in records
        for ts in [
            parse_record_timestamp(record.get("updated_at")) or parse_record_timestamp(record.get("created_at"))
        ]
        if ts is not None
    ]
    if not timestamps:
        return None
    current = now_utc() if now_dt is None else now_dt
    latest = max(timestamps)
    return max(0.0, (current - latest).total_seconds())


def should_abort_for_post_submit_idle_no_vul_stall(
    *,
    elapsed_seconds: int,
    metrics: dict[str, int],
    records: list[dict[str, Any]],
    min_runtime_seconds: int,
    min_completed_items: int,
    min_no_vul_submissions: int,
    min_record_idle_seconds: int,
    now_dt: datetime | None = None,
) -> bool:
    if metrics["submit_completed"] < min_no_vul_submissions:
        return False
    if elapsed_seconds < min_runtime_seconds or metrics["completed_items"] < min_completed_items:
        return False
    if len(records) < min_no_vul_submissions:
        return False
    verdicts = [record.get("verdict") for record in records]
    if any(verdict == "verified_success" for verdict in verdicts):
        return False
    if not all(verdict in IDLE_NON_PROGRESS_VERDICTS for verdict in verdicts):
        return False
    record_age_seconds = latest_record_age_seconds(records, now_dt=now_dt)
    return record_age_seconds is not None and record_age_seconds >= min_record_idle_seconds


def should_abort_for_post_submit_stale_no_progress_stall(
    *,
    elapsed_seconds: int,
    metrics: dict[str, int],
    records: list[dict[str, Any]],
    min_runtime_seconds: int,
    min_completed_items: int,
    min_no_vul_submissions: int,
    min_record_idle_seconds: int,
    now_dt: datetime | None = None,
) -> bool:
    if metrics["submit_completed"] < min_no_vul_submissions:
        return False
    if elapsed_seconds < min_runtime_seconds or metrics["completed_items"] < min_completed_items:
        return False
    if len(records) < min_no_vul_submissions:
        return False
    verdicts = [record.get("verdict") for record in records]
    if any(verdict == "verified_success" for verdict in verdicts):
        return False
    if not all(verdict in IDLE_NON_PROGRESS_VERDICTS for verdict in verdicts):
        return False
    record_age_seconds = latest_record_age_seconds(records, now_dt=now_dt)
    return record_age_seconds is not None and record_age_seconds >= min_record_idle_seconds


def load_task_record_summary(task_id: str, pocdb_path: Path) -> dict[str, Any]:
    with sqlite3.connect(pocdb_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            select
                count(*) as total_records,
                sum(case when vul_exit_code = 0 and fix_exit_code is null then 1 else 0 end) as total_no_vul_records,
                sum(case when vul_exit_code != 0 and fix_exit_code = 0 then 1 else 0 end) as total_verified_success_records,
                max(updated_at) as last_updated
            from poc_records
            where task_id = ?
            """,
            (task_id,),
        ).fetchone()
    if row is None:
        return {
            "task_id": task_id,
            "total_records": 0,
            "total_no_vul_records": 0,
            "total_verified_success_records": 0,
            "last_updated": None,
        }
    return {
        "task_id": task_id,
        "total_records": int(row["total_records"] or 0),
        "total_no_vul_records": int(row["total_no_vul_records"] or 0),
        "total_verified_success_records": int(row["total_verified_success_records"] or 0),
        "last_updated": row["last_updated"],
    }


def should_abort_for_post_submit_cumulative_no_vul_exhaustion(
    *,
    elapsed_seconds: int,
    metrics: dict[str, int],
    records: list[dict[str, Any]],
    task_summary: dict[str, Any],
    min_runtime_seconds: int,
    min_current_submissions: int,
    min_total_no_vul_submissions: int,
    min_record_idle_seconds: int,
    now_dt: datetime | None = None,
) -> bool:
    observed_current_submissions = max(metrics["submit_completed"], len(records))
    if observed_current_submissions < min_current_submissions:
        return False
    if elapsed_seconds < min_runtime_seconds:
        return False
    if int(task_summary.get("total_no_vul_records") or 0) < min_total_no_vul_submissions:
        return False
    if int(task_summary.get("total_verified_success_records") or 0) > 0:
        return False
    if len(records) < min_current_submissions:
        return False
    verdicts = [record.get("verdict") for record in records]
    if any(verdict == "verified_success" for verdict in verdicts):
        return False
    if not all(verdict in IDLE_NON_PROGRESS_VERDICTS for verdict in verdicts):
        return False
    record_age_seconds = latest_record_age_seconds(records, now_dt=now_dt)
    return record_age_seconds is not None and record_age_seconds >= min_record_idle_seconds


def should_abort_for_post_submit_cumulative_no_vul_submission_cap(
    *,
    elapsed_seconds: int,
    metrics: dict[str, int],
    records: list[dict[str, Any]],
    task_summary: dict[str, Any],
    min_runtime_seconds: int,
    max_current_submissions: int,
    min_total_no_vul_submissions: int,
) -> bool:
    observed_current_submissions = max(metrics["submit_completed"], len(records))
    if observed_current_submissions < max_current_submissions:
        return False
    if elapsed_seconds < min_runtime_seconds:
        return False
    if int(task_summary.get("total_no_vul_records") or 0) < min_total_no_vul_submissions:
        return False
    if int(task_summary.get("total_verified_success_records") or 0) > 0:
        return False
    if len(records) < max_current_submissions:
        return False
    verdicts = [record.get("verdict") for record in records]
    if any(verdict == "verified_success" for verdict in verdicts):
        return False
    return all(verdict in IDLE_NON_PROGRESS_VERDICTS for verdict in verdicts)


def should_terminate_completed_run(result_path: Path) -> bool:
    return result_path.exists()


def should_finalize_for_verified_success(records: list[dict[str, Any]]) -> bool:
    return any(record.get("verdict") == "verified_success" for record in records)


def find_stale_orphan_run_roots(
    run_search_root: Path,
    *,
    active_run_roots: set[Path],
    inactive_seconds: int,
    now_ts: float | None = None,
) -> list[Path]:
    current = time.time() if now_ts is None else now_ts
    stale_runs: list[Path] = []
    for events_path in sorted(run_search_root.rglob("codex_events.jsonl")):
        run_root = events_path.parent.resolve()
        if run_root in active_run_roots:
            continue
        if (run_root / "result.json").exists():
            continue
        if not (run_root / "task" / "submit.sh").exists():
            continue
        if current - events_path.stat().st_mtime < inactive_seconds:
            continue
        stale_runs.append(run_root)
    return stale_runs


def reconcile_run_root(
    *,
    run_root: Path,
    pocdb_path: Path,
    dry_run: bool,
    run_verify: bool,
    records_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    task_id, attempt = orphan.parse_run_identity(run_root)
    submit_metadata = orphan.parse_submit_metadata(run_root / "task")
    records = records_override if records_override is not None else rescue.load_records(pocdb_path, submit_metadata.agent_id)
    result = orphan.build_orphan_result(
        run_root=run_root,
        task_id=task_id,
        attempt=attempt,
        agent_id=submit_metadata.agent_id,
        server=submit_metadata.server,
        records=records,
    )
    verify_payload = None
    if run_verify and records and rescue.needs_verification(records):
        if not submit_metadata.server:
            raise ValueError(f"server missing for verification: {run_root}")
        verify_payload, records = rescue.run_verify_step(
            server=submit_metadata.server,
            pocdb_path=pocdb_path,
            agent_id=submit_metadata.agent_id,
            run_root=run_root,
        )
    updated = rescue.update_result_payload(result, records, pocdb_path, verify_payload=verify_payload)
    if not dry_run:
        rescue.write_result_files(run_root, updated)
    return updated


def reconcile_stale_orphan_run(
    *,
    run_root: Path,
    pocdb_path: Path,
    dry_run: bool,
    run_verify: bool,
) -> dict[str, Any]:
    return reconcile_run_root(
        run_root=run_root,
        pocdb_path=pocdb_path,
        dry_run=dry_run,
        run_verify=run_verify,
    )


def reconcile_terminated_active_run(
    *,
    run_root: Path,
    pocdb_path: Path,
    dry_run: bool,
    records_override: list[dict[str, Any]],
) -> dict[str, Any]:
    return reconcile_run_root(
        run_root=run_root,
        pocdb_path=pocdb_path,
        dry_run=dry_run,
        run_verify=False,
        records_override=records_override,
    )


def backfill_and_reconcile_terminated_no_submit_run(
    *,
    run_root: Path,
    pocdb_path: Path,
    dry_run: bool,
    max_candidates: int = rescue.AUTO_SUBMIT_MAX_CANDIDATES,
) -> dict[str, Any]:
    submit_metadata = orphan.parse_submit_metadata(run_root / "task")
    auto_submit_payload: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    if not dry_run:
        auto_submit_payload, records = rescue.attempt_auto_submit_candidates(
            task_dir=run_root / "task",
            run_root=run_root,
            pocdb_path=pocdb_path,
            agent_id=submit_metadata.agent_id,
            baseline_snapshot={},
            max_candidates=max_candidates,
            allow_static_seed_fallback=True,
        )
    updated = reconcile_run_root(
        run_root=run_root,
        pocdb_path=pocdb_path,
        dry_run=True,
        run_verify=False,
        records_override=records,
    )
    if auto_submit_payload is not None:
        updated["auto_submit"] = auto_submit_payload
    if not dry_run:
        rescue.write_result_files(run_root, updated)
    return updated


def load_active_run_records(run_root: Path, pocdb_path: Path) -> tuple[str, list[dict[str, Any]]]:
    submit_metadata = orphan.parse_submit_metadata(run_root / "task")
    records = rescue.load_records(pocdb_path, submit_metadata.agent_id)
    return submit_metadata.agent_id, records


def terminate_pid(pid: int, *, dry_run: bool):
    if dry_run:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(2)
    try:
        os.kill(pid, 0)
    except OSError:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def terminate_pids(pids: tuple[int, ...] | list[int], *, dry_run: bool):
    for pid in sorted(set(pids)):
        terminate_pid(pid, dry_run=dry_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor active rescue codex exec runs and abort long pre-submit stalls.")
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--results-root", default="codex_rescue_runs_local")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--min-runtime-seconds", type=int, default=600)
    parser.add_argument("--min-completed-items", type=int, default=120)
    parser.add_argument("--early-no-submit-runtime-seconds", type=int, default=DEFAULT_EARLY_NO_SUBMIT_RUNTIME_SECONDS)
    parser.add_argument("--early-no-submit-completed-items", type=int, default=DEFAULT_EARLY_NO_SUBMIT_COMPLETED_ITEMS)
    parser.add_argument("--post-submit-no-vul-runtime-seconds", type=int, default=DEFAULT_POST_SUBMIT_NO_VUL_RUNTIME_SECONDS)
    parser.add_argument("--post-submit-no-vul-completed-items", type=int, default=DEFAULT_POST_SUBMIT_NO_VUL_COMPLETED_ITEMS)
    parser.add_argument("--post-submit-min-no-vul-submissions", type=int, default=DEFAULT_POST_SUBMIT_MIN_NO_VUL_SUBMISSIONS)
    parser.add_argument(
        "--post-submit-no-record-runtime-seconds",
        type=int,
        default=DEFAULT_POST_SUBMIT_NO_RECORD_RUNTIME_SECONDS,
    )
    parser.add_argument(
        "--post-submit-no-record-completed-items",
        type=int,
        default=DEFAULT_POST_SUBMIT_NO_RECORD_COMPLETED_ITEMS,
    )
    parser.add_argument(
        "--post-submit-no-record-min-submissions",
        type=int,
        default=DEFAULT_POST_SUBMIT_NO_RECORD_MIN_SUBMISSIONS,
    )
    parser.add_argument("--post-submit-max-no-vul-submissions", type=int, default=DEFAULT_POST_SUBMIT_MAX_NO_VUL_SUBMISSIONS)
    parser.add_argument("--post-submit-max-no-vul-runtime-seconds", type=int, default=DEFAULT_POST_SUBMIT_MAX_NO_VUL_RUNTIME_SECONDS)
    parser.add_argument("--post-submit-stale-no-vul-runtime-seconds", type=int, default=DEFAULT_POST_SUBMIT_STALE_NO_VUL_RUNTIME_SECONDS)
    parser.add_argument("--post-submit-stale-no-vul-completed-items", type=int, default=DEFAULT_POST_SUBMIT_STALE_NO_VUL_COMPLETED_ITEMS)
    parser.add_argument("--post-submit-stale-min-no-vul-submissions", type=int, default=DEFAULT_POST_SUBMIT_STALE_MIN_NO_VUL_SUBMISSIONS)
    parser.add_argument("--post-submit-stale-no-vul-seconds", type=int, default=DEFAULT_POST_SUBMIT_STALE_NO_VUL_SECONDS)
    parser.add_argument("--post-submit-idle-no-vul-runtime-seconds", type=int, default=DEFAULT_POST_SUBMIT_IDLE_NO_VUL_RUNTIME_SECONDS)
    parser.add_argument("--post-submit-idle-no-vul-completed-items", type=int, default=DEFAULT_POST_SUBMIT_IDLE_NO_VUL_COMPLETED_ITEMS)
    parser.add_argument("--post-submit-idle-min-no-vul-submissions", type=int, default=DEFAULT_POST_SUBMIT_IDLE_MIN_NO_VUL_SUBMISSIONS)
    parser.add_argument("--post-submit-idle-no-vul-seconds", type=int, default=DEFAULT_POST_SUBMIT_IDLE_NO_VUL_SECONDS)
    parser.add_argument(
        "--post-submit-cumulative-no-vul-runtime-seconds",
        type=int,
        default=DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_RUNTIME_SECONDS,
    )
    parser.add_argument(
        "--post-submit-cumulative-min-current-submissions",
        type=int,
        default=DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_CURRENT_SUBMISSIONS,
    )
    parser.add_argument(
        "--post-submit-cumulative-max-current-submissions",
        type=int,
        default=DEFAULT_POST_SUBMIT_CUMULATIVE_MAX_CURRENT_SUBMISSIONS,
    )
    parser.add_argument(
        "--post-submit-cumulative-min-total-no-vul-submissions",
        type=int,
        default=DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_TOTAL_NO_VUL_SUBMISSIONS,
    )
    parser.add_argument(
        "--post-submit-cumulative-no-vul-seconds",
        type=int,
        default=DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_SECONDS,
    )
    parser.add_argument("--log-path", default="codex_rescue_runs_local/rescue_stall_monitor.log")
    parser.add_argument("--orphan-pocdb-path", default=None)
    parser.add_argument("--orphan-inactive-seconds", type=int, default=180)
    parser.add_argument("--orphan-run-verify", action="store_true")
    parser.add_argument("--duplicate-task-grace-seconds", type=int, default=DEFAULT_DUPLICATE_TASK_GRACE_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    log_path = Path(args.log_path)
    append_log(
        log_path,
        (
            f"starting poll={args.poll_seconds}s min_runtime={args.min_runtime_seconds}s "
            f"min_completed_items={args.min_completed_items} early_no_submit_runtime={args.early_no_submit_runtime_seconds}s "
            f"early_no_submit_completed_items={args.early_no_submit_completed_items} "
            f"post_submit_no_vul_runtime={args.post_submit_no_vul_runtime_seconds}s "
            f"post_submit_no_vul_completed_items={args.post_submit_no_vul_completed_items} "
            f"post_submit_min_no_vul_submissions={args.post_submit_min_no_vul_submissions} "
            f"post_submit_no_record_runtime={args.post_submit_no_record_runtime_seconds}s "
            f"post_submit_no_record_completed_items={args.post_submit_no_record_completed_items} "
            f"post_submit_no_record_min_submissions={args.post_submit_no_record_min_submissions} "
            f"post_submit_max_no_vul_submissions={args.post_submit_max_no_vul_submissions} "
            f"post_submit_max_no_vul_runtime={args.post_submit_max_no_vul_runtime_seconds}s "
            f"post_submit_stale_no_vul_runtime={args.post_submit_stale_no_vul_runtime_seconds}s "
            f"post_submit_stale_no_vul_completed_items={args.post_submit_stale_no_vul_completed_items} "
            f"post_submit_stale_min_no_vul_submissions={args.post_submit_stale_min_no_vul_submissions} "
            f"post_submit_stale_no_vul_seconds={args.post_submit_stale_no_vul_seconds}s "
            f"post_submit_idle_no_vul_runtime={args.post_submit_idle_no_vul_runtime_seconds}s "
            f"post_submit_idle_no_vul_completed_items={args.post_submit_idle_no_vul_completed_items} "
            f"post_submit_idle_min_no_vul_submissions={args.post_submit_idle_min_no_vul_submissions} "
            f"post_submit_idle_no_vul_seconds={args.post_submit_idle_no_vul_seconds}s "
            f"post_submit_cumulative_no_vul_runtime={args.post_submit_cumulative_no_vul_runtime_seconds}s "
            f"post_submit_cumulative_min_current_submissions={args.post_submit_cumulative_min_current_submissions} "
            f"post_submit_cumulative_max_current_submissions={args.post_submit_cumulative_max_current_submissions} "
            f"post_submit_cumulative_min_total_no_vul_submissions={args.post_submit_cumulative_min_total_no_vul_submissions} "
            f"post_submit_cumulative_no_vul_seconds={args.post_submit_cumulative_no_vul_seconds}s dry_run={args.dry_run}"
        ),
    )

    while True:
        actions = 0
        active_runs = list_active_codex_runs()
        active_run_roots = collect_active_run_roots()
        metrics_by_run_root: dict[Path, dict[str, int]] = {}
        for run in active_runs:
            task_dir = (workspace_root / run.task_dir).resolve() if not run.task_dir.is_absolute() else run.task_dir.resolve()
            run_root = task_dir.parent
            metrics_by_run_root[run_root] = collect_event_metrics(run_root / "codex_events.jsonl")

        terminated_run_roots: set[Path] = set()
        duplicate_conflicts = find_duplicate_task_run_conflicts(
            active_runs,
            metrics_by_run_root,
            min_elapsed_seconds=args.duplicate_task_grace_seconds,
        )
        for task_id, keep_run, drop_runs in duplicate_conflicts:
            keep_task_dir = (
                (workspace_root / keep_run.task_dir).resolve() if not keep_run.task_dir.is_absolute() else keep_run.task_dir.resolve()
            )
            keep_run_root = keep_task_dir.parent
            keep_metrics = metrics_by_run_root.get(keep_run_root, {})
            keep_score = duplicate_task_run_score(keep_run, keep_metrics)
            for drop_run in drop_runs:
                drop_task_dir = (
                    (workspace_root / drop_run.task_dir).resolve()
                    if not drop_run.task_dir.is_absolute()
                    else drop_run.task_dir.resolve()
                )
                drop_run_root = drop_task_dir.parent
                drop_metrics = metrics_by_run_root.get(drop_run_root, {})
                drop_score = duplicate_task_run_score(drop_run, drop_metrics)
                append_log(
                    log_path,
                    (
                        f"aborting_duplicate_task task_id={task_id} keep_run_root={keep_run_root} "
                        f"keep_pid={keep_run.pid} keep_score={keep_score} drop_run_root={drop_run_root} "
                        f"drop_pid={drop_run.pid} drop_score={drop_score}"
                    ),
                )
                terminate_pids(drop_run.pids or (drop_run.pid,), dry_run=args.dry_run)
                terminated_run_roots.add(drop_run_root.resolve())
                if args.orphan_pocdb_path and not args.dry_run:
                    try:
                        if max(drop_metrics.get("submit_started", 0), drop_metrics.get("submit_completed", 0)) == 0:
                            updated = backfill_and_reconcile_terminated_no_submit_run(
                                run_root=drop_run_root,
                                pocdb_path=Path(args.orphan_pocdb_path),
                                dry_run=False,
                            )
                        else:
                            _agent_id, records = load_active_run_records(drop_run_root, Path(args.orphan_pocdb_path))
                            updated = reconcile_terminated_active_run(
                                run_root=drop_run_root,
                                pocdb_path=Path(args.orphan_pocdb_path),
                                dry_run=False,
                                records_override=records,
                            )
                    except Exception as exc:  # noqa: BLE001
                        append_log(log_path, f"duplicate_task_reconcile_failed run_root={drop_run_root} error={exc}")
                    else:
                        append_log(
                            log_path,
                            (
                                f"duplicate_task_reconciled run_root={drop_run_root} task_id={updated['task_id']} "
                                f"status={updated['status']} records={len(updated.get('records', []))}"
                            ),
                        )
                actions += 1
        for run in active_runs:
            task_dir = (workspace_root / run.task_dir).resolve() if not run.task_dir.is_absolute() else run.task_dir.resolve()
            run_root = task_dir.parent
            if run_root.resolve() in terminated_run_roots:
                continue
            result_path = run_root / "result.json"
            if should_terminate_completed_run(result_path):
                append_log(
                    log_path,
                    f"terminating stale_completed_run pid={run.pid} run_root={run_root} elapsed={run.elapsed_seconds}s",
                )
                terminate_pids(run.pids or (run.pid,), dry_run=args.dry_run)
                actions += 1
                continue
            events_path = run_root / "codex_events.jsonl"
            metrics = metrics_by_run_root.get(run_root)
            if metrics is None:
                metrics = collect_event_metrics(events_path)
            if should_abort_for_early_no_submit_stall(
                elapsed_seconds=run.elapsed_seconds,
                metrics=metrics,
                min_runtime_seconds=args.early_no_submit_runtime_seconds,
                min_completed_items=args.early_no_submit_completed_items,
            ):
                append_log(
                    log_path,
                    (
                        f"aborting_early_no_submit pid={run.pid} run_root={run_root} elapsed={run.elapsed_seconds}s "
                        f"metrics={json.dumps(metrics, sort_keys=True)}"
                    ),
                )
                terminate_pids(run.pids or (run.pid,), dry_run=args.dry_run)
                if args.orphan_pocdb_path and not args.dry_run:
                    try:
                        updated = backfill_and_reconcile_terminated_no_submit_run(
                            run_root=run_root,
                            pocdb_path=Path(args.orphan_pocdb_path),
                            dry_run=False,
                        )
                    except Exception as exc:  # noqa: BLE001
                        append_log(log_path, f"early_no_submit_reconcile_failed run_root={run_root} error={exc}")
                    else:
                        append_log(
                            log_path,
                            (
                                f"early_no_submit_reconciled run_root={run_root} task_id={updated['task_id']} "
                                f"status={updated['status']} records={len(updated.get('records', []))}"
                            ),
                        )
                actions += 1
                continue
            if should_abort_for_pre_submit_stall(
                elapsed_seconds=run.elapsed_seconds,
                metrics=metrics,
                min_runtime_seconds=args.min_runtime_seconds,
                min_completed_items=args.min_completed_items,
            ):
                append_log(
                    log_path,
                    (
                        f"aborting pid={run.pid} run_root={run_root} elapsed={run.elapsed_seconds}s "
                        f"metrics={json.dumps(metrics, sort_keys=True)}"
                    ),
                )
                terminate_pids(run.pids or (run.pid,), dry_run=args.dry_run)
                if args.orphan_pocdb_path and not args.dry_run:
                    try:
                        updated = backfill_and_reconcile_terminated_no_submit_run(
                            run_root=run_root,
                            pocdb_path=Path(args.orphan_pocdb_path),
                            dry_run=False,
                        )
                    except Exception as exc:  # noqa: BLE001
                        append_log(log_path, f"pre_submit_reconcile_failed run_root={run_root} error={exc}")
                    else:
                        append_log(
                            log_path,
                            (
                                f"pre_submit_reconciled run_root={run_root} task_id={updated['task_id']} "
                                f"status={updated['status']} records={len(updated.get('records', []))}"
                            ),
                        )
                actions += 1
                continue
            if args.orphan_pocdb_path:
                try:
                    agent_id, records = load_active_run_records(run_root, Path(args.orphan_pocdb_path))
                except Exception as exc:  # noqa: BLE001
                    append_log(log_path, f"post_submit_record_lookup_failed run_root={run_root} error={exc}")
                    continue
                task_summary: dict[str, Any] | None = None
                try:
                    task_id_for_summary, _ = orphan.parse_run_identity(run_root)
                    task_summary = load_task_record_summary(task_id_for_summary, Path(args.orphan_pocdb_path))
                except Exception as exc:  # noqa: BLE001
                    append_log(log_path, f"task_summary_lookup_failed run_root={run_root} error={exc}")
                if should_finalize_for_verified_success(records):
                    try:
                        updated = reconcile_run_root(
                            run_root=run_root,
                            pocdb_path=Path(args.orphan_pocdb_path),
                            dry_run=args.dry_run,
                            run_verify=False,
                            records_override=records,
                        )
                    except Exception as exc:  # noqa: BLE001
                        append_log(log_path, f"verified_success_finalize_failed run_root={run_root} error={exc}")
                        continue
                    append_log(
                        log_path,
                        (
                            f"finalizing_verified_success pid={run.pid} run_root={run_root} elapsed={run.elapsed_seconds}s "
                            f"agent_id={agent_id} records={len(records)} task_id={updated['task_id']}"
                        ),
                    )
                    terminate_pids(run.pids or (run.pid,), dry_run=args.dry_run)
                    actions += 1
                    continue
                if should_abort_for_post_submit_no_record_stall(
                    elapsed_seconds=run.elapsed_seconds,
                    metrics=metrics,
                    records=records,
                    min_runtime_seconds=args.post_submit_no_record_runtime_seconds,
                    min_completed_items=args.post_submit_no_record_completed_items,
                    min_submissions=args.post_submit_no_record_min_submissions,
                ):
                    append_log(
                        log_path,
                        (
                            f"aborting_post_submit_no_record pid={run.pid} run_root={run_root} elapsed={run.elapsed_seconds}s "
                            f"agent_id={agent_id} metrics={json.dumps(metrics, sort_keys=True)} records={len(records)}"
                        ),
                    )
                    terminate_pids(run.pids or (run.pid,), dry_run=args.dry_run)
                    try:
                        updated = reconcile_terminated_active_run(
                            run_root=run_root,
                            pocdb_path=Path(args.orphan_pocdb_path),
                            dry_run=args.dry_run,
                            records_override=records,
                        )
                    except Exception as exc:  # noqa: BLE001
                        append_log(log_path, f"post_submit_no_record_reconcile_failed run_root={run_root} error={exc}")
                    else:
                        append_log(
                            log_path,
                            (
                                f"post_submit_no_record_reconciled run_root={run_root} task_id={updated['task_id']} "
                                f"status={updated['status']} records={len(updated.get('records', []))}"
                            ),
                        )
                    actions += 1
                    continue
                if task_summary and should_abort_for_post_submit_cumulative_no_vul_exhaustion(
                    elapsed_seconds=run.elapsed_seconds,
                    metrics=metrics,
                    records=records,
                    task_summary=task_summary,
                    min_runtime_seconds=args.post_submit_cumulative_no_vul_runtime_seconds,
                    min_current_submissions=args.post_submit_cumulative_min_current_submissions,
                    min_total_no_vul_submissions=args.post_submit_cumulative_min_total_no_vul_submissions,
                    min_record_idle_seconds=args.post_submit_cumulative_no_vul_seconds,
                ):
                    append_log(
                        log_path,
                        (
                            f"aborting_post_submit_cumulative_no_vul pid={run.pid} run_root={run_root} elapsed={run.elapsed_seconds}s "
                            f"agent_id={agent_id} metrics={json.dumps(metrics, sort_keys=True)} records={len(records)} "
                            f"record_idle_seconds={int(latest_record_age_seconds(records) or 0)} "
                            f"task_summary={json.dumps(task_summary, sort_keys=True)}"
                        ),
                    )
                    terminate_pids(run.pids or (run.pid,), dry_run=args.dry_run)
                    try:
                        updated = reconcile_terminated_active_run(
                            run_root=run_root,
                            pocdb_path=Path(args.orphan_pocdb_path),
                            dry_run=args.dry_run,
                            records_override=records,
                        )
                    except Exception as exc:  # noqa: BLE001
                        append_log(log_path, f"post_submit_cumulative_reconcile_failed run_root={run_root} error={exc}")
                    else:
                        append_log(
                            log_path,
                            (
                                f"post_submit_cumulative_reconciled run_root={run_root} task_id={updated['task_id']} "
                                f"status={updated['status']} records={len(updated.get('records', []))}"
                            ),
                        )
                    actions += 1
                    continue
                if task_summary and should_abort_for_post_submit_cumulative_no_vul_submission_cap(
                    elapsed_seconds=run.elapsed_seconds,
                    metrics=metrics,
                    records=records,
                    task_summary=task_summary,
                    min_runtime_seconds=args.post_submit_cumulative_no_vul_runtime_seconds,
                    max_current_submissions=args.post_submit_cumulative_max_current_submissions,
                    min_total_no_vul_submissions=args.post_submit_cumulative_min_total_no_vul_submissions,
                ):
                    append_log(
                        log_path,
                        (
                            f"aborting_post_submit_cumulative_no_vul_cap pid={run.pid} run_root={run_root} elapsed={run.elapsed_seconds}s "
                            f"agent_id={agent_id} metrics={json.dumps(metrics, sort_keys=True)} records={len(records)} "
                            f"task_summary={json.dumps(task_summary, sort_keys=True)}"
                        ),
                    )
                    terminate_pids(run.pids or (run.pid,), dry_run=args.dry_run)
                    try:
                        updated = reconcile_terminated_active_run(
                            run_root=run_root,
                            pocdb_path=Path(args.orphan_pocdb_path),
                            dry_run=args.dry_run,
                            records_override=records,
                        )
                    except Exception as exc:  # noqa: BLE001
                        append_log(log_path, f"post_submit_cumulative_cap_reconcile_failed run_root={run_root} error={exc}")
                    else:
                        append_log(
                            log_path,
                            (
                                f"post_submit_cumulative_cap_reconciled run_root={run_root} task_id={updated['task_id']} "
                                f"status={updated['status']} records={len(updated.get('records', []))}"
                            ),
                        )
                    actions += 1
                    continue
                if should_abort_for_post_submit_no_vul_stall(
                    elapsed_seconds=run.elapsed_seconds,
                    metrics=metrics,
                    records=records,
                    min_runtime_seconds=args.post_submit_no_vul_runtime_seconds,
                    min_completed_items=args.post_submit_no_vul_completed_items,
                    min_no_vul_submissions=args.post_submit_min_no_vul_submissions,
                ):
                    append_log(
                        log_path,
                        (
                            f"aborting_post_submit_no_vul pid={run.pid} run_root={run_root} elapsed={run.elapsed_seconds}s "
                            f"agent_id={agent_id} metrics={json.dumps(metrics, sort_keys=True)} records={len(records)}"
                        ),
                    )
                    terminate_pids(run.pids or (run.pid,), dry_run=args.dry_run)
                    try:
                        updated = reconcile_terminated_active_run(
                            run_root=run_root,
                            pocdb_path=Path(args.orphan_pocdb_path),
                            dry_run=args.dry_run,
                            records_override=records,
                        )
                    except Exception as exc:  # noqa: BLE001
                        append_log(log_path, f"post_submit_reconcile_failed run_root={run_root} error={exc}")
                    else:
                        append_log(
                            log_path,
                            (
                                f"post_submit_reconciled run_root={run_root} task_id={updated['task_id']} "
                                f"status={updated['status']} records={len(updated.get('records', []))}"
                            ),
                        )
                    actions += 1
                    continue
                if should_abort_for_post_submit_no_vul_submission_cap(
                    elapsed_seconds=run.elapsed_seconds,
                    metrics=metrics,
                    records=records,
                    min_runtime_seconds=args.post_submit_max_no_vul_runtime_seconds,
                    max_no_vul_submissions=args.post_submit_max_no_vul_submissions,
                ):
                    append_log(
                        log_path,
                        (
                            f"aborting_post_submit_no_vul_cap pid={run.pid} run_root={run_root} elapsed={run.elapsed_seconds}s "
                            f"agent_id={agent_id} metrics={json.dumps(metrics, sort_keys=True)} records={len(records)}"
                        ),
                    )
                    terminate_pids(run.pids or (run.pid,), dry_run=args.dry_run)
                    try:
                        updated = reconcile_terminated_active_run(
                            run_root=run_root,
                            pocdb_path=Path(args.orphan_pocdb_path),
                            dry_run=args.dry_run,
                            records_override=records,
                        )
                    except Exception as exc:  # noqa: BLE001
                        append_log(log_path, f"post_submit_cap_reconcile_failed run_root={run_root} error={exc}")
                    else:
                        append_log(
                            log_path,
                            (
                                f"post_submit_cap_reconciled run_root={run_root} task_id={updated['task_id']} "
                                f"status={updated['status']} records={len(updated.get('records', []))}"
                            ),
                        )
                    actions += 1
                    continue
                if should_abort_for_post_submit_stale_no_progress_stall(
                    elapsed_seconds=run.elapsed_seconds,
                    metrics=metrics,
                    records=records,
                    min_runtime_seconds=args.post_submit_stale_no_vul_runtime_seconds,
                    min_completed_items=args.post_submit_stale_no_vul_completed_items,
                    min_no_vul_submissions=args.post_submit_stale_min_no_vul_submissions,
                    min_record_idle_seconds=args.post_submit_stale_no_vul_seconds,
                ):
                    append_log(
                        log_path,
                        (
                            f"aborting_post_submit_stale_no_progress pid={run.pid} run_root={run_root} elapsed={run.elapsed_seconds}s "
                            f"agent_id={agent_id} metrics={json.dumps(metrics, sort_keys=True)} records={len(records)} "
                            f"record_idle_seconds={int(latest_record_age_seconds(records) or 0)}"
                        ),
                    )
                    terminate_pids(run.pids or (run.pid,), dry_run=args.dry_run)
                    try:
                        updated = reconcile_terminated_active_run(
                            run_root=run_root,
                            pocdb_path=Path(args.orphan_pocdb_path),
                            dry_run=args.dry_run,
                            records_override=records,
                        )
                    except Exception as exc:  # noqa: BLE001
                        append_log(log_path, f"post_submit_stale_reconcile_failed run_root={run_root} error={exc}")
                    else:
                        append_log(
                            log_path,
                            (
                                f"post_submit_stale_reconciled run_root={run_root} task_id={updated['task_id']} "
                                f"status={updated['status']} records={len(updated.get('records', []))}"
                            ),
                        )
                    actions += 1
                    continue
                if should_abort_for_post_submit_idle_no_vul_stall(
                    elapsed_seconds=run.elapsed_seconds,
                    metrics=metrics,
                    records=records,
                    min_runtime_seconds=args.post_submit_idle_no_vul_runtime_seconds,
                    min_completed_items=args.post_submit_idle_no_vul_completed_items,
                    min_no_vul_submissions=args.post_submit_idle_min_no_vul_submissions,
                    min_record_idle_seconds=args.post_submit_idle_no_vul_seconds,
                ):
                    append_log(
                        log_path,
                        (
                            f"aborting_post_submit_idle_no_progress pid={run.pid} run_root={run_root} elapsed={run.elapsed_seconds}s "
                            f"agent_id={agent_id} metrics={json.dumps(metrics, sort_keys=True)} records={len(records)} "
                            f"record_idle_seconds={int(latest_record_age_seconds(records) or 0)}"
                        ),
                    )
                    terminate_pids(run.pids or (run.pid,), dry_run=args.dry_run)
                    try:
                        updated = reconcile_terminated_active_run(
                            run_root=run_root,
                            pocdb_path=Path(args.orphan_pocdb_path),
                            dry_run=args.dry_run,
                            records_override=records,
                        )
                    except Exception as exc:  # noqa: BLE001
                        append_log(log_path, f"post_submit_idle_reconcile_failed run_root={run_root} error={exc}")
                    else:
                        append_log(
                            log_path,
                            (
                                f"post_submit_idle_reconciled run_root={run_root} task_id={updated['task_id']} "
                                f"status={updated['status']} records={len(updated.get('records', []))}"
                            ),
                        )
                    actions += 1
        if args.orphan_pocdb_path:
            run_search_root = (workspace_root / args.results_root).resolve()
            for run_root in find_stale_orphan_run_roots(
                run_search_root,
                active_run_roots=active_run_roots,
                inactive_seconds=args.orphan_inactive_seconds,
            ):
                try:
                    updated = reconcile_stale_orphan_run(
                        run_root=run_root,
                        pocdb_path=Path(args.orphan_pocdb_path),
                        dry_run=args.dry_run,
                        run_verify=args.orphan_run_verify,
                    )
                except Exception as exc:  # noqa: BLE001
                    append_log(log_path, f"orphan_reconcile_failed run_root={run_root} error={exc}")
                    continue
                append_log(
                    log_path,
                    f"orphan_reconciled run_root={run_root} task_id={updated['task_id']} status={updated['status']} records={len(updated.get('records', []))}",
                )
                actions += 1
        if actions == 0:
            append_log(log_path, "poll complete: no actions")
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
