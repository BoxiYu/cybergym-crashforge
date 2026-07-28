from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import codex_rescue_runner as rescue


def now_utc() -> datetime:
    return datetime.now(UTC)


def append_log(log_path: Path, payload: dict[str, object]):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_utc().isoformat()} {json.dumps(payload, ensure_ascii=False)}\n")


def collect_runtime_blocked_tasks(results_root: Path, max_candidates: int | None) -> list[dict[str, object]]:
    rows = rescue.select_auto_submit_backfill_runs(results_root=results_root, max_candidates=max_candidates)
    local_images = rescue.list_local_docker_images()
    blocked: list[dict[str, object]] = []
    seen_task_ids: set[str] = set()
    for run_root, result in rows:
        task_id = result.get("task_id")
        if not task_id or task_id in seen_task_ids:
            continue
        probe = rescue.inspect_runtime_assets(task_id, local_images=local_images)
        missing_images = probe.get("missing_images") or []
        issues = probe.get("issues") or []
        if not missing_images or issues:
            continue
        seen_task_ids.add(task_id)
        blocked.append(
            {
                "task_id": task_id,
                "run_root": str(run_root),
                "status": result.get("status"),
                "failure_category": result.get("failure_category"),
                "missing_images": list(missing_images),
            }
        )
    blocked.sort(key=blocked_task_sort_key)
    return blocked


def blocked_task_sort_key(row: dict[str, object]) -> tuple[int, int, str]:
    task_id = str(row.get("task_id") or "")
    family, _, _subid = task_id.partition(":")
    family_priority = 0 if family == "oss-fuzz" else 1
    failure_category = str(row.get("failure_category") or "")
    failure_priority = 0 if failure_category in {"provider_timeout", "provider_stream_disconnect"} else 1
    return (family_priority, failure_priority, task_id)


def write_missing_jsonl(path: Path, blocked_tasks: list[dict[str, object]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"task_id": row["task_id"]}, ensure_ascii=False) for row in blocked_tasks]
    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    path.write_text(payload, encoding="utf-8")


def run_fastlane_pull(args: argparse.Namespace, blocked_tasks: list[dict[str, object]]) -> dict[str, object]:
    write_missing_jsonl(args.missing_jsonl, blocked_tasks)
    command = [
        args.python_bin,
        "scripts/server_data/download_missing_images.py",
        "--missing-jsonl",
        str(args.missing_jsonl),
        "--output-dir",
        str(args.output_dir),
        "--execute",
        "--max-workers",
        str(args.max_workers),
        "--pull-timeout-seconds",
        str(args.pull_timeout_seconds),
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
        "event": "fastlane_poll",
        "returncode": result.returncode,
        "blocked_task_count": len(blocked_tasks),
        "blocked_task_ids": [row["task_id"] for row in blocked_tasks],
        "summary": summary_payload,
        "stdout_tail": stdout_lines[-5:],
        "stderr_tail": stderr_lines[-5:],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prioritize missing runtime images for runs that are immediately backfillable once images land."
    )
    parser.add_argument("--results-root", type=Path, default=Path("codex_rescue_runs_local"))
    parser.add_argument(
        "--missing-jsonl",
        type=Path,
        default=Path("/tmp/codex_rescue_partition_july25/backfill_missing_image_fastlane.jsonl"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/tmp/codex_rescue_partition_july25/pull_missing_backfill_fastlane_exec"),
    )
    parser.add_argument("--tasks-file", type=Path, default=Path("cybergym_data/tasks.json"))
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--pull-timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--log-path", type=Path, default=Path("codex_rescue_runs_local/backfill_missing_image_fastlane_watch.log"))
    parser.add_argument("--python-bin", default=sys.executable)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    append_log(
        args.log_path,
        {
            "event": "watch_started",
            "results_root": str(args.results_root),
            "missing_jsonl": str(args.missing_jsonl),
            "output_dir": str(args.output_dir),
            "max_candidates": args.max_candidates,
            "max_workers": args.max_workers,
            "pull_timeout_seconds": args.pull_timeout_seconds,
            "poll_seconds": args.poll_seconds,
        },
    )
    while True:
        blocked_tasks = collect_runtime_blocked_tasks(args.results_root, args.max_candidates)
        if blocked_tasks:
            append_log(args.log_path, run_fastlane_pull(args, blocked_tasks))
        else:
            append_log(
                args.log_path,
                {
                    "event": "fastlane_poll",
                    "blocked_task_count": 0,
                    "blocked_task_ids": [],
                    "summary": None,
                    "stdout_tail": [],
                    "stderr_tail": [],
                },
            )
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
