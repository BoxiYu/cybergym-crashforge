from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from verdicts import classify_poc_verdict

import rescue_stall_monitor as stall


ACTIVE_MANIFEST_RE = re.compile(
    r"(fresh_manifests|retry_manifests|followup_retry_manifests|post_followup_retry_manifests)/(\d+_arvo_(\d+))\.jsonl"
)
RUN_TASK_RE = re.compile(r"(\d{8}-\d{6})-arvo_(\d+)-")


def now_utc() -> datetime:
    return datetime.now(UTC)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def collect_active_runners(wave_dir: Path) -> list[dict[str, Any]]:
    proc = subprocess.run(["ps", "-eo", "pid=,etimes=,cmd="], check=True, capture_output=True, text=True)
    active: list[dict[str, Any]] = []
    manifest_prefix = f"{wave_dir.relative_to(Path.cwd())}/"
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if "scripts/codex_rescue_runner.py run --manifest " not in line:
            continue
        if manifest_prefix not in line:
            continue
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            continue
        pid_text, elapsed_text, command = parts
        match = ACTIVE_MANIFEST_RE.search(command)
        if not match:
            continue
        try:
            pid = int(pid_text)
            elapsed_seconds = int(elapsed_text)
        except ValueError:
            continue
        manifest_kind, queue_name, task_num = match.groups()
        active.append(
            {
                "pid": pid,
                "elapsed_seconds": elapsed_seconds,
                "manifest_kind": manifest_kind,
                "queue_name": queue_name,
                "task_id": f"arvo:{task_num}",
                "command": command,
            }
        )
    return sorted(active, key=lambda item: (item["manifest_kind"], item["queue_name"]))


def collect_run_roots(runs_dir: Path) -> dict[str, Path]:
    latest_by_task: dict[str, tuple[str, float, Path]] = {}
    for candidate in runs_dir.rglob("*-arvo_*"):
        if not candidate.is_dir():
            continue
        match = RUN_TASK_RE.search(candidate.name)
        if not match:
            continue
        launch_stamp, task_num = match.groups()
        task_id = f"arvo:{task_num}"
        order_key = (launch_stamp, candidate.stat().st_mtime)
        current = latest_by_task.get(task_id)
        if current is None or order_key > (current[0], current[1]):
            latest_by_task[task_id] = (launch_stamp, candidate.stat().st_mtime, candidate)
    return {task_id: payload[2] for task_id, payload in latest_by_task.items()}


def derive_record_verdict(row: dict[str, Any]) -> str:
    return str(
        classify_poc_verdict(
            row.get("vul_exit_code"),
            row.get("fix_exit_code"),
            str(row.get("task_id") or ""),
        )
    )


def load_task_records(task_ids: list[str], pocdb_path: Path) -> dict[str, list[dict[str, Any]]]:
    if not task_ids:
        return {}
    by_task: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    with sqlite3.connect(pocdb_path) as conn:
        conn.row_factory = sqlite3.Row
        for task_id in task_ids:
            rows = conn.execute(
                """
                select task_id, poc_id, created_at, updated_at, vul_exit_code, fix_exit_code
                from poc_records
                where task_id = ?
                order by created_at
                """,
                (task_id,),
            ).fetchall()
            task_rows: list[dict[str, Any]] = []
            for row in rows:
                payload = dict(row)
                payload["verdict"] = derive_record_verdict(payload)
                task_rows.append(payload)
            by_task[task_id] = task_rows
    return by_task


def collect_event_metrics(run_root: Path) -> tuple[dict[str, int], dict[str, Any] | None]:
    metrics = {
        "completed_items": 0,
        "submit_started": 0,
        "submit_completed": 0,
    }
    last_submit_completed: dict[str, Any] | None = None
    events_path = run_root / "codex_events.jsonl"
    if not events_path.exists():
        return metrics, last_submit_completed

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
                last_submit_completed = payload
    return metrics, last_submit_completed


def make_candidate(rule: str, blockers: dict[str, float | int]) -> dict[str, Any]:
    positive_blockers = {key: value for key, value in blockers.items() if value > 0}
    return {
        "rule": rule,
        "ready": not positive_blockers,
        "blockers": positive_blockers,
    }


