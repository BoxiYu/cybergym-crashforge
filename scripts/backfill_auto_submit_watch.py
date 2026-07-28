from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def now_utc() -> datetime:
    return datetime.now(UTC)


def append_log(log_path: Path, payload: dict[str, object]):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_utc().isoformat()} {json.dumps(payload, ensure_ascii=False)}\n")


def run_backfill(args: argparse.Namespace) -> dict[str, object]:
    command = [
        args.python_bin,
        "scripts/codex_rescue_runner.py",
        "backfill-auto-submit",
        "--results-root",
        args.results_root,
        "--max-candidates",
        str(args.max_candidates),
    ]
    if args.pocdb_path:
        command.extend(["--pocdb-path", args.pocdb_path])

    result = subprocess.run(command, check=False, capture_output=True, text=True)
    stdout_lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    summary: dict[str, object] | None = None
    if stdout_lines:
        try:
            parsed = json.loads(stdout_lines[-1])
            if isinstance(parsed, dict):
                summary = parsed
        except json.JSONDecodeError:
            summary = None
    return {
        "event": "backfill_poll",
        "returncode": result.returncode,
        "summary": summary,
        "stdout_tail": stdout_lines[-5:],
        "stderr_tail": (result.stderr or "").splitlines()[-5:],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Periodically run backfill-auto-submit as local images arrive.")
    parser.add_argument("--results-root", default="codex_rescue_runs_local")
    parser.add_argument("--pocdb-path", default=None)
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--log-path", default="codex_rescue_runs_local/backfill_auto_submit_watch.log")
    parser.add_argument("--python-bin", default=sys.executable)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_path = Path(args.log_path)
    append_log(
        log_path,
        {
            "event": "watch_started",
            "results_root": args.results_root,
            "max_candidates": args.max_candidates,
            "poll_seconds": args.poll_seconds,
        },
    )
    while True:
        append_log(log_path, run_backfill(args))
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
