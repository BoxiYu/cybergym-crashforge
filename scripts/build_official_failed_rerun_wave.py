#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_used_tasks(selection_paths: list[Path]) -> set[str]:
    used: set[str] = set()
    for path in selection_paths:
        if not path.exists():
            continue
        try:
            payload = read_json(path)
        except Exception:
            continue
        for task_id in payload.get("selected_tasks", []):
            if isinstance(task_id, str) and task_id:
                used.add(task_id)
    return used


def collect_selection_paths(
    reports_root: Path,
    explicit_paths: list[Path],
    output_dir: Path,
    auto_include_official_wave_selections: bool,
) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved == (output_dir / "task_selection.json").resolve():
            return
        if resolved in seen or not resolved.exists():
            return
        seen.add(resolved)
        ordered.append(resolved)

    for path in explicit_paths:
        add(path)

    if auto_include_official_wave_selections:
        patterns = (
            "official_level1*_source/task_selection.json",
            "official_level1_fast_wave_*_source/task_selection.json",
        )
        for pattern in patterns:
            for path in sorted(reports_root.glob(pattern)):
                add(path)

    return ordered


def load_active_wave_tasks(state_paths: list[Path]) -> set[str]:
    active: set[str] = set()
    for path in state_paths:
        if not path.exists():
            continue
        try:
            payload = read_json(path)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if bool(payload.get("queue_complete")):
            continue
        for task_id in payload.get("processed_task_ids", []):
            if isinstance(task_id, str) and task_id:
                active.add(task_id)
        for task_id in payload.get("pending_task_ids_preview", []):
            if isinstance(task_id, str) and task_id:
                active.add(task_id)
        for task_id in payload.get("deferred_task_ids", []):
            if isinstance(task_id, str) and task_id:
                active.add(task_id)
    return active


def collect_active_state_paths(
    reports_root: Path,
    explicit_paths: list[Path],
    auto_exclude_official_wave_state: bool,
) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            return
        seen.add(resolved)
        ordered.append(resolved)

    for path in explicit_paths:
        add(path)

    if auto_exclude_official_wave_state:
        patterns = (
            "official_level1*/combined_queue.binary.state.json",
            "official_level1_fast_wave_*/combined_queue.binary.state.json",
        )
        for pattern in patterns:
            for path in sorted(reports_root.glob(pattern)):
                add(path)

    return ordered


def load_project_by_task(tasks_json: Path) -> dict[str, str]:
    if not tasks_json.exists():
        return {}
    payload = read_json(tasks_json)
    if not isinstance(payload, list):
        return {}
    project_by_task: dict[str, str] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        project = str(row.get("project_name") or row.get("project_main_repo") or "unknown")
        project_by_task[task_id] = project
    return project_by_task


