from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_utc() -> datetime:
    return datetime.now(UTC)


def append_log(log_path: Path, message: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_utc().isoformat()} {message}\n")


def resolve_json_field(payload: Any, field_path: str) -> Any:
    value = payload
    for part in field_path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(field_path)
        value = value[part]
    return value


def ready_json_matches(json_path: Path, field_path: str, expected_value: str | None) -> tuple[bool, str | None]:
    if not json_path.exists():
        return False, f"ready_json_missing:{json_path}"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    try:
        value = resolve_json_field(payload, field_path)
    except KeyError:
        return False, f"ready_json_field_missing:{field_path}"
    if expected_value is None:
        return bool(value), f"ready_json_field_false:{field_path}"
    actual_value = str(value)
    if actual_value != expected_value:
        return False, f"ready_json_mismatch:{field_path}={actual_value}"
    return True, None


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


def find_blocking_processes(
    process_rows: list[tuple[int, str]],
    absent_substrings: list[str],
    *,
    ignore_pids: set[int] | None = None,
) -> dict[str, list[tuple[int, str]]]:
    ignored = ignore_pids or set()
    blockers: dict[str, list[tuple[int, str]]] = {}
    for substring in absent_substrings:
        matches = []
        for pid, args in process_rows:
            if pid in ignored:
                continue
            if args.startswith("/bin/bash -c") or args.startswith("bash -lc "):
                continue
            if substring not in args:
                continue
            if (
                "queue_autostart_watch.py" in args
                and "queue_autostart_watch.py" not in substring
                and "scripts/rescue_queue_launcher.py" in substring
            ):
                continue
            if (
                "queue_autostart_watch.py" in args
                and substring.endswith(".log")
                and "queue_autostart_watch.py" not in substring
            ):
                if f"--log-path {substring}" not in args:
                    continue
            matches.append((pid, args))
        if matches:
            blockers[substring] = matches
    return blockers


def next_clear_streak(clear_streak: int, *, ready: bool, blockers: dict[str, list[tuple[int, str]]]) -> int:
    if ready and not blockers:
        return clear_streak + 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for queue handoff conditions, then launch a follow-up command.")
    parser.add_argument("--ready-json-path", required=True)
    parser.add_argument("--ready-json-field", required=True)
    parser.add_argument("--ready-json-value")
    parser.add_argument("--wait-until-absent", action="append", default=[])
    parser.add_argument("--min-clear-polls", type=int, default=1)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--launch-cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.launch_cmd:
        raise SystemExit("--launch-cmd is required")
    return args


def main() -> int:
    args = parse_args()
    log_path = Path(args.log_path)
    append_log(log_path, "queue_autostart_watch started")
    clear_streak = 0

    try:
        while True:
            ready, ready_reason = ready_json_matches(
                Path(args.ready_json_path),
                args.ready_json_field,
                args.ready_json_value,
            )
            process_rows = get_process_rows()
            blockers = find_blocking_processes(
                process_rows,
                list(args.wait_until_absent),
                ignore_pids={os.getpid(), os.getppid()},
            )
            clear_streak = next_clear_streak(clear_streak, ready=ready, blockers=blockers)
            blocker_summary = ";".join(
                f"{substring}:{','.join(str(pid) for pid, _ in matches[:4])}"
                for substring, matches in blockers.items()
            )
            append_log(
                log_path,
                (
                    f"poll ready={ready} ready_reason={ready_reason} blockers={len(blockers)} "
                    f"clear_streak={clear_streak}/{args.min_clear_polls} details={blocker_summary or '-'}"
                ),
            )

            if clear_streak >= args.min_clear_polls:
                append_log(log_path, f"launching command={' '.join(args.launch_cmd)}")
                proc = subprocess.run(args.launch_cmd, check=False)
                append_log(log_path, f"launch exited rc={proc.returncode}")
                return proc.returncode

            time.sleep(args.poll_seconds)
    except Exception:  # noqa: BLE001
        append_log(log_path, "fatal_exception=" + traceback.format_exc().strip().replace("\n", "\\n"))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
