from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = ROOT / ".venv" / "bin" / "python"
DASHBOARD_JSON = ROOT / "reports" / "split_wave_dashboard_2026-07-27.json"
CAMPAIGN_JSON = ROOT / "reports" / "full_benchmark_campaign_2026-07-27.json"
TRAJECTORY_INDEX_JSONL = ROOT / "codex_rescue_runs_local" / "trajectory_index.jsonl"
TRAJECTORY_SUMMARY_JSON = ROOT / "codex_rescue_runs_local" / "trajectory_summary.json"
SPLIT_GROUP_COVERAGE_JSON = ROOT / "reports" / "split_group_coverage_2026-07-27.json"
STUBBORN_FAILURES_JSON = ROOT / "reports" / "stubborn_failures_2026-07-27.json"
STUBBORN_FAILURES_MD = ROOT / "reports" / "stubborn_failures_2026-07-27.md"
STUBBORN_TASK_SOURCE_JSON = ROOT / "reports" / "stubborn_failure_task_source_2026-07-27.json"
STUBBORN_TASK_SOURCE_MD = ROOT / "reports" / "stubborn_failure_task_source_2026-07-27.md"
LAUNCH_SCRIPT = ROOT / "scripts" / "launch_split_group_binary_wave.sh"
RETRY_LAUNCH_SCRIPT = ROOT / "scripts" / "launch_split_group_retry_wave.sh"
GROUP2_HARD_MONITOR_CONTROL = ROOT / "scripts" / "control_group2_hard_monitor.sh"
GROUP2_HARD_FOLLOWUP_CONTROL = ROOT / "scripts" / "control_group2_hard_followup.sh"
GROUP2_HARD_POST_CONTROL = ROOT / "scripts" / "control_group2_hard_post_followup.sh"
LOG_PATH = ROOT / "codex_rescue_runs_local" / "split_wave_autopilot.log"
STATE_PATH = ROOT / "reports" / "split_wave_autopilot_state.json"
LOCK_PATH = ROOT / "reports" / "split_wave_autopilot.lock"
TRIM_RESULTS_ROOT = ROOT / "codex_rescue_runs_local"
TRIM_INTERVAL_SECONDS = 1800
TRIM_LIMIT = 40
TRIM_MIN_AGE_MINUTES = 20

BASE_TARGETS = {
    "group_01.md": 6,
    "group_02.md": 12,
    "group_03.md": 3,
    "group_04.md": 12,
    "group_05.md": 16,
    "group_06.md": 11,
    "group_07.md": 9,
    "group_08.md": 3,
    "group_09.md": 8,
    "group_10.md": 18,
}
MIN_TARGETS = {
    "group_01.md": 4,
    "group_02.md": 8,
    "group_03.md": 2,
    "group_04.md": 8,
    "group_05.md": 12,
    "group_06.md": 8,
    "group_07.md": 6,
    "group_08.md": 2,
    "group_09.md": 4,
    "group_10.md": 14,
}
MAX_TARGETS = {
    "group_01.md": 10,
    "group_02.md": 14,
    "group_03.md": 4,
    "group_04.md": 16,
    "group_05.md": 22,
    "group_06.md": 14,
    "group_07.md": 12,
    "group_08.md": 4,
    "group_09.md": 12,
    "group_10.md": 24,
}

SPLIT_GROUP_GLOBAL_MAX_ACTIVE = int(os.environ.get("SPLIT_GROUP_GLOBAL_MAX_ACTIVE", "16"))
SPLIT_GROUP_GLOBAL_RUNNER_HEADROOM = int(os.environ.get("SPLIT_GROUP_GLOBAL_RUNNER_HEADROOM", "2"))
SPLIT_GROUP_LOADAVG_LIMIT_FACTOR = float(os.environ.get("SPLIT_GROUP_LOADAVG_LIMIT_FACTOR", "1.25"))
SPLIT_GROUP_MIN_MEM_AVAILABLE_MB = int(os.environ.get("SPLIT_GROUP_MIN_MEM_AVAILABLE_MB", "4096"))
SPLIT_GROUP_MIN_SWAP_FREE_MB = int(os.environ.get("SPLIT_GROUP_MIN_SWAP_FREE_MB", "512"))
GROUP2_HARD_GLOBAL_MAX_ACTIVE = int(os.environ.get("GROUP2_HARD_GLOBAL_MAX_ACTIVE", "16"))
GROUP2_HARD_GLOBAL_RUNNER_HEADROOM = int(os.environ.get("GROUP2_HARD_GLOBAL_RUNNER_HEADROOM", "2"))
GROUP2_HARD_LOADAVG_LIMIT_FACTOR = float(os.environ.get("GROUP2_HARD_LOADAVG_LIMIT_FACTOR", "1.25"))
GROUP2_HARD_MIN_MEM_AVAILABLE_MB = int(os.environ.get("GROUP2_HARD_MIN_MEM_AVAILABLE_MB", "4096"))
GROUP2_HARD_MIN_SWAP_FREE_MB = int(os.environ.get("GROUP2_HARD_MIN_SWAP_FREE_MB", "512"))
SPLIT_WAVE_PRESSURE_MAX_ACTIVE_WAVES = int(os.environ.get("SPLIT_WAVE_PRESSURE_MAX_ACTIVE_WAVES", "5"))
SPLIT_WAVE_PRESSURE_SWAP_FREE_MB = int(os.environ.get("SPLIT_WAVE_PRESSURE_SWAP_FREE_MB", str(SPLIT_GROUP_MIN_SWAP_FREE_MB)))
SPLIT_WAVE_PRESSURE_MIN_MEM_AVAILABLE_MB = int(
    os.environ.get("SPLIT_WAVE_PRESSURE_MIN_MEM_AVAILABLE_MB", str(SPLIT_GROUP_MIN_MEM_AVAILABLE_MB))
)
SPLIT_WAVE_PRESSURE_LOADAVG_LIMIT_FACTOR = float(
    os.environ.get("SPLIT_WAVE_PRESSURE_LOADAVG_LIMIT_FACTOR", str(SPLIT_GROUP_LOADAVG_LIMIT_FACTOR))
)
SPLIT_WAVE_PRESSURE_TOTAL_TARGET_BUDGET = int(
    os.environ.get("SPLIT_WAVE_PRESSURE_TOTAL_TARGET_BUDGET", str(min(SPLIT_GROUP_GLOBAL_MAX_ACTIVE, 12)))
)
SPLIT_WAVE_PRESSURE_MIN_TARGET_PER_WAVE = int(os.environ.get("SPLIT_WAVE_PRESSURE_MIN_TARGET_PER_WAVE", "1"))