def build_project_rates(scoreboard: dict[str, Any], project_by_task: dict[str, str]) -> dict[str, float]:
    submitted: Counter[str] = Counter()
    success: Counter[str] = Counter()
    for row in scoreboard.get("tasks", []):
        if not isinstance(row, dict):
            continue
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        project = project_by_task.get(task_id, "unknown")
        if bool(row.get("exactly_one_submit")):
            submitted[project] += 1
            if bool(row.get("final_submission_success")):
                success[project] += 1
    return {
        project: (success[project] / submitted[project] if submitted[project] else 0.0)
        for project in set(submitted) | set(success)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an official Level1 rerun wave from latest failed submitted tasks.")
    parser.add_argument(
        "--scoreboard-json",
        type=Path,
        default=ROOT / "reports" / "official_level1_scoreboard_2026-07-28.json",
    )
    parser.add_argument("--reports-root", type=Path, default=ROOT / "reports")
    parser.add_argument("--tasks-json", type=Path, default=ROOT / "cybergym_data" / "tasks.json")
    parser.add_argument("--selection-json", type=Path, action="append", default=[])
    parser.add_argument("--exclude-wave-state", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument(
        "--task-prefix",
        action="append",
        default=["arvo:"],
        help="Restrict selection to task IDs with one of these prefixes. Defaults to arvo: only.",
    )
    parser.add_argument("--project-name", action="append", default=[])
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--require-clean-local-only", action="store_true")
    parser.add_argument("--require-evidence-complete", action="store_true")
    parser.add_argument("--exclude-forbidden-access", action="store_true")
    parser.add_argument("--exclude-network-flagged", action="store_true")
    parser.add_argument(
        "--auto-include-official-wave-selections",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically exclude tasks already present in official wave selection JSONs under reports/.",
    )
    parser.add_argument(
        "--auto-exclude-official-wave-state",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically exclude tasks that are still pending or active in official wave state files under reports/.",
    )
    args = parser.parse_args()

    scoreboard = read_json(args.scoreboard_json.resolve())
    project_by_task = load_project_by_task(args.tasks_json.resolve())
    project_rates = build_project_rates(scoreboard, project_by_task)
    reports_root = args.reports_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    selection_paths = collect_selection_paths(
        reports_root,
        [path.resolve() for path in args.selection_json],
        output_dir,
        args.auto_include_official_wave_selections,
    )
    used_tasks = load_used_tasks(selection_paths)
    active_state_paths = collect_active_state_paths(
        reports_root,
        [path.resolve() for path in args.exclude_wave_state],
        args.auto_exclude_official_wave_state,
    )
    active_tasks = load_active_wave_tasks(active_state_paths)
    allowed_projects = {value for value in args.project_name if value}
    allowed_tasks = {value for value in args.task_id if value}
    task_prefixes = tuple(value for value in args.task_prefix if value)

    candidates: list[tuple[float, str, str, dict[str, Any]]] = []
    for row in scoreboard.get("tasks", []):
        if not isinstance(row, dict):
            continue
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        if task_id in used_tasks or task_id in active_tasks:
            continue
        if task_prefixes and not any(task_id.startswith(prefix) for prefix in task_prefixes):
            continue
        if allowed_tasks and task_id not in allowed_tasks:
            continue
        if not bool(row.get("exactly_one_submit")):
            continue
        if bool(row.get("final_submission_success")):
            continue
        if args.require_clean_local_only and not bool(row.get("clean_local_only")):
            continue
        if args.require_evidence_complete and not bool(row.get("evidence_complete")):
            continue
        if args.exclude_forbidden_access and bool(row.get("forbidden_access_detected")):
            continue
        if args.exclude_network_flagged and bool(row.get("network_command_detected")):
            continue
        project = project_by_task.get(task_id, "unknown")
        if allowed_projects and project not in allowed_projects:
            continue
        verdict = str(row.get("final_submission_verdict") or "")
        status = str(row.get("status") or "")
        severity_bias = 1.0 if verdict == "no_vul_crash" or status == "no_vul_crash" else 0.5
        audit_bias = 0.0
        if bool(row.get("clean_local_only")):
            audit_bias += 4.0
        if bool(row.get("evidence_complete")):
            audit_bias += 3.0
        if not bool(row.get("forbidden_access_detected")):
            audit_bias += 2.0
        if not bool(row.get("network_command_detected")):
            audit_bias += 1.0
        score = (project_rates.get(project, 0.0) * 100.0) + severity_bias + audit_bias
        candidates.append((score, project, task_id, row))

    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    selected = candidates[: max(args.count, 0)]
    selected_tasks = [task_id for _score, _project, task_id, _row in selected]

    (output_dir / "tasks.md").write_text("\n".join(selected_tasks) + ("\n" if selected_tasks else ""), encoding="utf-8")
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "selection_policy": "latest failed submitted tasks from official scoreboard, ranked by project success-rate",
        "selected_count": len(selected_tasks),
        "selected_tasks": selected_tasks,
        "selection_filters": {
            "project_names": sorted(allowed_projects),
            "task_ids": sorted(allowed_tasks),
            "task_prefixes": list(task_prefixes),
            "selection_json_files": [str(path) for path in selection_paths],
            "excluded_active_wave_state_files": [str(path) for path in active_state_paths],
            "require_clean_local_only": args.require_clean_local_only,
            "require_evidence_complete": args.require_evidence_complete,
            "exclude_forbidden_access": args.exclude_forbidden_access,
            "exclude_network_flagged": args.exclude_network_flagged,
            "auto_include_official_wave_selections": args.auto_include_official_wave_selections,
            "auto_exclude_official_wave_state": args.auto_exclude_official_wave_state,
        },
        "excluded_active_task_count": len(active_tasks),
        "excluded_selection_task_count": len(used_tasks),
        "selected_rows": [
            {
                "task_id": task_id,
                "project": project,
                "project_success_rate": project_rates.get(project, 0.0),
                "status": row.get("status"),
                "final_submission_verdict": row.get("final_submission_verdict"),
                "clean_local_only": row.get("clean_local_only"),
                "evidence_complete": row.get("evidence_complete"),
                "forbidden_access_detected": row.get("forbidden_access_detected"),
                "network_command_detected": row.get("network_command_detected"),
                "run_root": row.get("run_root"),
            }
            for _score, project, task_id, row in selected
        ],
    }
    (output_dir / "task_selection.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"selected_count": len(selected_tasks), "tasks": selected_tasks}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
