from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = ROOT / ".venv" / "bin" / "python"
GROUP2_HARD_WAVE_DIR = ROOT / "reports" / "group2_hard_wave_2026-07-27"
GROUP2_HARD_SNAPSHOT_JSON = GROUP2_HARD_WAVE_DIR / "group2_hard_status_snapshot.json"


def run_cmd(args: list[str]) -> None:
    subprocess.run(args, check=True, cwd=ROOT)


def read_wave_group_file(wave_dir: Path) -> str | None:
    prep_summary = wave_dir / "wave_prep_summary.json"
    if not prep_summary.exists():
        return None
    try:
        import json

        payload = json.loads(prep_summary.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    group_file = payload.get("group_file")
    if not group_file:
        return None
    try:
        return str(Path(group_file).resolve().relative_to(ROOT))
    except ValueError:
        return str(Path(group_file).resolve())


def sync_group2_hard_monitor_pid() -> None:
    monitor_pid_file = ROOT / "reports" / "group2_hard_wave_2026-07-27" / "monitor.pid"
    proc = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    for line in proc.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid, args = parts
        if "scripts/monitor_group2_hard_wave.sh" not in args:
            continue
        monitor_pid_file.write_text(f"{pid}\n", encoding="utf-8")
        break


def refresh_trajectory_index() -> None:
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/refresh_rescue_trajectory_index.py",
            "--results-root",
            "./codex_rescue_runs_local",
            "--output-jsonl",
            "./codex_rescue_runs_local/trajectory_index.jsonl",
            "--summary-json",
            "./codex_rescue_runs_local/trajectory_summary.json",
        ]
    )