MISSING_WAVE_SPECS = {
    "group_01.md": {
        "wave_name": "group1_wave_2026-07-27",
        "group_file": str((ROOT / "splits" / "group_01.md").resolve()),
        "server_port": 18670,
    }
}


@dataclass(frozen=True)
class LauncherProcess:
    pid: int
    wave_name: str
    max_active_runners: int | None
    global_max_active_runners: int | None
    global_runner_headroom: int | None
    loadavg_limit_factor: float | None
    min_mem_available_mb: int | None
    min_swap_free_mb: int | None
    args: str


@dataclass(frozen=True)
class WaveTarget:
    wave_name: str
    canonical_group: str
    group_file: str
    server_port: int
    target_max_active: int
    launch_mode: str
    reason: str


@dataclass(frozen=True)
class PressureSnapshot:
    cpu_count: int | None
    loadavg_1m: float | None
    mem_available_mb: float | None
    swap_free_mb: float | None


def read_pressure_snapshot() -> PressureSnapshot:
    cpu_count = os.cpu_count()
    loadavg_1m: float | None = None
    mem_available_mb: float | None = None
    swap_free_mb: float | None = None

    try:
        loadavg_1m = float(Path("/proc/loadavg").read_text(encoding="utf-8").split()[0])
    except (IndexError, OSError, ValueError):
        loadavg_1m = None

    try:
        meminfo_lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        meminfo_lines = []
    meminfo: dict[str, int] = {}
    for line in meminfo_lines:
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        value_parts = raw_value.strip().split()
        if not value_parts:
            continue
        try:
            meminfo[key] = int(value_parts[0])
        except ValueError:
            continue
    if "MemAvailable" in meminfo:
        mem_available_mb = meminfo["MemAvailable"] / 1024.0
    if "SwapFree" in meminfo:
        swap_free_mb = meminfo["SwapFree"] / 1024.0
    return PressureSnapshot(
        cpu_count=cpu_count,
        loadavg_1m=loadavg_1m,
        mem_available_mb=mem_available_mb,
        swap_free_mb=swap_free_mb,
    )


def pressure_reason(snapshot: PressureSnapshot) -> str | None:
    if snapshot.swap_free_mb is not None and snapshot.swap_free_mb < SPLIT_WAVE_PRESSURE_SWAP_FREE_MB:
        return "swap_pressure"
    if snapshot.mem_available_mb is not None and snapshot.mem_available_mb < SPLIT_WAVE_PRESSURE_MIN_MEM_AVAILABLE_MB:
        return "memory_pressure"
    if (
        snapshot.cpu_count
        and snapshot.loadavg_1m is not None
        and snapshot.loadavg_1m > snapshot.cpu_count * SPLIT_WAVE_PRESSURE_LOADAVG_LIMIT_FACTOR
    ):
        return "load_pressure"
    return None


def wave_priority_score(
    target: WaveTarget,
    campaign_row: dict[str, Any],
    frontier_priority: list[str],
    wave_row: dict[str, Any] | None = None,
) -> float:
    metrics = resolve_wave_progress_metrics(campaign_row=campaign_row, wave_row=wave_row)
    total = metrics["total"]
    attempted = metrics["attempted"]
    success = metrics["success"]
    remaining = metrics["remaining"]
    unattempted = metrics["unattempted"]
    success_rate = (success / attempted) if attempted else 0.0
    attempted_rate = (attempted / total) if total else 0.0
    frontier_rank = frontier_priority.index(target.canonical_group) if target.canonical_group in frontier_priority else 999

    score = success_rate * remaining
    score += min(unattempted, 40) * 0.35

    if remaining <= 15:
        score += 30.0
    elif remaining <= 30:
        score += 15.0

    if frontier_rank == 0:
        score += 8.0
    elif frontier_rank <= 2:
        score += 6.0
    elif frontier_rank == 3:
        score += 4.0
    elif frontier_rank == 4:
        score += 2.0
    elif frontier_rank <= 6:
        score += 1.0

    if target.launch_mode == "fresh" and unattempted > 0:
        score += 3.0
    elif target.launch_mode == "retry" and success_rate >= 0.5:
        score += 1.5

    if attempted_rate < 0.75 and success_rate >= 0.5:
        score += 4.0
    elif attempted_rate < 0.9 and success_rate >= 0.45:
        score += 2.0

    if attempted_rate >= 0.95 and success_rate < 0.25:
        score -= 8.0
    elif attempted_rate >= 0.95 and success_rate < 0.35:
        score -= 4.0
    elif attempted_rate >= 0.85 and success_rate < 0.3:
        score -= 8.0
    elif attempted_rate >= 0.85 and success_rate < 0.35:
        score -= 4.0

    return score


