from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PRIMARY_GROUPS = [f"group_{index:02d}.md" for index in range(1, 11)]
DEFAULT_DASHBOARD = Path("reports/split_wave_dashboard_2026-07-27.json")
DEFAULT_COVERAGE = Path("reports/split_group_coverage_2026-07-27.json")
DEFAULT_OUTPUT_JSON = Path("reports/full_benchmark_campaign_2026-07-27.json")
DEFAULT_OUTPUT_MD = Path("reports/full_benchmark_campaign_2026-07-27.md")


@dataclass(frozen=True)
class GroupCampaignRow:
    group_file: str
    total: int
    attempted_count: int
    success_count: int
    unattempted_count: int
    unsolved_count: int
    remaining_count: int
    attempted_rate: float
    success_rate_all: float
    wave_name: str | None
    wave_attempted: int | None
    wave_success: int | None
    active_runner_count: int
    queue_complete: bool | None
    state: str
    priority: str
    goal: str


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def classify_group(
    group_file: str,
    coverage_row: dict[str, Any],
    wave_name: str | None,
    wave_row: dict[str, Any] | None,
) -> GroupCampaignRow:
    total = int(coverage_row.get("total") or 0)
    attempted_count = int(coverage_row.get("attempted_count") or 0)
    success_count = int(coverage_row.get("success_count") or 0)
    unattempted_count = max(total - attempted_count, 0)
    unsolved_count = max(total - success_count, 0)
    remaining_count = unsolved_count
    attempted_rate = float(coverage_row.get("attempted_rate") or 0.0)
    success_rate_all = float(coverage_row.get("success_rate_all") or 0.0)

    wave_attempted = int(wave_row.get("attempted") or 0) if wave_row else None
    wave_success = int(wave_row.get("latest_success") or 0) if wave_row else None
    active_runner_count = int(wave_row.get("active_runner_count") or 0) if wave_row else 0
    queue_complete = bool(wave_row.get("queue_complete")) if wave_row and "queue_complete" in wave_row else None

    if group_file == "group_02.md":
        state = "split_campaign"
        priority = "active"
        goal = (
            "Continue `group2_easy` frontier work immediately; keep `group2_hard` prepared and monitored until usage limit resets."
        )
    elif wave_row is None and attempted_count == 0:
        state = "not_started"
        priority = "launch"
        goal = "Launch a binary-only split wave and drive attempted coverage upward."
    elif wave_row is None and unsolved_count > 0:
        state = "historical_revisit"
        priority = "launch"
        goal = "Launch a binary-only retry wave over currently unsolved tasks and keep pushing benchmark score upward."
    elif wave_row is None:
        state = "historical_partial"
        priority = "launch"
        goal = "Convert historical partial coverage into a managed split wave."
    elif queue_complete and unsolved_count > 0:
        state = "wave_revisit"
        priority = "active"
        goal = "Reopen this completed wave as a retry frontier focused only on currently unsolved tasks."
    elif queue_complete:
        state = "wave_complete"
        priority = "freeze"
        goal = "Keep the completed wave frozen because every task in the canonical group already has verified success."
    else:
        state = "wave_active"
        priority = "active"
        goal = "Keep the wave running until queue completion, then convert it into a retry frontier if unsolved tasks remain."

    return GroupCampaignRow(
        group_file=group_file,
        total=total,
        attempted_count=attempted_count,
        success_count=success_count,
        unattempted_count=unattempted_count,
        unsolved_count=unsolved_count,
        remaining_count=remaining_count,
        attempted_rate=attempted_rate,
        success_rate_all=success_rate_all,
        wave_name=wave_name,
        wave_attempted=wave_attempted,
        wave_success=wave_success,
        active_runner_count=active_runner_count,
        queue_complete=queue_complete,
        state=state,
        priority=priority,
        goal=goal,
    )