def refresh_group2_hard() -> None:
    sync_group2_hard_monitor_pid()
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/summarize_group_results.py",
            "--trajectory-index",
            "./codex_rescue_runs_local/trajectory_index.jsonl",
            "--group",
            "./splits/group_02.md",
            "--easy-group",
            "./splits/group_02_easy.md",
            "--hard-group",
            "./splits/group_02_hard.md",
            "--output-json",
            "./reports/group2_hard_wave_2026-07-27/group2_live_summary.json",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/summarize_group2_hard_binary_wave.py",
            "--live-summary",
            "./reports/group2_hard_wave_2026-07-27/group2_live_summary.json",
            "--pocdb-path",
            "./server_poc_group2_binary/poc.db",
            "--state-file",
            "./reports/group2_hard_wave_2026-07-27/combined_queue.binary.state.json",
            "--retry-dir",
            "./reports/group2_hard_wave_2026-07-27/retry_after_binary_fix",
            "--followup-queue-file",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.json",
            "--followup-state-file",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.state.json",
            "--followup-launcher-pid-file",
            "./reports/group2_hard_wave_2026-07-27/followup_queue_launcher.pid",
            "--followup-scheduler-log",
            "./codex_rescue_runs_local/group2_hard_wave_20260727_followup_scheduler_binary.log",
            "--post-main-preview-queue-file",
            "./reports/group2_hard_wave_2026-07-27/post_main_static_retry_queue.json",
            "--post-main-preview-summary-file",
            "./reports/group2_hard_wave_2026-07-27/post_main_static_retry_summary.json",
            "--output-json",
            "./reports/group2_hard_wave_2026-07-27/group2_binary_wave_status.json",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_group2_active_runner_metrics.py",
            "--wave-dir",
            "./reports/group2_hard_wave_2026-07-27",
            "--runs-dir",
            "./codex_rescue_runs_local/group2_hard_wave_20260727_single",
            "--pocdb-path",
            "./server_poc_group2_binary/poc.db",
            "--output-json",
            "./reports/group2_hard_wave_2026-07-27/group2_active_runner_metrics.json",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_group2_intervention_watch.py",
            "--wave-dir",
            "./reports/group2_hard_wave_2026-07-27",
            "--runs-dir",
            "./codex_rescue_runs_local/group2_hard_wave_20260727_single",
            "--pocdb-path",
            "./server_poc_group2_binary/poc.db",
            "--output-json",
            "./reports/group2_hard_wave_2026-07-27/group2_intervention_watch.json",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/backfill_queue_state_task_ids.py",
            "--queue-file",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.json",
            "--state-file",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.state.json",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/backfill_queue_state_task_ids.py",
            "--queue-file",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.json",
            "--state-file",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.state.json",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_group2_hard_pending_followup.py",
            "--queue-file",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.json",
            "--state-file",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.state.json",
            "--summary-file",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_summary.json",
            "--output-json",
            "./reports/group2_hard_wave_2026-07-27/followup_pending_tasks.json",
            "--output-tsv",
            "./reports/group2_hard_wave_2026-07-27/followup_pending_tasks.tsv",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_group2_hard_status_snapshot.py",
            "--live-summary",
            "./reports/group2_hard_wave_2026-07-27/group2_live_summary.json",
            "--binary-wave-status",
            "./reports/group2_hard_wave_2026-07-27/group2_binary_wave_status.json",
            "--queue-state",
            "./reports/group2_hard_wave_2026-07-27/combined_queue.binary.state.json",
            "--queue-file",
            "./reports/group2_hard_wave_2026-07-27/combined_queue.json",
            "--followup-queue-state",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.state.json",
            "--followup-queue-file",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.json",
            "--post-followup-queue-state",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.state.json",
            "--post-followup-queue-file",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.json",
            "--active-runner-metrics",
            "./reports/group2_hard_wave_2026-07-27/group2_active_runner_metrics.json",
            "--monitor-pid-file",
            "./reports/group2_hard_wave_2026-07-27/monitor.pid",
            "--post-followup-launcher-pid-file",
            "./reports/group2_hard_wave_2026-07-27/post_followup_queue_launcher.pid",
            "--output-json",
            "./reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.intermediate.json",
            "--output-tsv",
            "./reports/group2_hard_wave_2026-07-27/group2_hard_task_status.intermediate.tsv",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_group2_hard_post_followup_retry.py",
            "--snapshot",
            "./reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.intermediate.json",
            "--snapshot-label",
            str(GROUP2_HARD_SNAPSHOT_JSON),
            "--pending-followup",
            "./reports/group2_hard_wave_2026-07-27/followup_pending_tasks.json",
            "--trajectory-index",
            "./codex_rescue_runs_local/trajectory_index.jsonl",
            "--results-root",
            "./codex_rescue_runs_local",
            "--live-summary",
            "./reports/group2_hard_wave_2026-07-27/group2_live_summary.json",
            "--output-exhausted-json",
            "./reports/group2_hard_wave_2026-07-27/post_followup_exhausted_tasks.json",
            "--output-exhausted-tsv",
            "./reports/group2_hard_wave_2026-07-27/post_followup_exhausted_tasks.tsv",
            "--output-manifest-dir",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_manifests",
            "--output-queue-file",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.json",
            "--output-summary-json",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_summary.json",
            "--output-root",
            "./codex_rescue_runs_local/group2_hard_wave_20260727_single",
            "--server",
            "http://127.0.0.1:18668",
            "--data-dir",
            "./cybergym_data/data",
            "--difficulty",
            "level1",
            "--campaign",
            "group2_hard_post_followup_static_retry",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_group2_hard_status_snapshot.py",
            "--live-summary",
            "./reports/group2_hard_wave_2026-07-27/group2_live_summary.json",
            "--binary-wave-status",
            "./reports/group2_hard_wave_2026-07-27/group2_binary_wave_status.json",
            "--queue-state",
            "./reports/group2_hard_wave_2026-07-27/combined_queue.binary.state.json",
            "--queue-file",
            "./reports/group2_hard_wave_2026-07-27/combined_queue.json",
            "--followup-queue-state",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.state.json",
            "--followup-queue-file",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.json",
            "--post-followup-queue-state",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.state.json",
            "--post-followup-queue-file",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.json",
            "--active-runner-metrics",
            "./reports/group2_hard_wave_2026-07-27/group2_active_runner_metrics.json",
            "--monitor-pid-file",
            "./reports/group2_hard_wave_2026-07-27/monitor.pid",
            "--post-followup-launcher-pid-file",
            "./reports/group2_hard_wave_2026-07-27/post_followup_queue_launcher.pid",
            "--output-json",
            "./reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.json",
            "--output-tsv",
            "./reports/group2_hard_wave_2026-07-27/group2_hard_task_status.tsv",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_group2_hard_unsolved_tasks.py",
            "--snapshot",
            "./reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.json",
            "--output-json",
            "./reports/group2_hard_wave_2026-07-27/unsolved_tasks.json",
            "--output-tsv",
            "./reports/group2_hard_wave_2026-07-27/unsolved_tasks.tsv",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/check_group2_hard_wave_consistency.py",
            "--snapshot",
            "./reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.json",
            "--resume-packet",
            "./reports/group2_hard_wave_2026-07-27/resume_packet.json",
            "--followup-pending",
            "./reports/group2_hard_wave_2026-07-27/followup_pending_tasks.json",
            "--followup-queue",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.json",
            "--followup-state",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.state.json",
            "--post-followup-queue",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.json",
            "--post-followup-state",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.state.json",
            "--post-followup-summary",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_summary.json",
            "--post-followup-exhausted",
            "./reports/group2_hard_wave_2026-07-27/post_followup_exhausted_tasks.json",
            "--output-json",
            "./reports/group2_hard_wave_2026-07-27/consistency_check.json",
            "--skip-resume-packet-checks",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_group2_hard_resume_packet.py",
            "--snapshot",
            "./reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.json",
            "--consistency-check",
            "./reports/group2_hard_wave_2026-07-27/consistency_check.json",
            "--pending-followup",
            "./reports/group2_hard_wave_2026-07-27/followup_pending_tasks.json",
            "--post-followup-summary",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_summary.json",
            "--post-followup-queue",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.json",
            "--output-json",
            "./reports/group2_hard_wave_2026-07-27/resume_packet.json",
            "--output-md",
            "./reports/group2_hard_wave_2026-07-27/resume_packet.md",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/check_group2_hard_wave_consistency.py",
            "--snapshot",
            "./reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.json",
            "--resume-packet",
            "./reports/group2_hard_wave_2026-07-27/resume_packet.json",
            "--followup-pending",
            "./reports/group2_hard_wave_2026-07-27/followup_pending_tasks.json",
            "--followup-queue",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.json",
            "--followup-state",
            "./reports/group2_hard_wave_2026-07-27/followup_retry_queue.state.json",
            "--post-followup-queue",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.json",
            "--post-followup-state",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_queue.state.json",
            "--post-followup-summary",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_summary.json",
            "--post-followup-exhausted",
            "./reports/group2_hard_wave_2026-07-27/post_followup_exhausted_tasks.json",
            "--output-json",
            "./reports/group2_hard_wave_2026-07-27/consistency_check.json",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_group2_hard_next_wave_report.py",
            "--snapshot",
            "./reports/group2_hard_wave_2026-07-27/group2_hard_status_snapshot.json",
            "--pending-followup",
            "./reports/group2_hard_wave_2026-07-27/followup_pending_tasks.json",
            "--post-followup-summary",
            "./reports/group2_hard_wave_2026-07-27/post_followup_retry_summary.json",
            "--consistency-check",
            "./reports/group2_hard_wave_2026-07-27/consistency_check.json",
            "--output-md",
            "./reports/group2_hard_wave_2026-07-27/next_wave_status.md",
        ]
    )


