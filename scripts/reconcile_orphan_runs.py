from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import codex_rescue_runner as rescue
import rescue_queue_launcher as queue

RUN_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-(.+)-codex-rescue-attempt(\d+)-[0-9a-f]+$")
SUBMIT_SERVER_PATTERN = re.compile(r"curl -X POST (\S+)/submit-vul")
SUBMIT_METADATA_PATTERN = re.compile(r"metadata=(\{.*\})")


@dataclass
class SubmitMetadata:
    agent_id: str
    server: str | None


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_run_identity(run_root: Path) -> tuple[str, int]:
    match = RUN_ID_PATTERN.match(run_root.name)
    if match is None:
        raise ValueError(f"unrecognized rescue run id format: {run_root.name}")
    task_slug = match.group(1)
    attempt = int(match.group(2))
    if "_" not in task_slug:
        raise ValueError(f"cannot recover task_id from run id: {run_root.name}")
    family, subid = task_slug.split("_", 1)
    return f"{family}:{subid}", attempt


def parse_submit_metadata(task_dir: Path) -> SubmitMetadata:
    submit_text = (task_dir / "submit.sh").read_text(encoding="utf-8")
    server_match = SUBMIT_SERVER_PATTERN.search(submit_text)
    metadata_match = SUBMIT_METADATA_PATTERN.search(submit_text)
    if metadata_match is None:
        raise ValueError(f"submit metadata not found in {task_dir / 'submit.sh'}")
    metadata = json.loads(metadata_match.group(1))
    agent_id = metadata.get("agent_id")
    if not agent_id:
        raise ValueError(f"agent_id missing in {task_dir / 'submit.sh'}")
    server = server_match.group(1) if server_match is not None else None
    return SubmitMetadata(agent_id=agent_id, server=server)


def is_run_active(run_root: Path, ps_output: str) -> bool:
    return str(run_root / "task") in ps_output


def has_valid_result(result_path: Path) -> bool:
    if not result_path.exists():
        return False
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict)


def build_orphan_result(
    *,
    run_root: Path,
    task_id: str,
    attempt: int,
    agent_id: str,
    server: str | None,
    records: list[dict],
) -> dict:
    started_at = datetime.fromtimestamp(run_root.stat().st_mtime, UTC).isoformat()
    codex_events = run_root / "codex_events.jsonl"
    metrics = rescue.collect_codex_event_metrics(codex_events)
    derived_status = rescue.derive_status(records, 0, None)
    failure_category = None if derived_status == "success" else derived_status
    return {
        "run_id": run_root.name,
        "task_id": task_id,
        "source_run_id": run_root.name,
        "parent_run_id": run_root.name,
        "status": "pending_reconcile",
        "solution_status": "pending_reconcile",
        "executor_status": "pending_reconcile",
        "failure_category": failure_category,
        "retryable": derived_status != "success",
        "agent_id": agent_id,
        "attempt": attempt,
        "external_step_cap_supported": False,
        "started_at": started_at,
        "ended_at": now_utc().isoformat(),
        "server": server,
        "paths": {
            "run_root": str(run_root),
            "task_dir": str(run_root / "task"),
            "prompt": str(run_root / "prompt.txt"),
            "codex_events": str(codex_events),
            "codex_last_message": str(run_root / "codex_last_message.md"),
        },
        "codex": {
            "command": None,
            "returncode": 0,
            "timed_out": False,
            "watchdog_abort_reason": None,
            "watchdog_metrics": metrics,
            "elapsed_seconds": int(time.time() - run_root.stat().st_mtime),
        },
        "verify": None,
        "records": [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile stale rescue runs with missing or invalid result.json.")
    parser.add_argument("--run-root", type=Path, default=Path("codex_rescue_runs_local"))
    parser.add_argument("--pocdb-path", type=Path, required=True)
    parser.add_argument("--inactive-seconds", type=int, default=180)
    parser.add_argument("--run-verify", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ps_output = queue.get_ps_output()
    now_ts = time.time()
    reconciled = 0
    skipped = 0

    for events_path in sorted(args.run_root.rglob("codex_events.jsonl")):
        run_root = events_path.parent
        result_path = run_root / "result.json"
        if has_valid_result(result_path):
            continue
        if is_run_active(run_root, ps_output):
            continue
        if now_ts - events_path.stat().st_mtime < args.inactive_seconds:
            continue
        if not (run_root / "task" / "submit.sh").exists():
            skipped += 1
            continue

        try:
            task_id, attempt = parse_run_identity(run_root)
            submit_metadata = parse_submit_metadata(run_root / "task")
            records = rescue.load_records(args.pocdb_path, submit_metadata.agent_id)
            result = build_orphan_result(
                run_root=run_root,
                task_id=task_id,
                attempt=attempt,
                agent_id=submit_metadata.agent_id,
                server=submit_metadata.server,
                records=records,
            )
            verify_payload = None
            if args.run_verify and records and rescue.needs_verification(records):
                if not submit_metadata.server:
                    raise ValueError(f"server missing for verification: {run_root}")
                verify_payload, records = rescue.run_verify_step(
                    server=submit_metadata.server,
                    pocdb_path=args.pocdb_path,
                    agent_id=submit_metadata.agent_id,
                    run_root=run_root,
                )
            updated = rescue.update_result_payload(result, records, args.pocdb_path, verify_payload=verify_payload)
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP error run_root={run_root} error={exc}", file=sys.stderr)
            skipped += 1
            continue

        print(
            f"RECONCILE run_root={run_root} task_id={updated['task_id']} status={updated['status']} records={len(updated.get('records', []))}"
        )
        if not args.dry_run:
            rescue.write_result_files(run_root, updated)
        reconciled += 1

    print(f"SUMMARY reconciled={reconciled} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