def build_candidate_reclaims(
    *,
    elapsed_seconds: int,
    metrics: dict[str, int],
    verdicts: list[str],
    task_summary: dict[str, Any],
    latest_record_age_seconds: float | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    has_verified = any(verdict == "verified_success" for verdict in verdicts)
    all_non_progress = bool(verdicts) and all(verdict in stall.IDLE_NON_PROGRESS_VERDICTS for verdict in verdicts)
    observed_submissions = max(metrics["submit_completed"], len(verdicts))
    non_differential_count = sum(1 for verdict in verdicts if verdict == "non_differential")
    total_no_vul_records = int(task_summary.get("total_no_vul_records") or 0)

    if metrics["submit_started"] == 0 and metrics["submit_completed"] == 0:
        early_no_submit_blockers = {
            "runtime_seconds_needed": max(
                0,
                stall.DEFAULT_EARLY_NO_SUBMIT_RUNTIME_SECONDS - elapsed_seconds,
            ),
            "completed_items_needed": max(
                0,
                stall.DEFAULT_EARLY_NO_SUBMIT_COMPLETED_ITEMS - metrics["completed_items"],
            ),
        }
        candidates.append(make_candidate("early_no_submit", early_no_submit_blockers))

    if metrics["submit_completed"] > 0 and len(verdicts) == 0:
        post_submit_no_record_blockers = {
            "submissions_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_NO_RECORD_MIN_SUBMISSIONS - metrics["submit_completed"],
            ),
            "runtime_seconds_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_NO_RECORD_RUNTIME_SECONDS - elapsed_seconds,
            ),
            "completed_items_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_NO_RECORD_COMPLETED_ITEMS - metrics["completed_items"],
            ),
        }
        candidates.append(make_candidate("post_submit_no_record", post_submit_no_record_blockers))

    if not has_verified and all_non_progress:
        no_vul_cap_blockers = {
            "submissions_needed": max(0, stall.DEFAULT_POST_SUBMIT_MAX_NO_VUL_SUBMISSIONS - observed_submissions),
            "runtime_seconds_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_MAX_NO_VUL_RUNTIME_SECONDS - elapsed_seconds,
            )
            if observed_submissions <= stall.DEFAULT_POST_SUBMIT_MAX_NO_VUL_SUBMISSIONS
            else 0,
        }
        candidates.append(make_candidate("post_submit_no_vul_cap", no_vul_cap_blockers))

        stale_blockers = {
            "submissions_needed": max(0, stall.DEFAULT_POST_SUBMIT_STALE_MIN_NO_VUL_SUBMISSIONS - metrics["submit_completed"]),
            "runtime_seconds_needed": max(0, stall.DEFAULT_POST_SUBMIT_STALE_NO_VUL_RUNTIME_SECONDS - elapsed_seconds),
            "completed_items_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_STALE_NO_VUL_COMPLETED_ITEMS - metrics["completed_items"],
            ),
            "db_idle_seconds_needed": max(
                0.0,
                stall.DEFAULT_POST_SUBMIT_STALE_NO_VUL_SECONDS - (latest_record_age_seconds or 0.0),
            ),
        }
        candidates.append(make_candidate("post_submit_stale_no_progress", stale_blockers))

        cumulative_blockers = {
            "current_submissions_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_CURRENT_SUBMISSIONS - observed_submissions,
            ),
            "runtime_seconds_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_RUNTIME_SECONDS - elapsed_seconds,
            ),
            "total_no_vul_records_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_TOTAL_NO_VUL_SUBMISSIONS - total_no_vul_records,
            ),
            "db_idle_seconds_needed": max(
                0.0,
                stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_SECONDS - (latest_record_age_seconds or 0.0),
            ),
        }
        candidates.append(make_candidate("post_submit_cumulative_no_vul", cumulative_blockers))

        cumulative_cap_blockers = {
            "current_submissions_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MAX_CURRENT_SUBMISSIONS - observed_submissions,
            ),
            "runtime_seconds_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_RUNTIME_SECONDS - elapsed_seconds,
            ),
            "total_no_vul_records_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_TOTAL_NO_VUL_SUBMISSIONS - total_no_vul_records,
            ),
        }
        candidates.append(make_candidate("post_submit_cumulative_no_vul_cap", cumulative_cap_blockers))

    if not has_verified and all_non_progress and non_differential_count > 0:
        repeated_non_diff_blockers = {
            "current_submissions_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_MIN_CURRENT_SUBMISSIONS - observed_submissions,
            ),
            "non_differential_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_MIN_NON_DIFF_SUBMISSIONS - non_differential_count,
            ),
            "runtime_seconds_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_NON_DIFF_RUNTIME_SECONDS - elapsed_seconds,
            ),
            "completed_items_needed": max(
                0,
                stall.DEFAULT_POST_SUBMIT_NON_DIFF_COMPLETED_ITEMS - metrics["completed_items"],
            ),
            "db_idle_seconds_needed": max(
                0.0,
                stall.DEFAULT_POST_SUBMIT_NON_DIFF_IDLE_SECONDS - (latest_record_age_seconds or 0.0),
            ),
        }
        candidates.append(make_candidate("post_submit_repeated_non_differential", repeated_non_diff_blockers))

    candidates.sort(
        key=lambda item: (
            0 if item["ready"] else 1,
            len(item["blockers"]),
            sum(float(value) for value in item["blockers"].values()),
            item["rule"],
        )
    )
    return candidates