def refresh_regular_waves() -> None:
    reports_root = ROOT / "reports"
    for wave_dir in sorted(reports_root.glob("group*_wave_2026-07-27")):
        if wave_dir.name == "group2_hard_wave_2026-07-27":
            continue
        group_file = read_wave_group_file(wave_dir)
        if group_file is None:
            continue
        run_cmd(
            [
                str(PYTHON_BIN),
                "scripts/summarize_group_results.py",
                "--trajectory-index",
                "./codex_rescue_runs_local/trajectory_index.jsonl",
                "--group",
                group_file if group_file.startswith("/") else f"./{group_file}",
                "--output-json",
                f"./reports/{wave_dir.name}/live_summary.json",
            ]
        )


def refresh_coverage_and_dashboard(dashboard_output: Path) -> None:
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_split_group_coverage.py",
            "--output-json",
            "./reports/split_group_coverage_2026-07-27.json",
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_split_wave_dashboard.py",
            "--output-json",
            str(dashboard_output),
        ]
    )
    run_cmd(
        [
            str(PYTHON_BIN),
            "scripts/export_full_benchmark_campaign.py",
            "--dashboard-json",
            str(dashboard_output),
            "--coverage-json",
            "./reports/split_group_coverage_2026-07-27.json",
            "--output-json",
            "./reports/full_benchmark_campaign_2026-07-27.json",
            "--output-md",
            "./reports/full_benchmark_campaign_2026-07-27.md",
        ]
    )


def run_refresh_step(name: str, action: Callable[[], None], failures: list[str]) -> None:
    try:
        action()
    except subprocess.CalledProcessError as exc:
        failures.append(f"{name}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh trajectory state, active split-wave summaries, and dashboard JSON.")
    parser.add_argument(
        "--dashboard-output",
        type=Path,
        default=Path("./reports/split_wave_dashboard_2026-07-27.json"),
    )
    args = parser.parse_args()

    failures: list[str] = []
    run_refresh_step("refresh_trajectory_index", refresh_trajectory_index, failures)
    run_refresh_step("refresh_group2_hard", refresh_group2_hard, failures)
    run_refresh_step("refresh_regular_waves", refresh_regular_waves, failures)
    run_refresh_step(
        "refresh_coverage_and_dashboard",
        lambda: refresh_coverage_and_dashboard(args.dashboard_output),
        failures,
    )
    if failures:
        for failure in failures:
            print(f"warning: {failure}")
    print(args.dashboard_output.resolve())
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
