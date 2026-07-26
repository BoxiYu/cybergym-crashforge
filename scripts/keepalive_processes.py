from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class ProcessSpec:
    name: str
    argv: list[str]
    match_substrings: list[str]
    workdir: str | None = None
    stdout_log_path: str | None = None
    state_json_path: str | None = None
    state_json_field: str | None = None
    state_json_value: str | None = None


def now_utc() -> datetime:
    return datetime.now(UTC)


def append_log(log_path: Path, message: str) -> None:
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


def state_condition_met(spec: ProcessSpec) -> bool:
    if not spec.state_json_path or not spec.state_json_field:
        return False
    json_path = Path(spec.state_json_path)
    if not json_path.exists():
        return False
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    try:
        value = resolve_json_field(payload, spec.state_json_field)
    except KeyError:
        return False
    if spec.state_json_value is None:
        return bool(value)
    return str(value) == spec.state_json_value


def load_specs(spec_file: Path) -> list[ProcessSpec]:
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("specs"), list):
        raise ValueError("spec file must be an object with a 'specs' list")
    specs: list[ProcessSpec] = []
    for raw in payload["specs"]:
        if not isinstance(raw, dict):
            raise ValueError("spec entries must be objects")
        name = str(raw.get("name") or "")
        argv = raw.get("argv")
        match_substrings = raw.get("match_substrings")
        if not name or not isinstance(argv, list) or not argv or not isinstance(match_substrings, list) or not match_substrings:
            raise ValueError(f"invalid process spec: {raw}")
        specs.append(
            ProcessSpec(
                name=name,
                argv=[str(item) for item in argv],
                match_substrings=[str(item) for item in match_substrings],
                workdir=str(raw["workdir"]) if raw.get("workdir") else None,
                stdout_log_path=str(raw["stdout_log_path"]) if raw.get("stdout_log_path") else None,
                state_json_path=str(raw["state_json_path"]) if raw.get("state_json_path") else None,
                state_json_field=str(raw["state_json_field"]) if raw.get("state_json_field") else None,
                state_json_value=str(raw["state_json_value"]) if raw.get("state_json_value") is not None else None,
            )
        )
    return specs


def get_process_rows() -> list[tuple[int, str]]:
    result = subprocess.run(["ps", "-eo", "pid=,args="], check=True, capture_output=True, text=True)
    rows: list[tuple[int, str]] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            pid_text, args = line.split(None, 1)
        except ValueError:
            continue
        try:
            rows.append((int(pid_text), args))
        except ValueError:
            continue
    return rows


def matching_pids(spec: ProcessSpec, process_rows: list[tuple[int, str]], *, ignore_pids: set[int]) -> list[int]:
    matches: list[int] = []
    for pid, args in process_rows:
        if pid in ignore_pids:
            continue
        if all(fragment in args for fragment in spec.match_substrings):
            matches.append(pid)
    return matches


def spawn_spec(spec: ProcessSpec) -> int:
    stdout_handle = None
    try:
        if spec.stdout_log_path:
            stdout_path = Path(spec.stdout_log_path)
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_handle = stdout_path.open("a", encoding="utf-8")
        process = subprocess.Popen(  # noqa: S603
            spec.argv,
            cwd=spec.workdir or None,
            stdout=stdout_handle or subprocess.DEVNULL,
            stderr=stdout_handle or subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
        return process.pid
    finally:
        if stdout_handle is not None:
            stdout_handle.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Keep selected long-running helper processes alive.")
    parser.add_argument("--spec-file", required=True)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--log-path", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    spec_file = Path(args.spec_file)
    log_path = Path(args.log_path)
    append_log(log_path, f"keepalive started spec_file={spec_file}")
    last_status: dict[str, str] = {}
    while True:
        specs = load_specs(spec_file)
        process_rows = get_process_rows()
        ignore_pids = {os.getpid(), os.getppid()}
        for spec in specs:
            if state_condition_met(spec):
                status = "complete"
                if last_status.get(spec.name) != status:
                    append_log(log_path, f"{spec.name} state_complete")
                last_status[spec.name] = status
                continue
            pids = matching_pids(spec, process_rows, ignore_pids=ignore_pids)
            if pids:
                status = "running"
                if last_status.get(spec.name) != status:
                    append_log(log_path, f"{spec.name} running pids={pids[:4]}")
                last_status[spec.name] = status
                continue
            pid = spawn_spec(spec)
            append_log(log_path, f"{spec.name} started pid={pid} argv={' '.join(spec.argv)}")
            last_status[spec.name] = "started"
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
