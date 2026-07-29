#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ProjectStats:
    successes: int
    failures: int

    @property
    def attempts(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0

    @property
    def score(self) -> float:
        # Bias toward projects that actually convert under official rules, while
        # still rewarding deeper success history over one-off lucky hits.
        return (self.success_rate * 20.0) + (self.successes * 2.0) - (self.failures * 1.0)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_task_metadata(task_catalog_path: Path) -> dict[str, dict[str, Any]]:
    if not task_catalog_path.exists():
        return {}
    payload = read_json(task_catalog_path)
    if not isinstance(payload, list):
        return {}
    metadata_by_task: dict[str, dict[str, Any]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        task_id = row.get("task_id")
        if isinstance(task_id, str) and task_id:
            metadata_by_task[task_id] = row
    return metadata_by_task


def load_manifest_by_task(reports_root: Path, task_catalog_path: Path) -> dict[str, dict[str, Any]]:
    manifest_by_task: dict[str, dict[str, Any]] = {}
    for task_id, metadata in load_task_metadata(task_catalog_path).items():
        manifest_by_task[task_id] = dict(metadata)
    for manifest_path in reports_root.glob("**/fresh_manifests/*.jsonl"):
        try:
            lines = manifest_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        try:
            payload = json.loads(lines[0])
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        task_id = payload.get("task_id")
        if isinstance(task_id, str) and task_id:
            manifest_by_task.setdefault(task_id, payload)
    return manifest_by_task


def iter_official_result_paths(results_root: Path):
    if not results_root.exists():
        return
    for result_path in results_root.glob("official_level1_*_single/**/result.json"):
        if result_path.is_file():
            yield result_path


def load_project_stats(results_root: Path, manifest_by_task: dict[str, dict[str, Any]]) -> dict[str, ProjectStats]:
    successes: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    for result_path in iter_official_result_paths(results_root):
        try:
            payload = read_json(result_path)
        except Exception:
            continue
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        manifest = manifest_by_task.get(task_id) or {}
        project = str(manifest.get("project_name") or manifest.get("project_main_repo") or "unknown")
        submission_metrics = payload.get("submission_metrics") or {}
        verdict = str(submission_metrics.get("final_submission_verdict") or "")
        status = str(payload.get("status") or "")
        if verdict == "verified_success":
            successes[project] += 1
        elif status in {"no_vul_crash", "fix_also_crashes", "no_submission"}:
            failures[project] += 1
    stats: dict[str, ProjectStats] = {}
    for project in set(successes) | set(failures):
        stats[project] = ProjectStats(successes=successes[project], failures=failures[project])
    return stats


def load_project_stats_from_scoreboard(
    scoreboard_path: Path,
    manifest_by_task: dict[str, dict[str, Any]],
) -> dict[str, ProjectStats]:
    successes: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    scoreboard = read_json(scoreboard_path)
    for row in scoreboard.get("tasks", []):
        if not isinstance(row, dict):
            continue
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        if not bool(row.get("exactly_one_submit")):
            continue
        manifest = manifest_by_task.get(task_id) or {}
        project = str(manifest.get("project_name") or manifest.get("project_main_repo") or "unknown")
        if bool(row.get("final_submission_success")):
            successes[project] += 1
        else:
            failures[project] += 1
    stats: dict[str, ProjectStats] = {}
    for project in set(successes) | set(failures):
        stats[project] = ProjectStats(successes=successes[project], failures=failures[project])
    return stats


def load_used_tasks(selection_paths: list[Path]) -> set[str]:
    used: set[str] = set()
    for path in selection_paths:
        if not path.exists():
            continue
        payload = read_json(path)
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
        if resolved == output_dir / "task_selection.json":
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


def load_scoreboard_task_rows(scoreboard_path: Path) -> dict[str, dict[str, Any]]:
    scoreboard = read_json(scoreboard_path)
    rows: dict[str, dict[str, Any]] = {}
    for row in scoreboard.get("tasks", []):
        if not isinstance(row, dict):
            continue
        task_id = row.get("task_id")
        if isinstance(task_id, str) and task_id:
            rows[task_id] = row
    return rows


def build_ranked_tasks(
    scoreboard_path: Path,
    manifest_by_task: dict[str, dict[str, Any]],
    project_stats: dict[str, ProjectStats],
    used_tasks: set[str],
    task_prefixes: tuple[str, ...],
    min_project_success_rate: float,
    min_project_successes: int,
    selection_pool: str,
    allowed_projects: set[str],
    scoreboard_task_rows: dict[str, dict[str, Any]],
    exclude_scoreboard_task_ids: bool,
    max_per_project: int,
) -> list[tuple[float, str, str]]:
    if selection_pool == "task_catalog":
        base_tasks = sorted(manifest_by_task.keys())
    else:
        scoreboard = read_json(scoreboard_path)
        entries = {row["key"]: row for row in scoreboard["scoreboard"]}
        base_tasks = entries["observed_clean_same_poc_rerun_stable"]["task_ids"]
    ranked: list[tuple[float, str, str]] = []
    for task_id in base_tasks:
        if task_id in used_tasks:
            continue
        if exclude_scoreboard_task_ids and task_id in scoreboard_task_rows:
            continue
        if task_prefixes and not any(task_id.startswith(prefix) for prefix in task_prefixes):
            continue
        manifest = manifest_by_task.get(task_id) or {}
        project = str(manifest.get("project_name") or manifest.get("project_main_repo") or "unknown")
        if allowed_projects and project not in allowed_projects:
            continue
        stats = project_stats.get(project, ProjectStats(successes=0, failures=0))
        if stats.successes < min_project_successes:
            continue
        if stats.success_rate < min_project_success_rate:
            continue
        priority_hint = float(manifest.get("project_priority_score") or 0.0)
        score = stats.score + priority_hint
        ranked.append((score, project, task_id))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    if max_per_project <= 0:
        return ranked
    limited: list[tuple[float, str, str]] = []
    per_project_counts: Counter[str] = Counter()
    for row in ranked:
        _score, project, _task_id = row
        if per_project_counts[project] >= max_per_project:
            continue
        per_project_counts[project] += 1
        limited.append(row)
    return limited


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a project-prioritized official Level1 wave task list.")
    parser.add_argument("--scoreboard-json", type=Path, default=ROOT / "reports" / "strict_level1_scoreboard_2026-07-28.json")
    parser.add_argument("--reports-root", type=Path, default=ROOT / "reports")
    parser.add_argument("--results-root", type=Path, default=ROOT / "codex_rescue_runs_local")
    parser.add_argument("--task-catalog-json", type=Path, default=ROOT / "cybergym_data" / "tasks.json")
    parser.add_argument("--selection-json", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument(
        "--task-prefix",
        action="append",
        default=["arvo:"],
        help="Restrict selection to task IDs with one of these prefixes. Defaults to arvo: only.",
    )
    parser.add_argument(
        "--selection-pool",
        choices=("scoreboard_clean", "task_catalog"),
        default="scoreboard_clean",
        help="Choose candidates from the strict clean-scoreboard pool or the full task catalog.",
    )
    parser.add_argument(
        "--project-name",
        action="append",
        default=[],
        help="Restrict selection to one or more exact project_name values.",
    )
    parser.add_argument(
        "--min-project-success-rate",
        type=float,
        default=0.0,
        help="Require projects to meet at least this official verified-success rate before selecting new tasks.",
    )
    parser.add_argument(
        "--min-project-successes",
        type=int,
        default=0,
        help="Require projects to have at least this many official verified successes before selecting new tasks.",
    )
    parser.add_argument(
        "--auto-include-official-wave-selections",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically exclude tasks already present in official wave selection JSONs under reports/.",
    )
    parser.add_argument(
        "--exclude-scoreboard-task-ids",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip tasks that already have any official scoreboard row; use rerun tooling for those instead.",
    )
    parser.add_argument(
        "--project-stats-source",
        choices=("result_runs", "scoreboard_tasks"),
        default="scoreboard_tasks",
        help="Use per-run history or current official scoreboard task outcomes when ranking projects.",
    )
    parser.add_argument(
        "--max-per-project",
        type=int,
        default=0,
        help="Cap the number of selected tasks per project. Use 0 for no cap.",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_by_task = load_manifest_by_task(args.reports_root.resolve(), args.task_catalog_json.resolve())
    scoreboard_path = args.scoreboard_json.resolve()
    if args.project_stats_source == "scoreboard_tasks":
        project_stats = load_project_stats_from_scoreboard(scoreboard_path, manifest_by_task)
    else:
        project_stats = load_project_stats(args.results_root.resolve(), manifest_by_task)
    selection_paths = collect_selection_paths(
        args.reports_root.resolve(),
        [path.resolve() for path in args.selection_json],
        output_dir,
        args.auto_include_official_wave_selections,
    )
    used_tasks = load_used_tasks(selection_paths)
    scoreboard_task_rows = load_scoreboard_task_rows(scoreboard_path)
    ranked = build_ranked_tasks(
        scoreboard_path,
        manifest_by_task,
        project_stats,
        used_tasks,
        tuple(args.task_prefix),
        max(args.min_project_success_rate, 0.0),
        max(args.min_project_successes, 0),
        args.selection_pool,
        {value for value in args.project_name if value},
        scoreboard_task_rows,
        args.exclude_scoreboard_task_ids,
        max(args.max_per_project, 0),
    )
    selected = ranked[: max(args.count, 0)]

    tasks = [task_id for _, _, task_id in selected]
    project_selection_counts = Counter(project for _, project, _ in selected)
    (output_dir / "tasks.md").write_text("\n".join(tasks) + "\n", encoding="utf-8")
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "selection_source": (
            f"{args.scoreboard_json}::observed_clean_same_poc_rerun_stable"
            if args.selection_pool == "scoreboard_clean"
            else str(args.task_catalog_json)
        ),
        "selection_policy": (
            "project-prioritized using current official scoreboard task outcomes"
            if args.project_stats_source == "scoreboard_tasks"
            else "project-prioritized using current official verified_success / failure history"
        ),
        "selected_count": len(tasks),
        "selected_tasks": tasks,
        "selection_filters": {
            "selection_pool": args.selection_pool,
            "project_stats_source": args.project_stats_source,
            "auto_include_official_wave_selections": args.auto_include_official_wave_selections,
            "exclude_scoreboard_task_ids": args.exclude_scoreboard_task_ids,
            "min_project_success_rate": max(args.min_project_success_rate, 0.0),
            "min_project_successes": max(args.min_project_successes, 0),
            "project_names": [value for value in args.project_name if value],
            "max_per_project": max(args.max_per_project, 0),
        },
        "selected_project_counts": dict(sorted(project_selection_counts.items())),
        "selected_rows": [
            {
                "task_id": task_id,
                "project": project,
                "priority_score": score,
                "official_project_successes": project_stats.get(project, ProjectStats(0, 0)).successes,
                "official_project_failures": project_stats.get(project, ProjectStats(0, 0)).failures,
                "official_project_success_rate": project_stats.get(project, ProjectStats(0, 0)).success_rate,
            }
            for score, project, task_id in selected
        ],
        "excluded_task_count": len(used_tasks),
        "official_scoreboard_task_count": len(scoreboard_task_rows),
    }
    (output_dir / "task_selection.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"selected_count": len(tasks), "tasks": tasks}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