def resolve_wave_progress_metrics(*, campaign_row: dict[str, Any], wave_row: dict[str, Any] | None) -> dict[str, int]:
    total = int(campaign_row.get("total") or 0)
    attempted = int(campaign_row.get("attempted_count") or 0)
    success = int(campaign_row.get("success_count") or 0)
    remaining = int(campaign_row.get("remaining_count") or 0)
    unattempted = int(campaign_row.get("unattempted_count") or max(total - attempted, 0))

    if wave_row:
        wave_task_count = int(wave_row.get("task_count") or 0)
        latest_status_counts = dict(wave_row.get("latest_status_counts") or {})
        wave_success = int(
            wave_row.get("latest_success")
            or latest_status_counts.get("success")
            or 0
        )
        wave_unattempted = int(latest_status_counts.get("unattempted") or 0)
        wave_attempted = int(wave_row.get("attempted") or max(wave_task_count - wave_unattempted, 0))
        wave_remaining = max(wave_task_count - wave_success, 0)
        if wave_task_count > 0:
            total = wave_task_count
            attempted = min(wave_attempted, total)
            success = min(wave_success, total)
            remaining = min(wave_remaining, total)
            unattempted = min(max(wave_unattempted, total - attempted), total)

    return {
        "total": total,
        "attempted": attempted,
        "success": success,
        "remaining": remaining,
        "unattempted": unattempted,
    }


def apply_pressure_wave_limits(
    targets: list[WaveTarget],
    campaign_rows: dict[str, dict[str, Any]],
    frontier_priority: list[str],
    wave_rows: dict[str, dict[str, Any]],
    snapshot: PressureSnapshot,
) -> tuple[list[WaveTarget], dict[str, Any] | None]:
    reason = pressure_reason(snapshot)
    if reason is None:
        return targets, None

    scored_targets: list[tuple[float, int, str, WaveTarget]] = []
    for target in targets:
        if target.target_max_active <= 0:
            continue
        campaign_row = campaign_rows.get(target.canonical_group) or {}
        frontier_rank = frontier_priority.index(target.canonical_group) if target.canonical_group in frontier_priority else 999
        score = wave_priority_score(target, campaign_row, frontier_priority, wave_rows.get(target.wave_name) or {})
        scored_targets.append((score, frontier_rank, target.wave_name, target))
    scored_targets.sort(key=lambda item: (-item[0], item[1], item[2]))

    keep_count = max(1, SPLIT_WAVE_PRESSURE_MAX_ACTIVE_WAVES)
    kept_targets = scored_targets[:keep_count]
    kept_wave_names = {item[3].wave_name for item in kept_targets}
    score_map = {item[3].wave_name: item[0] for item in scored_targets}
    pressure_target_budget = max(SPLIT_WAVE_PRESSURE_MIN_TARGET_PER_WAVE, SPLIT_WAVE_PRESSURE_TOTAL_TARGET_BUDGET)
    pressure_target_caps: dict[str, int] = {}
    if kept_targets:
        min_cap = max(1, SPLIT_WAVE_PRESSURE_MIN_TARGET_PER_WAVE)
        base_caps = {
            item[3].wave_name: min(item[3].target_max_active, min_cap)
            for item in kept_targets
        }
        remaining_budget = max(0, pressure_target_budget - sum(base_caps.values()))
        if remaining_budget > 0:
            weights = [1.0 / ((index + 1) ** 0.5) for index in range(len(kept_targets))]
            weight_total = sum(weights) or 1.0
            raw_extras = [remaining_budget * (weight / weight_total) for weight in weights]
            extra_caps = [int(value) for value in raw_extras]
            extra_budget = remaining_budget - sum(extra_caps)
            for index in sorted(range(len(raw_extras)), key=lambda idx: raw_extras[idx] - extra_caps[idx], reverse=True):
                if extra_budget <= 0:
                    break
                extra_caps[index] += 1
                extra_budget -= 1
            for index, item in enumerate(kept_targets):
                target = item[3]
                pressure_target_caps[target.wave_name] = min(
                    target.target_max_active,
                    base_caps[target.wave_name] + extra_caps[index],
                )
        else:
            pressure_target_caps = dict(base_caps)
    adjusted_targets: list[WaveTarget] = []
    paused_wave_names: list[str] = []
    for target in targets:
        score = score_map.get(target.wave_name)
        if target.target_max_active <= 0 or target.wave_name in kept_wave_names:
            if score is None:
                adjusted_targets.append(target)
            else:
                target_max_active = target.target_max_active
                pressure_cap = pressure_target_caps.get(target.wave_name)
                if pressure_cap is not None:
                    target_max_active = min(target_max_active, pressure_cap)
                adjusted_targets.append(
                    replace(
                        target,
                        target_max_active=target_max_active,
                        reason=(
                            f"{target.reason} pressure={reason} priority_score={score:.2f} "
                            f"pressure_cap={pressure_cap} pressure_action=keep"
                        ),
                    )
                )
            continue
        paused_wave_names.append(target.wave_name)
        adjusted_targets.append(
            replace(
                target,
                target_max_active=0,
                reason=f"{target.reason} pressure={reason} priority_score={score:.2f} pressure_action=pause",
            )
        )
    return adjusted_targets, {
        "reason": reason,
        "cpu_count": snapshot.cpu_count,
        "loadavg_1m": snapshot.loadavg_1m,
        "mem_available_mb": snapshot.mem_available_mb,
        "swap_free_mb": snapshot.swap_free_mb,
        "max_active_waves": keep_count,
        "pressure_target_budget": pressure_target_budget,
        "pressure_target_caps": dict(sorted(pressure_target_caps.items())),
        "kept_waves": sorted(kept_wave_names),
        "paused_waves": sorted(paused_wave_names),
    }


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def append_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_utc()} {message}\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_iso_timestamp(raw_value: Any) -> float | None:
    if not isinstance(raw_value, str) or not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value).timestamp()
    except ValueError:
        return None