def build_watch_entry(
    *,
    active_runner: dict[str, Any],
    run_root: Path | None,
    records: list[dict[str, Any]],
    task_summary: dict[str, Any],
    now_dt: datetime,
) -> dict[str, Any]:
    metrics = {
        "completed_items": 0,
        "submit_started": 0,
        "submit_completed": 0,
    }
    last_submit_output_excerpt = ""
    if run_root is not None:
        metrics, last_submit_completed = collect_event_metrics(run_root)
        if last_submit_completed is not None:
            last_submit_output_excerpt = str((last_submit_completed.get("item") or {}).get("aggregated_output") or "")[:220]

    verdicts = [str(record.get("verdict")) for record in records]
    latest_record_age_seconds = stall.latest_record_age_seconds(records, now_dt=now_dt)
    observed_submissions = max(metrics["submit_completed"], len(records))
    submit_record_gap = max(0, metrics["submit_completed"] - len(records))

    checks = {
        "post_submit_no_record": stall.should_abort_for_post_submit_no_record_stall(
            elapsed_seconds=active_runner["elapsed_seconds"],
            metrics=metrics,
            records=records,
            min_runtime_seconds=stall.DEFAULT_POST_SUBMIT_NO_RECORD_RUNTIME_SECONDS,
            min_completed_items=stall.DEFAULT_POST_SUBMIT_NO_RECORD_COMPLETED_ITEMS,
            min_submissions=stall.DEFAULT_POST_SUBMIT_NO_RECORD_MIN_SUBMISSIONS,
        ),
        "post_submit_all_no_vul": stall.should_abort_for_post_submit_no_vul_stall(
            elapsed_seconds=active_runner["elapsed_seconds"],
            metrics=metrics,
            records=records,
            min_runtime_seconds=stall.DEFAULT_POST_SUBMIT_NO_VUL_RUNTIME_SECONDS,
            min_completed_items=stall.DEFAULT_POST_SUBMIT_NO_VUL_COMPLETED_ITEMS,
            min_no_vul_submissions=stall.DEFAULT_POST_SUBMIT_MIN_NO_VUL_SUBMISSIONS,
        ),
        "post_submit_no_vul_cap": stall.should_abort_for_post_submit_no_vul_submission_cap(
            elapsed_seconds=active_runner["elapsed_seconds"],
            metrics=metrics,
            records=records,
            min_runtime_seconds=stall.DEFAULT_POST_SUBMIT_MAX_NO_VUL_RUNTIME_SECONDS,
            max_no_vul_submissions=stall.DEFAULT_POST_SUBMIT_MAX_NO_VUL_SUBMISSIONS,
        ),
        "post_submit_stale_no_progress": stall.should_abort_for_post_submit_stale_no_progress_stall(
            elapsed_seconds=active_runner["elapsed_seconds"],
            metrics=metrics,
            records=records,
            min_runtime_seconds=stall.DEFAULT_POST_SUBMIT_STALE_NO_VUL_RUNTIME_SECONDS,
            min_completed_items=stall.DEFAULT_POST_SUBMIT_STALE_NO_VUL_COMPLETED_ITEMS,
            min_no_vul_submissions=stall.DEFAULT_POST_SUBMIT_STALE_MIN_NO_VUL_SUBMISSIONS,
            min_record_idle_seconds=stall.DEFAULT_POST_SUBMIT_STALE_NO_VUL_SECONDS,
            now_dt=now_dt,
        ),
        "post_submit_repeated_non_differential": stall.should_abort_for_post_submit_repeated_non_differential_stall(
            elapsed_seconds=active_runner["elapsed_seconds"],
            metrics=metrics,
            records=records,
            min_runtime_seconds=stall.DEFAULT_POST_SUBMIT_NON_DIFF_RUNTIME_SECONDS,
            min_completed_items=stall.DEFAULT_POST_SUBMIT_NON_DIFF_COMPLETED_ITEMS,
            min_current_submissions=stall.DEFAULT_POST_SUBMIT_MIN_CURRENT_SUBMISSIONS,
            min_non_differential_submissions=stall.DEFAULT_POST_SUBMIT_MIN_NON_DIFF_SUBMISSIONS,
            min_record_idle_seconds=stall.DEFAULT_POST_SUBMIT_NON_DIFF_IDLE_SECONDS,
            now_dt=now_dt,
        ),
        "post_submit_cumulative_no_vul": stall.should_abort_for_post_submit_cumulative_no_vul_exhaustion(
            elapsed_seconds=active_runner["elapsed_seconds"],
            metrics=metrics,
            records=records,
            task_summary=task_summary,
            min_runtime_seconds=stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_RUNTIME_SECONDS,
            min_current_submissions=stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_CURRENT_SUBMISSIONS,
            min_total_no_vul_submissions=stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_TOTAL_NO_VUL_SUBMISSIONS,
            min_record_idle_seconds=stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_SECONDS,
            now_dt=now_dt,
        ),
        "post_submit_cumulative_no_vul_cap": stall.should_abort_for_post_submit_cumulative_no_vul_submission_cap(
            elapsed_seconds=active_runner["elapsed_seconds"],
            metrics=metrics,
            records=records,
            task_summary=task_summary,
            min_runtime_seconds=stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_RUNTIME_SECONDS,
            max_current_submissions=stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MAX_CURRENT_SUBMISSIONS,
            min_total_no_vul_submissions=stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_TOTAL_NO_VUL_SUBMISSIONS,
        ),
    }
    triggered_checks = [name for name, enabled in checks.items() if enabled]
    candidate_reclaims = build_candidate_reclaims(
        elapsed_seconds=active_runner["elapsed_seconds"],
        metrics=metrics,
        verdicts=verdicts,
        task_summary=task_summary,
        latest_record_age_seconds=latest_record_age_seconds,
    )

    notes: list[str] = []
    all_non_progress = bool(verdicts) and all(verdict in stall.IDLE_NON_PROGRESS_VERDICTS for verdict in verdicts)
    non_differential_count = sum(1 for verdict in verdicts if verdict == "non_differential")
    if triggered_checks:
        notes.append(f"Reclaimable now via: {', '.join(triggered_checks)}.")
    else:
        if submit_record_gap > 0:
            notes.append(
                f"Submit completions exceed DB records by {submit_record_gap}; likely duplicate re-submits or submits that have not produced new records yet."
            )
        if metrics["submit_completed"] < stall.DEFAULT_POST_SUBMIT_MIN_NO_VUL_SUBMISSIONS:
            notes.append(
                f"Need {stall.DEFAULT_POST_SUBMIT_MIN_NO_VUL_SUBMISSIONS - metrics['submit_completed']} more submit completions for 4-submit post-submit rules."
            )
        if verdicts and all(verdict == "no_vul_crash" for verdict in verdicts) and active_runner["elapsed_seconds"] < stall.DEFAULT_POST_SUBMIT_NO_VUL_RUNTIME_SECONDS:
            notes.append(
                f"All-no-vul reclaim still requires {stall.DEFAULT_POST_SUBMIT_NO_VUL_RUNTIME_SECONDS - active_runner['elapsed_seconds']} more runtime seconds."
            )
        if non_differential_count > 0 and non_differential_count < stall.DEFAULT_POST_SUBMIT_MIN_NON_DIFF_SUBMISSIONS:
            notes.append(
                f"Need {stall.DEFAULT_POST_SUBMIT_MIN_NON_DIFF_SUBMISSIONS - non_differential_count} more non_differential verdicts before repeated-non_differential reclaim applies."
            )
        if non_differential_count >= stall.DEFAULT_POST_SUBMIT_MIN_NON_DIFF_SUBMISSIONS:
            if active_runner["elapsed_seconds"] < stall.DEFAULT_POST_SUBMIT_NON_DIFF_RUNTIME_SECONDS:
                notes.append(
                    f"Repeated-non_differential reclaim still requires {stall.DEFAULT_POST_SUBMIT_NON_DIFF_RUNTIME_SECONDS - active_runner['elapsed_seconds']} more runtime seconds."
                )
            if metrics["completed_items"] < stall.DEFAULT_POST_SUBMIT_NON_DIFF_COMPLETED_ITEMS:
                notes.append(
                    f"Repeated-non_differential reclaim still requires {stall.DEFAULT_POST_SUBMIT_NON_DIFF_COMPLETED_ITEMS - metrics['completed_items']} more completed items."
                )
            if observed_submissions < stall.DEFAULT_POST_SUBMIT_MIN_CURRENT_SUBMISSIONS:
                notes.append(
                    f"Repeated-non_differential reclaim still requires {stall.DEFAULT_POST_SUBMIT_MIN_CURRENT_SUBMISSIONS - observed_submissions} more total submissions."
                )
        if latest_record_age_seconds is not None and latest_record_age_seconds < stall.DEFAULT_POST_SUBMIT_STALE_NO_VUL_SECONDS:
            notes.append(
                f"Latest DB record is only {latest_record_age_seconds:.0f}s old; stale-no-progress rule needs {stall.DEFAULT_POST_SUBMIT_STALE_NO_VUL_SECONDS}s."
            )
        total_no_vul_records = int(task_summary.get("total_no_vul_records") or 0)
        if total_no_vul_records >= stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_TOTAL_NO_VUL_SUBMISSIONS:
            if active_runner["elapsed_seconds"] < stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_RUNTIME_SECONDS:
                notes.append(
                    f"Cumulative-no_vul reclaim still requires {stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_RUNTIME_SECONDS - active_runner['elapsed_seconds']} more runtime seconds."
                )
            if observed_submissions < stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_CURRENT_SUBMISSIONS:
                notes.append(
                    f"Cumulative-no_vul reclaim still requires {stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_CURRENT_SUBMISSIONS - observed_submissions} more current-run submissions."
                )
            if latest_record_age_seconds is not None and latest_record_age_seconds < stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_SECONDS:
                notes.append(
                    f"Cumulative-no_vul reclaim still requires {stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_SECONDS - latest_record_age_seconds:.0f}s more DB idle time."
                )
        elif total_no_vul_records > 0:
            notes.append(
                f"Cumulative-no_vul reclaim still requires {stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_TOTAL_NO_VUL_SUBMISSIONS - total_no_vul_records} more total no_vul records."
            )
        if verdicts and not all_non_progress:
            notes.append("Recorded verdicts are not all non-progress verdicts, so idle/no-vul reclaim rules do not apply cleanly.")
        if not notes:
            notes.append("No reclaim rule is currently satisfied.")

    return {
        "task_id": active_runner["task_id"],
        "manifest_kind": active_runner.get("manifest_kind"),
        "queue_name": active_runner["queue_name"],
        "pid": active_runner["pid"],
        "elapsed_seconds": active_runner["elapsed_seconds"],
        "completed_items": metrics["completed_items"],
        "submit_started": metrics["submit_started"],
        "submit_completed": metrics["submit_completed"],
        "db_record_count": len(records),
        "db_task_summary": {
            "total_records": int(task_summary.get("total_records") or 0),
            "total_no_vul_records": int(task_summary.get("total_no_vul_records") or 0),
            "total_verified_success_records": int(task_summary.get("total_verified_success_records") or 0),
            "last_updated": task_summary.get("last_updated"),
        },
        "db_verdicts": verdicts,
        "observed_submissions": observed_submissions,
        "submit_record_gap": submit_record_gap,
        "latest_record_age_seconds": latest_record_age_seconds,
        "last_submit_output_excerpt": last_submit_output_excerpt,
        "checks": checks,
        "triggered_checks": triggered_checks,
        "reclaimable_now": bool(triggered_checks),
        "candidate_reclaims": candidate_reclaims,
        "next_reclaim_candidate": candidate_reclaims[0] if candidate_reclaims else None,
        "notes": notes,
    }