def infer_primary_wave_by_group_file(waves: dict[str, dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for wave_name, wave_row in waves.items():
        group_file = Path(str(wave_row.get("group_file") or "")).name
        if not group_file:
            continue
        if group_file == "group_02_easy.md":
            mapping["group_02.md"] = wave_name
            continue
        if group_file.endswith("_hard.md") or group_file.endswith("_easy.md"):
            continue
        mapping[group_file] = wave_name
    return mapping


def build_payload(dashboard: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    waves = dict(coverage.get("groups") or {})
    dashboard_waves = dict(dashboard.get("waves") or {})
    primary_wave_map = infer_primary_wave_by_group_file(dashboard_waves)

    group_rows: list[GroupCampaignRow] = []
    for group_file in PRIMARY_GROUPS:
        coverage_row = dict(waves.get(group_file) or {})
        wave_name = primary_wave_map.get(group_file)
        wave_row = dict(dashboard_waves.get(wave_name) or {}) if wave_name else None
        group_rows.append(classify_group(group_file, coverage_row, wave_name, wave_row))

    total_tasks = sum(row.total for row in group_rows)
    attempted_tasks = sum(row.attempted_count for row in group_rows)
    success_tasks = sum(row.success_count for row in group_rows)
    unattempted_tasks = sum(row.unattempted_count for row in group_rows)
    remaining_tasks = sum(row.remaining_count for row in group_rows)

    active_groups = [row for row in group_rows if row.priority == "active" and row.group_file != "group_02.md"]
    active_groups.sort(
        key=lambda row: (
            -row.active_runner_count,
            -row.success_rate_all,
            -row.remaining_count,
            row.group_file,
        )
    )

    payload = {
        "generated_at": now_utc(),
        "goal": {
            "objective": "Maximize verified_success coverage across the full benchmark with binary-only, repo-aware routing.",
            "execution_mode": "binary_only_split_waves_with_repo_routing",
            "success_metric": "sum of success_count over canonical groups group_01.md .. group_10.md",
        },
        "benchmark": {
            "primary_groups": PRIMARY_GROUPS,
            "total_tasks": total_tasks,
            "attempted_tasks": attempted_tasks,
            "success_tasks": success_tasks,
            "unattempted_tasks": unattempted_tasks,
            "unsolved_tasks": remaining_tasks,
            "remaining_tasks": remaining_tasks,
            "attempted_rate": (attempted_tasks / total_tasks) if total_tasks else 0.0,
            "success_rate_all": (success_tasks / total_tasks) if total_tasks else 0.0,
        },
        "live_operations": {
            "dashboard_generated_at": dashboard.get("generated_at"),
            "wave_count": int(dashboard.get("wave_count") or 0),
            "total_active_runners": int(dashboard.get("total_active_runners") or 0),
            "completed_waves": [
                name for name, row in sorted(dashboard_waves.items()) if bool(row.get("queue_complete"))
            ],
            "active_waves": [
                {
                    "wave_name": name,
                    "group_file": Path(str(row.get("group_file") or "")).name,
                    "active_runner_count": int(row.get("active_runner_count") or 0),
                    "attempted": int(row.get("attempted") or 0),
                    "latest_success": int(row.get("latest_success") or 0),
                    "task_count": int(row.get("task_count") or 0),
                }
                for name, row in sorted(dashboard_waves.items())
                if not bool(row.get("queue_complete"))
            ],
        },
        "group2_special_case": {
            "easy_wave": dashboard_waves.get("group2_easy_wave_2026-07-27"),
            "hard_wave": dashboard_waves.get("group2_hard_wave_2026-07-27"),
            "policy": (
                "Treat group2 easy as an active frontier wave. Treat group2 hard as prepared/monitored until usage-limit reset, "
                "then resume followup before third-pass retries."
            ),
        },
        "campaign_rules": [
            "Run broad coverage on canonical groups group_01 .. group_10 via binary-only waves and retry waves.",
            "Within each wave, route tasks by repository-level yield so strong repositories get deeper retries while black-hole repositories get earlier cutoffs.",
            "Spend runner budget on unattempted tasks first, then recycle idle capacity into unsolved-task retry waves.",
            "Only freeze a canonical group once every task in that group has verified success.",
            "Keep group2 hard automation healthy, but do not let it block whole-benchmark refresh/dashboard updates.",
            "Refresh status from `scripts/refresh_active_split_waves.py` and regenerate this campaign report after major scheduling changes.",
        ],
        "group_rows": [row.__dict__ for row in group_rows],
        "frontier_priority": [row.group_file for row in active_groups],
    }
    return payload


def render_markdown(payload: dict[str, Any]) -> str:
    benchmark = payload["benchmark"]
    live = payload["live_operations"]
    hard_wave = payload["group2_special_case"].get("hard_wave") or {}

    lines = [
        "# Full Benchmark Campaign",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "## Goal",
        "",
        f"- Objective: `{payload['goal']['objective']}`",
        f"- Execution mode: `{payload['goal']['execution_mode']}`",
        f"- Success metric: `{payload['goal']['success_metric']}`",
        "",
        "## Benchmark Snapshot",
        "",
        f"- Canonical groups: `{', '.join(payload['goal']['objective'] and benchmark['primary_groups'])}`",
        f"- Total tasks: `{benchmark['total_tasks']}`",
        f"- Attempted tasks: `{benchmark['attempted_tasks']}`",
        f"- Success tasks: `{benchmark['success_tasks']}`",
        f"- Unattempted tasks: `{benchmark['unattempted_tasks']}`",
        f"- Unsolved tasks: `{benchmark['unsolved_tasks']}`",
        f"- Remaining tasks: `{benchmark['remaining_tasks']}`",
        f"- Attempted rate: `{benchmark['attempted_rate']:.1%}`",
        f"- Success rate all: `{benchmark['success_rate_all']:.1%}`",
        "",
        "## Live Operations",
        "",
        f"- Dashboard generated at: `{live['dashboard_generated_at']}`",
        f"- Active wave count: `{live['wave_count']}`",
        f"- Total active runners: `{live['total_active_runners']}`",
        f"- Completed waves: `{', '.join(live['completed_waves']) if live['completed_waves'] else 'none'}`",
        "",
        "## Active Frontier Order",
        "",
    ]

    for group_file in payload["frontier_priority"]:
        row = next(item for item in payload["group_rows"] if item["group_file"] == group_file)
        lines.append(
            f"- `{group_file}`: active_runners=`{row['active_runner_count']}`, "
            f"attempted=`{row['attempted_count']}/{row['total']}`, success=`{row['success_count']}`, "
            f"unsolved=`{row['unsolved_count']}`"
        )

    lines.extend(
        [
            "",
            "## Group Strategy",
            "",
            "| group | state | priority | attempted | success | unattempted | unsolved | wave | goal |",
            "|---|---|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for row in payload["group_rows"]:
        lines.append(
            f"| `{row['group_file']}` | `{row['state']}` | `{row['priority']}` | "
            f"`{row['attempted_count']}/{row['total']}` | `{row['success_count']}` | `{row['unattempted_count']}` | `{row['unsolved_count']}` | "
            f"`{row['wave_name'] or 'historical'}` | {row['goal']} |"
        )

    lines.extend(
        [
            "",
            "## Group2 Policy",
            "",
            f"- `group2_easy`: keep running as the only active split for the canonical `group_02.md` frontier.",
            f"- `group2_hard`: latest success=`{hard_wave.get('latest_success') or 0}`, "
            f"active runners=`{hard_wave.get('active_runner_count') or 0}`.",
            "- `group2_hard` rule: maintain monitor/queues, but do not let its usage-limit gate stall full-benchmark scheduling.",
            "",
            "## Campaign Rules",
            "",
        ]
    )
    for rule in payload["campaign_rules"]:
        lines.append(f"- {rule}")

    lines.extend(
        [
            "",
            "## Commands",
            "",
            "```bash",
            "python3 scripts/refresh_active_split_waves.py",
            "python3 scripts/export_full_benchmark_campaign.py",
            "cat reports/full_benchmark_campaign_2026-07-27.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the current full-benchmark campaign goal and operating plan.")
    parser.add_argument("--dashboard-json", type=Path, default=DEFAULT_DASHBOARD)
    parser.add_argument("--coverage-json", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_OUTPUT_MD)
    args = parser.parse_args()

    dashboard = read_json(args.dashboard_json.resolve())
    coverage = read_json(args.coverage_json.resolve())
    payload = build_payload(dashboard, coverage)
    markdown = render_markdown(payload)

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(markdown + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output_json": str(args.output_json.resolve()),
                "output_md": str(args.output_md.resolve()),
                "success_tasks": payload["benchmark"]["success_tasks"],
                "remaining_tasks": payload["benchmark"]["remaining_tasks"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
