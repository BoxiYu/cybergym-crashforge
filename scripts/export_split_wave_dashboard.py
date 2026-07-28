from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SERVER_PORT_RE = re.compile(r"http://127\.0\.0\.1:(\d+)")
RUNNER_CMD_RE = re.compile(r"scripts/codex_rescue_runner\.py run --manifest (\S+)")


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_queue_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"processed_names": [], "queue_complete": False, "updated_at": None, "read_error": "missing"}
    try:
        payload = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "processed_names": [],
            "queue_complete": False,
            "updated_at": None,
            "read_error": "invalid_json",
        }
    return {
        "processed_names": list(payload.get("processed_names") or []),
        "queue_complete": bool(payload.get("queue_complete")),
        "updated_at": payload.get("updated_at"),
        "read_error": None,
    }


def load_pid_status(pid_file: Path) -> dict[str, Any]:
    pid_text = pid_file.read_text(encoding="utf-8").strip() if pid_file.exists() else ""
    pid = int(pid_text) if pid_text.isdigit() else None
    running = False
    if pid is not None:
        try:
            import os

            os.kill(pid, 0)
        except OSError:
            running = False
        else:
            running = True
    return {
        "pid_file": str(pid_file),
        "pid": pid,
        "running": running,
    }


def detect_server_port(log_path: Path) -> int | None:
    if not log_path.exists():
        return None
    for line in reversed(log_path.read_text(encoding="utf-8", errors="ignore").splitlines()):
        match = SERVER_PORT_RE.search(line)
        if match is None:
            continue
        return int(match.group(1))
    return None


def detect_server_port_from_value(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = SERVER_PORT_RE.search(value)
    if match is None:
        return None
    return int(match.group(1))


def collect_active_manifest_counts() -> dict[str, int]:
    proc = subprocess.run(["ps", "-eo", "args="], check=True, capture_output=True, text=True)
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        match = RUNNER_CMD_RE.search(line)
        if match is None:
            continue
        manifest_path = Path(match.group(1)).resolve()
        try:
            wave_dir = manifest_path.parents[1]
        except IndexError:
            continue
        counts[str(wave_dir)] = counts.get(str(wave_dir), 0) + 1
    return counts


def summarize_regular_wave(wave_dir: Path, active_counts: dict[str, int]) -> dict[str, Any]:
    prep = read_json(wave_dir / "wave_prep_summary.json")
    queue_state = load_queue_state(wave_dir / "combined_queue.binary.state.json")
    live_summary_path = wave_dir / "live_summary.json"
    live_group = (read_json(live_summary_path).get("group") if live_summary_path.exists() else None) or {}
    launcher = load_pid_status(wave_dir / "queue_launcher_binary.pid")
    server_port = detect_server_port(wave_dir / "binary_server.log")
    if server_port is None:
        server_port = detect_server_port_from_value(prep.get("server"))
    return {
        "wave_dir": str(wave_dir),
        "group_file": prep.get("group_file"),
        "server_port": server_port,
        "task_count": int(prep.get("task_count") or live_group.get("total") or 0),
        "attempted": int(live_group.get("attempted") or 0),
        "latest_success": int(live_group.get("latest_success") or 0),
        "latest_status_counts": dict(live_group.get("latest_status_counts") or {}),
        "queue_processed_count": len(queue_state["processed_names"]),
        "queue_processed_tail": queue_state["processed_names"][-5:],
        "queue_complete": queue_state["queue_complete"],
        "queue_updated_at": queue_state["updated_at"],
        "queue_state_read_error": queue_state["read_error"],
        "active_runner_count": active_counts.get(str(wave_dir.resolve()), 0),
        "launcher": launcher,
    }


def summarize_group2_hard(wave_dir: Path, active_counts: dict[str, int]) -> dict[str, Any]:
    snapshot = read_json(wave_dir / "group2_hard_status_snapshot.json")
    consistency_path = wave_dir / "consistency_check.json"
    consistency = read_json(consistency_path) if consistency_path.exists() else None
    launcher = load_pid_status(wave_dir / "queue_launcher_binary.pid")
    return {
        "wave_dir": str(wave_dir),
        "group_file": str(Path("splits/group_02_hard.md").resolve()),
        "server_port": detect_server_port(wave_dir / "binary_server.log"),
        "task_count": int(snapshot.get("hard_total") or 0),
        "attempted": int(snapshot.get("hard_attempted") or 0),
        "latest_success": int(snapshot.get("hard_latest_success") or 0),
        "latest_status_counts": dict(snapshot.get("hard_latest_status_counts") or {}),
        "queue_processed_count": int(snapshot.get("processed_queue_count") or 0),
        "queue_processed_tail": list(snapshot.get("processed_queue_tail") or []),
        "queue_complete": bool(snapshot.get("queue_complete")),
        "queue_updated_at": (snapshot.get("followup_queue") or {}).get("updated_at"),
        "active_runner_count": active_counts.get(str(wave_dir.resolve()), 0),
        "launcher": launcher,
        "followup_launcher": snapshot.get("followup_launcher"),
        "monitor": snapshot.get("monitor"),
        "automation": snapshot.get("automation"),
        "post_followup_queue": snapshot.get("post_followup_queue"),
        "post_followup_launcher": snapshot.get("post_followup_launcher"),
        "followup_usage_limit_halt": snapshot.get("followup_usage_limit_halt"),
        "consistency_check": consistency,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a compact dashboard for active split-group binary waves.")
    parser.add_argument("--reports-root", type=Path, default=Path("reports"))
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()

    active_counts = collect_active_manifest_counts()
    waves: dict[str, dict[str, Any]] = {}
    reports_root = args.reports_root.resolve()

    group2_hard_dir = reports_root / "group2_hard_wave_2026-07-27"
    if group2_hard_dir.exists() and (group2_hard_dir / "group2_hard_status_snapshot.json").exists():
        waves[group2_hard_dir.name] = summarize_group2_hard(group2_hard_dir, active_counts)

    for wave_dir in sorted(reports_root.glob("group*_wave_2026-07-27")):
        if wave_dir.name == "group2_hard_wave_2026-07-27":
            continue
        if not (wave_dir / "wave_prep_summary.json").exists():
            continue
        waves[wave_dir.name] = summarize_regular_wave(wave_dir, active_counts)

    payload = {
        "generated_at": now_utc(),
        "wave_count": len(waves),
        "total_active_runners": sum(item.get("active_runner_count", 0) for item in waves.values()),
        "waves": waves,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output_json": str(args.output_json.resolve()), "wave_count": len(waves)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