def build_payload(*, wave_dir: Path, runs_dir: Path, pocdb_path: Path) -> dict[str, Any]:
    now_dt = now_utc()
    active_runners = collect_active_runners(wave_dir)
    run_roots = collect_run_roots(runs_dir)
    task_summaries = {
        item["task_id"]: stall.load_task_record_summary(item["task_id"], pocdb_path)
        for item in active_runners
    }
    active_records_by_task: dict[str, list[dict[str, Any]]] = {}
    for item in active_runners:
        run_root = run_roots.get(item["task_id"])
        if run_root is None:
            active_records_by_task[item["task_id"]] = []
            continue
        try:
            _agent_id, records = stall.load_active_run_records(run_root, pocdb_path)
        except Exception:  # noqa: BLE001
            records = []
        active_records_by_task[item["task_id"]] = records

    return {
        "generated_at": now_dt.isoformat(),
        "thresholds": {
            "early_no_submit_runtime_seconds": stall.DEFAULT_EARLY_NO_SUBMIT_RUNTIME_SECONDS,
            "early_no_submit_completed_items": stall.DEFAULT_EARLY_NO_SUBMIT_COMPLETED_ITEMS,
            "post_submit_no_vul_runtime_seconds": stall.DEFAULT_POST_SUBMIT_NO_VUL_RUNTIME_SECONDS,
            "post_submit_no_vul_completed_items": stall.DEFAULT_POST_SUBMIT_NO_VUL_COMPLETED_ITEMS,
            "post_submit_min_no_vul_submissions": stall.DEFAULT_POST_SUBMIT_MIN_NO_VUL_SUBMISSIONS,
            "post_submit_no_record_runtime_seconds": stall.DEFAULT_POST_SUBMIT_NO_RECORD_RUNTIME_SECONDS,
            "post_submit_no_record_completed_items": stall.DEFAULT_POST_SUBMIT_NO_RECORD_COMPLETED_ITEMS,
            "post_submit_no_record_min_submissions": stall.DEFAULT_POST_SUBMIT_NO_RECORD_MIN_SUBMISSIONS,
            "post_submit_max_no_vul_submissions": stall.DEFAULT_POST_SUBMIT_MAX_NO_VUL_SUBMISSIONS,
            "post_submit_max_no_vul_runtime_seconds": stall.DEFAULT_POST_SUBMIT_MAX_NO_VUL_RUNTIME_SECONDS,
            "post_submit_stale_no_vul_runtime_seconds": stall.DEFAULT_POST_SUBMIT_STALE_NO_VUL_RUNTIME_SECONDS,
            "post_submit_stale_no_vul_completed_items": stall.DEFAULT_POST_SUBMIT_STALE_NO_VUL_COMPLETED_ITEMS,
            "post_submit_stale_min_no_vul_submissions": stall.DEFAULT_POST_SUBMIT_STALE_MIN_NO_VUL_SUBMISSIONS,
            "post_submit_stale_no_vul_seconds": stall.DEFAULT_POST_SUBMIT_STALE_NO_VUL_SECONDS,
            "post_submit_non_diff_runtime_seconds": stall.DEFAULT_POST_SUBMIT_NON_DIFF_RUNTIME_SECONDS,
            "post_submit_non_diff_completed_items": stall.DEFAULT_POST_SUBMIT_NON_DIFF_COMPLETED_ITEMS,
            "post_submit_min_current_submissions": stall.DEFAULT_POST_SUBMIT_MIN_CURRENT_SUBMISSIONS,
            "post_submit_min_non_diff_submissions": stall.DEFAULT_POST_SUBMIT_MIN_NON_DIFF_SUBMISSIONS,
            "post_submit_non_diff_idle_seconds": stall.DEFAULT_POST_SUBMIT_NON_DIFF_IDLE_SECONDS,
            "post_submit_cumulative_no_vul_runtime_seconds": stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_RUNTIME_SECONDS,
            "post_submit_cumulative_min_current_submissions": stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_CURRENT_SUBMISSIONS,
            "post_submit_cumulative_max_current_submissions": stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MAX_CURRENT_SUBMISSIONS,
            "post_submit_cumulative_min_total_no_vul_submissions": stall.DEFAULT_POST_SUBMIT_CUMULATIVE_MIN_TOTAL_NO_VUL_SUBMISSIONS,
            "post_submit_cumulative_no_vul_seconds": stall.DEFAULT_POST_SUBMIT_CUMULATIVE_NO_VUL_SECONDS,
        },
        "active_tasks": [
            build_watch_entry(
                active_runner=item,
                run_root=run_roots.get(item["task_id"]),
                records=active_records_by_task.get(item["task_id"], []),
                task_summary=task_summaries.get(item["task_id"], {}),
                now_dt=now_dt,
            )
            for item in active_runners
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a group2 hard runner intervention watch snapshot.")
    parser.add_argument("--wave-dir", type=Path, required=True)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--pocdb-path", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = build_payload(
        wave_dir=args.wave_dir.resolve(),
        runs_dir=args.runs_dir.resolve(),
        pocdb_path=args.pocdb_path.resolve(),
    )
    write_json(args.output_json.resolve(), payload)
    print(json.dumps({"active_task_count": len(payload["active_tasks"]), "output_json": str(args.output_json.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