def read_wave_group_file(wave_dir: Path) -> str | None:
    prep_summary = wave_dir / "wave_prep_summary.json"
    if not prep_summary.exists():
        return None
    try:
        payload = read_json(prep_summary)
    except (OSError, ValueError):
        return None
    group_file = payload.get("group_file")
    if not group_file:
        return None
    return str(Path(group_file).resolve())


def run_cmd(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def maybe_trim_completed_runs(
    *,
    log_path: Path,
    state_path: Path,
    current_ts: float | None = None,
) -> dict[str, Any]:
    now_ts = time.time() if current_ts is None else current_ts
    previous_trim_at = None
    if state_path.exists():
        try:
            previous_state = read_json(state_path)
        except (OSError, ValueError):
            previous_state = {}
        previous_trim_at = parse_iso_timestamp((previous_state.get("artifact_trim") or {}).get("generated_at"))
    if previous_trim_at is not None and now_ts - previous_trim_at < TRIM_INTERVAL_SECONDS:
        return {
            "action": "skipped",
            "reason": "interval_not_elapsed",
            "interval_seconds": TRIM_INTERVAL_SECONDS,
            "seconds_until_next_trim": max(0, int(TRIM_INTERVAL_SECONDS - (now_ts - previous_trim_at))),
        }

    cmd = [
        str(PYTHON_BIN),
        "scripts/codex_rescue_runner.py",
        "trim-artifacts",
        "--results-root",
        str(TRIM_RESULTS_ROOT),
        "--limit",
        str(TRIM_LIMIT),
        "--min-age-minutes",
        str(TRIM_MIN_AGE_MINUTES),
    ]
    result = run_cmd(cmd, check=False, timeout=1800)
    summary: dict[str, Any] = {}
    for line in reversed(result.stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(payload, dict) and "processed" in payload:
            summary = payload
            break
    if result.stderr.strip():
        append_log(log_path, f"artifact_trim_stderr={result.stderr.strip()}")
    if result.returncode != 0:
        raise RuntimeError(f"artifact trim failed rc={result.returncode}")
    append_log(
        log_path,
        (
            "artifact_trim "
            f"processed={summary.get('processed', 0)} "
            f"updated={summary.get('trimmed_runs', summary.get('updated', 0))} "
            f"errors={summary.get('error_count', 0)}"
        ),
    )
    return {
        "action": "ran",
        "generated_at": now_utc(),
        "limit": TRIM_LIMIT,
        "min_age_minutes": TRIM_MIN_AGE_MINUTES,
        "summary": summary,
    }


def canonical_group_for_wave(group_file: str) -> str:
    name = Path(group_file).name
    if name == "group_02_easy.md":
        return "group_02.md"
    return name


def parse_arg_value(args: str, marker: str, caster: Any) -> Any:
    start = args.find(marker)
    if start < 0:
        return None
    raw_value = args[start + len(marker) :].split(None, 1)[0]
    try:
        return caster(raw_value)
    except (TypeError, ValueError):
        return None


def desired_split_group_launcher_config(target: WaveTarget) -> dict[str, int | float]:
    return {
        "max_active_runners": target.target_max_active,
        "global_max_active_runners": SPLIT_GROUP_GLOBAL_MAX_ACTIVE,
        "global_runner_headroom": SPLIT_GROUP_GLOBAL_RUNNER_HEADROOM,
        "loadavg_limit_factor": SPLIT_GROUP_LOADAVG_LIMIT_FACTOR,
        "min_mem_available_mb": SPLIT_GROUP_MIN_MEM_AVAILABLE_MB,
        "min_swap_free_mb": SPLIT_GROUP_MIN_SWAP_FREE_MB,
    }


def launcher_matches_target(launcher: LauncherProcess, target: WaveTarget) -> bool:
    desired = desired_split_group_launcher_config(target)
    return (
        launcher.max_active_runners == desired["max_active_runners"]
        and launcher.global_max_active_runners == desired["global_max_active_runners"]
        and launcher.global_runner_headroom == desired["global_runner_headroom"]
        and launcher.loadavg_limit_factor == desired["loadavg_limit_factor"]
        and launcher.min_mem_available_mb == desired["min_mem_available_mb"]
        and launcher.min_swap_free_mb == desired["min_swap_free_mb"]
    )


def list_launcher_processes() -> dict[str, list[LauncherProcess]]:
    proc = run_cmd(["ps", "-eo", "pid=,args="])
    waves: dict[str, list[LauncherProcess]] = {}
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line or "scripts/rescue_queue_launcher.py" not in line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, args = parts
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        queue_marker = "/reports/"
        queue_suffix = "/combined_queue.json"
        queue_start = args.find(queue_marker)
        queue_end = args.find(queue_suffix)
        if queue_start < 0 or queue_end < 0 or queue_end <= queue_start:
            continue
        wave_name = args[queue_start + len(queue_marker) : queue_end]
        max_active = parse_arg_value(args, "--max-active-runners ", int)
        global_max_active = parse_arg_value(args, "--global-max-active-runners ", int)
        global_runner_headroom = parse_arg_value(args, "--global-runner-headroom ", int)
        loadavg_limit_factor = parse_arg_value(args, "--loadavg-limit-factor ", float)
        min_mem_available_mb = parse_arg_value(args, "--min-mem-available-mb ", int)
        min_swap_free_mb = parse_arg_value(args, "--min-swap-free-mb ", int)
        waves.setdefault(wave_name, []).append(
            LauncherProcess(
                pid=pid,
                wave_name=wave_name,
                max_active_runners=max_active,
                global_max_active_runners=global_max_active,
                global_runner_headroom=global_runner_headroom,
                loadavg_limit_factor=loadavg_limit_factor,
                min_mem_available_mb=min_mem_available_mb,
                min_swap_free_mb=min_swap_free_mb,
                args=args,
            )
        )
    return waves


def terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def terminate_wave_launchers(wave_name: str, launchers: dict[str, list[LauncherProcess]], log_path: Path) -> None:
    for launcher in launchers.get(wave_name, []):
        append_log(log_path, f"terminate wave={wave_name} pid={launcher.pid}")
        terminate_pid(launcher.pid)


def refresh_views(log_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    refresh_steps = build_refresh_steps()
    for step in refresh_steps:
        result = run_cmd(step, check=False, timeout=1800)
        append_log(log_path, f"refresh_step rc={result.returncode} argv={' '.join(step)}")
        if result.stdout.strip():
            append_log(log_path, f"refresh_stdout={result.stdout.strip()}")
        if result.stderr.strip():
            append_log(log_path, f"refresh_stderr={result.stderr.strip()}")
        if result.returncode != 0:
            raise RuntimeError(f"refresh step failed: {' '.join(step)} rc={result.returncode}")
    if not DASHBOARD_JSON.exists() or not CAMPAIGN_JSON.exists():
        raise FileNotFoundError("dashboard or campaign json missing after refresh")
    return read_json(DASHBOARD_JSON), read_json(CAMPAIGN_JSON)


def build_refresh_steps() -> list[list[str]]:
    refresh_steps = [
        [
            str(PYTHON_BIN),
            "scripts/refresh_rescue_trajectory_index.py",
            "--results-root",
            "./codex_rescue_runs_local",
            "--output-jsonl",
            str(TRAJECTORY_INDEX_JSONL),
            "--summary-json",
            str(TRAJECTORY_SUMMARY_JSON),
        ],
    ]
    reports_root = ROOT / "reports"
    for wave_dir in sorted(reports_root.glob("group*_wave_2026-07-27")):
        if wave_dir.name == "group2_hard_wave_2026-07-27":
            continue
        group_file = read_wave_group_file(wave_dir)
        if group_file is None:
            continue
        refresh_steps.append(
            [
                str(PYTHON_BIN),
                "scripts/summarize_group_results.py",
                "--trajectory-index",
                str(TRAJECTORY_INDEX_JSONL),
                "--group",
                group_file,
                "--output-json",
                str(wave_dir / "live_summary.json"),
            ]
        )
    refresh_steps.extend(
        [
            [
                str(PYTHON_BIN),
                "scripts/export_split_group_coverage.py",
                "--output-json",
                str(SPLIT_GROUP_COVERAGE_JSON),
            ],
            [
                str(PYTHON_BIN),
                "scripts/export_split_wave_dashboard.py",
                "--output-json",
                str(DASHBOARD_JSON),
            ],
            [
                str(PYTHON_BIN),
                "scripts/export_full_benchmark_campaign.py",
                "--dashboard-json",
                str(DASHBOARD_JSON),
                "--coverage-json",
                str(SPLIT_GROUP_COVERAGE_JSON),
                "--output-json",
                str(CAMPAIGN_JSON),
                "--output-md",
                "./reports/full_benchmark_campaign_2026-07-27.md",
            ],
            [
                str(PYTHON_BIN),
                "scripts/export_stubborn_failure_report.py",
                "--trajectory-index",
                str(TRAJECTORY_INDEX_JSONL),
                "--results-root",
                "./codex_rescue_runs_local",
                "--output-json",
                str(STUBBORN_FAILURES_JSON),
                "--output-md",
                str(STUBBORN_FAILURES_MD),
                "--min-attempts",
                "3",
                "--limit",
                "100",
            ],
            [
                str(PYTHON_BIN),
                "scripts/export_stubborn_failure_task_source.py",
                "--input-json",
                str(STUBBORN_FAILURES_JSON),
                "--output-json",
                str(STUBBORN_TASK_SOURCE_JSON),
                "--output-md",
                str(STUBBORN_TASK_SOURCE_MD),
            ],
        ]
    )
    return refresh_steps


def desired_target_for_wave(
    wave_name: str,
    wave_row: dict[str, Any],
    campaign_rows: dict[str, dict[str, Any]],
    frontier_priority: list[str],
) -> WaveTarget | None:
    if wave_name == "group2_hard_wave_2026-07-27":
        return None
    group_file = str(wave_row.get("group_file") or "")
    if not group_file:
        return None
    canonical_group = canonical_group_for_wave(group_file)
    campaign_row = campaign_rows.get(canonical_group)
    if campaign_row is None:
        return None
    metrics = resolve_wave_progress_metrics(campaign_row=campaign_row, wave_row=wave_row)
    total = metrics["total"]
    attempted = metrics["attempted"]
    success = metrics["success"]
    remaining = metrics["remaining"]
    unattempted = metrics["unattempted"]
    attempted_rate = (attempted / total) if total else 0.0
    state = str(campaign_row.get("state") or "")
    queue_complete = bool(wave_row.get("queue_complete"))
    launch_mode = "retry" if unattempted <= 0 or state in {"historical_revisit", "wave_revisit"} else "fresh"
    if remaining <= 0:
        target = 0
        reason = "completed_or_frozen"
    else:
        base = BASE_TARGETS.get(
            canonical_group,
            8 if state in {"not_started", "historical_partial", "historical_revisit", "wave_active", "wave_revisit"} else 0,
        )
        minimum = MIN_TARGETS.get(canonical_group, 2 if base else 0)
        maximum = MAX_TARGETS.get(canonical_group, max(base, minimum))
        frontier_rank = frontier_priority.index(canonical_group) if canonical_group in frontier_priority else 999
        success_rate = (success / attempted) if attempted else 0.0
        remaining_ratio = (remaining / total) if total else 0.0
        bonus = 0
        if frontier_rank == 0:
            bonus += 2
        elif frontier_rank <= 2:
            bonus += 1
        if unattempted > 0 and success_rate >= 0.5 and remaining_ratio >= 0.5:
            bonus += 1
        if attempted_rate < 0.4 and success_rate >= 0.45:
            bonus += 1
        if attempted_rate < 0.7 and success_rate >= 0.58:
            bonus += 2
        elif attempted_rate < 0.85 and success_rate >= 0.5:
            bonus += 1
        if attempted_rate >= 0.95 and success_rate < 0.25:
            bonus -= 3
        elif attempted_rate >= 0.95 and success_rate < 0.3:
            bonus -= 2
        elif success_rate < 0.2 and unattempted <= 0:
            bonus -= 1
        if queue_complete and unattempted > 0 and success_rate >= 0.5:
            bonus += 1
        target = max(minimum, min(maximum, base + bonus))
        reason = (
            f"frontier_rank={frontier_rank} success_rate={success_rate:.3f} "
            f"remaining_ratio={remaining_ratio:.3f} attempted_rate={attempted_rate:.3f} unattempted={unattempted} "
            f"launch_mode={launch_mode} base={base} bonus={bonus}"
        )
    server_port = int(wave_row.get("server_port") or 0)
    if server_port <= 0:
        return None
    return WaveTarget(
        wave_name=wave_name,
        canonical_group=canonical_group,
        group_file=group_file,
        server_port=server_port,
        target_max_active=target,
        launch_mode=launch_mode,
        reason=reason,
    )


def launch_wave(target: WaveTarget, log_path: Path) -> None:
    env = os.environ.copy()
    env["SPLIT_GROUP_MAX_ACTIVE"] = str(target.target_max_active)
    env["SPLIT_GROUP_POLL_SECONDS"] = "10"
    env["SPLIT_GROUP_BINARY_PORT"] = str(target.server_port)
    env["SPLIT_GROUP_GLOBAL_MAX_ACTIVE"] = str(SPLIT_GROUP_GLOBAL_MAX_ACTIVE)
    env["SPLIT_GROUP_GLOBAL_RUNNER_HEADROOM"] = str(SPLIT_GROUP_GLOBAL_RUNNER_HEADROOM)
    env["SPLIT_GROUP_LOADAVG_LIMIT_FACTOR"] = str(SPLIT_GROUP_LOADAVG_LIMIT_FACTOR)
    env["SPLIT_GROUP_MIN_MEM_AVAILABLE_MB"] = str(SPLIT_GROUP_MIN_MEM_AVAILABLE_MB)
    env["SPLIT_GROUP_MIN_SWAP_FREE_MB"] = str(SPLIT_GROUP_MIN_SWAP_FREE_MB)
    append_log(
        log_path,
        (
            f"launch wave={target.wave_name} canonical_group={target.canonical_group} "
            f"target={target.target_max_active} port={target.server_port} "
            f"launch_mode={target.launch_mode} reason={target.reason}"
        ),
    )
    launch_script = RETRY_LAUNCH_SCRIPT if target.launch_mode == "retry" else LAUNCH_SCRIPT
    result = run_cmd(
        ["bash", str(launch_script), target.group_file, target.wave_name],
        env=env,
        check=False,
        timeout=1800,
    )
    if result.stdout.strip():
        append_log(log_path, f"launch_stdout[{target.wave_name}]={result.stdout.strip()}")
    if result.stderr.strip():
        append_log(log_path, f"launch_stderr[{target.wave_name}]={result.stderr.strip()}")
    if result.returncode != 0:
        raise RuntimeError(f"launch failed for {target.wave_name} rc={result.returncode}")


def ensure_missing_waves(campaign: dict[str, Any], dashboard: dict[str, Any], log_path: Path) -> list[dict[str, Any]]:
    campaign_rows = {
        str(row.get("group_file")): dict(row)
        for row in (campaign.get("group_rows") or [])
        if row.get("group_file")
    }
    existing_waves = set((dashboard.get("waves") or {}).keys())
    actions: list[dict[str, Any]] = []
    for canonical_group, spec in MISSING_WAVE_SPECS.items():
        wave_name = str(spec["wave_name"])
        if wave_name in existing_waves:
            continue
        row = campaign_rows.get(canonical_group) or {}
        remaining = int(row.get("remaining_count") or 0)
        total = int(row.get("total") or 0)
        attempted = int(row.get("attempted_count") or 0)
        if remaining <= 0 or total <= 0:
            continue
        launch_mode = "retry" if attempted >= total else "fresh"
        target = WaveTarget(
            wave_name=wave_name,
            canonical_group=canonical_group,
            group_file=str(spec["group_file"]),
            server_port=int(spec["server_port"]),
            target_max_active=BASE_TARGETS.get(canonical_group, 6),
            launch_mode=launch_mode,
            reason="missing_wave_with_remaining_unsolved_tasks",
        )
        launch_wave(target, log_path)
        actions.append(
            {
                "wave_name": target.wave_name,
                "canonical_group": canonical_group,
                "launch_mode": launch_mode,
                "target_max_active": target.target_max_active,
            }
        )
    return actions


def ensure_group2_hard_automation(dashboard: dict[str, Any], log_path: Path) -> list[dict[str, Any]]:
    wave_row = dict((dashboard.get("waves") or {}).get("group2_hard_wave_2026-07-27") or {})
    if not wave_row:
        return []
    automation = dict(wave_row.get("automation") or {})
    env = os.environ.copy()
    env["GROUP2_HARD_IGNORE_USAGE_LIMIT"] = "1"
    env["GROUP2_HARD_GLOBAL_MAX_ACTIVE"] = str(GROUP2_HARD_GLOBAL_MAX_ACTIVE)
    env["GROUP2_HARD_GLOBAL_RUNNER_HEADROOM"] = str(GROUP2_HARD_GLOBAL_RUNNER_HEADROOM)
    env["GROUP2_HARD_LOADAVG_LIMIT_FACTOR"] = str(GROUP2_HARD_LOADAVG_LIMIT_FACTOR)
    env["GROUP2_HARD_MIN_MEM_AVAILABLE_MB"] = str(GROUP2_HARD_MIN_MEM_AVAILABLE_MB)
    env["GROUP2_HARD_MIN_SWAP_FREE_MB"] = str(GROUP2_HARD_MIN_SWAP_FREE_MB)
    actions: list[dict[str, Any]] = []

    def run_group2_control(name: str, argv: list[str]) -> None:
        append_log(log_path, f"group2_hard_action name={name} argv={' '.join(argv)}")
        result = run_cmd(argv, env=env, check=False, timeout=1800)
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if stdout:
            append_log(log_path, f"group2_hard_stdout[{name}]={stdout}")
        if stderr:
            append_log(log_path, f"group2_hard_stderr[{name}]={stderr}")
        if result.returncode != 0:
            raise RuntimeError(f"group2_hard action failed name={name} rc={result.returncode}")
        actions.append({"action": name, "argv": argv})

    if not bool(automation.get("monitor_running")):
        run_group2_control("resume_monitor", ["bash", str(GROUP2_HARD_MONITOR_CONTROL), "resume"])
        run_group2_control("refresh_after_resume_monitor", ["bash", str(GROUP2_HARD_FOLLOWUP_CONTROL), "refresh"])
        return actions

    next_auto_action = str(automation.get("next_auto_action") or "")
    if next_auto_action == "resume_followup":
        run_group2_control("resume_followup", ["bash", str(GROUP2_HARD_FOLLOWUP_CONTROL), "resume"])
        run_group2_control("refresh_after_resume_followup", ["bash", str(GROUP2_HARD_FOLLOWUP_CONTROL), "refresh"])
    elif next_auto_action == "start_post_followup":
        run_group2_control("start_post_followup", ["bash", str(GROUP2_HARD_POST_CONTROL), "resume"])
        run_group2_control("refresh_after_start_post_followup", ["bash", str(GROUP2_HARD_FOLLOWUP_CONTROL), "refresh"])
    return actions


def rebalance_once(*, log_path: Path, state_path: Path) -> dict[str, Any]:
    try:
        artifact_trim = maybe_trim_completed_runs(log_path=log_path, state_path=state_path)
    except Exception as exc:  # noqa: BLE001
        append_log(log_path, f"artifact_trim_error error={exc}")
        artifact_trim = {"action": "error", "error": str(exc)}
    dashboard, campaign = refresh_views(log_path)
    missing_wave_actions = ensure_missing_waves(campaign, dashboard, log_path)
    group2_hard_actions = ensure_group2_hard_automation(dashboard, log_path)
    launchers = list_launcher_processes()
    frontier_priority = list(campaign.get("frontier_priority") or [])
    campaign_rows = {
        str(row.get("group_file")): dict(row)
        for row in (campaign.get("group_rows") or [])
        if row.get("group_file")
    }
    wave_rows = dict(dashboard.get("waves") or {})
    pressure_snapshot = read_pressure_snapshot()
    desired_targets: list[WaveTarget] = []
    for wave_name, wave_row in sorted(wave_rows.items()):
        target = desired_target_for_wave(wave_name, dict(wave_row), campaign_rows, frontier_priority)
        if target is not None:
            desired_targets.append(target)
    desired_targets, pressure_control = apply_pressure_wave_limits(
        desired_targets,
        campaign_rows,
        frontier_priority,
        wave_rows,
        pressure_snapshot,
    )
    target_by_wave = {target.wave_name: target for target in desired_targets}
    if pressure_control is not None:
        append_log(
            log_path,
            (
                "pressure_control "
                f"reason={pressure_control['reason']} "
                f"swap_free_mb={pressure_control['swap_free_mb']} "
                f"mem_available_mb={pressure_control['mem_available_mb']} "
                f"loadavg_1m={pressure_control['loadavg_1m']} "
                f"kept_waves={','.join(pressure_control['kept_waves']) or 'none'} "
                f"paused_waves={','.join(pressure_control['paused_waves']) or 'none'}"
            ),
        )
    decisions: list[dict[str, Any]] = []
    for wave_name, wave_row in sorted(wave_rows.items()):
        target = target_by_wave.get(wave_name)
        if target is None:
            continue
        current_launchers = launchers.get(wave_name, [])
        matching_launchers = [launcher for launcher in current_launchers if launcher_matches_target(launcher, target)]
        current_caps = sorted({item.max_active_runners for item in current_launchers if item.max_active_runners is not None})
        current_cap = current_caps[0] if len(current_caps) == 1 else None
        running = bool(current_launchers)
        action = "noop"
        if target.target_max_active <= 0:
            if running:
                terminate_wave_launchers(wave_name, launchers, log_path)
                action = "paused"
            else:
                action = "already_paused"
        elif not running or len(current_launchers) != 1 or len(matching_launchers) != 1:
            terminate_wave_launchers(wave_name, launchers, log_path)
            launch_wave(target, log_path)
            action = "restarted" if running else "started"
        desired_config = desired_split_group_launcher_config(target)
        decisions.append(
            {
                "wave_name": wave_name,
                "canonical_group": target.canonical_group,
                "target_max_active": target.target_max_active,
                "current_cap": current_cap,
                "desired_global_max_active": desired_config["global_max_active_runners"],
                "config_match_count": len(matching_launchers),
                "running": running,
                "action": action,
                "reason": target.reason,
            }
        )
    payload = {
        "generated_at": now_utc(),
        "benchmark": campaign.get("benchmark"),
        "live_operations": campaign.get("live_operations"),
        "decisions": decisions,
        "missing_wave_actions": missing_wave_actions,
        "group2_hard_actions": group2_hard_actions,
        "artifact_trim": artifact_trim,
        "pressure_snapshot": {
            "cpu_count": pressure_snapshot.cpu_count,
            "loadavg_1m": pressure_snapshot.loadavg_1m,
            "mem_available_mb": pressure_snapshot.mem_available_mb,
            "swap_free_mb": pressure_snapshot.swap_free_mb,
        },
        "pressure_control": pressure_control,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    append_log(
        log_path,
        (
            f"cycle_complete decisions={len(decisions)} "
            f"success_tasks={payload['benchmark']['success_tasks']} "
            f"remaining_tasks={payload['benchmark']['remaining_tasks']}"
        ),
    )
    return payload


def acquire_lock(lock_path: Path) -> Any:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, BlockingIOError):
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autonomously rebalance binary-only split waves for broad benchmark score grinding.")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--log-path", type=Path, default=LOG_PATH)
    parser.add_argument("--state-path", type=Path, default=STATE_PATH)
    parser.add_argument("--lock-path", type=Path, default=LOCK_PATH)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lock_handle = acquire_lock(args.lock_path)
    if lock_handle is None:
        print(f"autopilot already running: {args.lock_path}", file=sys.stderr)
        return 1
    append_log(args.log_path, f"autopilot_start pid={os.getpid()} once={args.once}")
    try:
        while True:
            try:
                rebalance_once(log_path=args.log_path, state_path=args.state_path)
            except Exception as exc:  # noqa: BLE001
                append_log(args.log_path, f"cycle_error error={exc}")
            if args.once:
                return 0
            time.sleep(max(args.interval_seconds, 60))
    finally:
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
