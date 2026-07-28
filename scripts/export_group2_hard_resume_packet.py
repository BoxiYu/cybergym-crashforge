from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def summarize_followup_tasks(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    tasks = []
    counts: dict[str, int] = {}
    for item in payload.get("pending_tasks", []) or []:
        if not isinstance(item, dict):
            continue
        prior_status = str(item.get("prior_status") or "unknown")
        counts[prior_status] = counts.get(prior_status, 0) + 1
        tasks.append(
            {
                "queue_name": item.get("queue_name"),
                "task_id": item.get("task_id"),
                "prior_status": prior_status,
                "attempt": item.get("attempt"),
                "source_run_id": item.get("source_run_id"),
            }
        )
    return tasks, dict(sorted(counts.items()))


def summarize_post_followup_tasks(summary_payload: dict[str, Any], queue_payload: Any) -> list[dict[str, Any]]:
    queue_items = queue_payload.get("items") if isinstance(queue_payload, dict) else queue_payload
    queue_items = queue_items if isinstance(queue_items, list) else []
    queue_by_name = {
        str(item.get("name")): item
        for item in queue_items
        if isinstance(item, dict) and item.get("name")
    }
    tasks = []
    for item in summary_payload.get("tasks", []) or []:
        if not isinstance(item, dict):
            continue
        queue_name = str(item.get("queue_name") or "")
        queue_entry = queue_by_name.get(queue_name, {})
        tasks.append(
            {
                "queue_name": item.get("queue_name"),
                "task_id": item.get("task_id"),
                "prior_status": item.get("prior_status"),
                "attempt": item.get("attempt"),
                "source_run_id": item.get("source_run_id"),
                "manifest": queue_entry.get("manifest"),
            }
        )
    return tasks


def build_markdown(payload: dict[str, Any]) -> str:
    usage_limit = payload.get("usage_limit") or {}
    followup = payload.get("followup") or {}
    post_followup = payload.get("post_followup") or {}
    consistency = payload.get("consistency_check") or {}

    lines = [
        "# Group2 Hard Resume Packet",
        "",
        f"Generated at: `{payload.get('generated_at')}`",
        "",
        "## Automation State",
        "",
        f"- Monitor running: `{payload.get('monitor_running')}`",
        f"- Next auto action: `{payload.get('next_auto_action')}`",
        f"- Followup launcher running: `{payload.get('followup_launcher_running')}`",
        f"- Post-followup launcher running: `{payload.get('post_followup_launcher_running')}`",
        "",
        "## Consistency Check",
        "",
        f"- Consistency ok: `{consistency.get('ok')}`",
        f"- Followup remaining verified: `{consistency.get('followup_remaining')}`",
        f"- Post-followup remaining verified: `{consistency.get('post_followup_remaining')}`",
        f"- Errors: `{json.dumps(consistency.get('errors', []), sort_keys=True)}`",
        "",
        "## Usage Limit",
        "",
        f"- Blocked: `{usage_limit.get('blocked')}`",
        f"- Pending items: `{usage_limit.get('pending_items')}`",
        f"- Reset at: `{usage_limit.get('reset_at')}`",
        "",
        "## Followup Queue",
        "",
        f"- Remaining tasks: `{followup.get('pending_count')}`",
        f"- Prior status counts: `{json.dumps(followup.get('prior_status_counts', {}), sort_keys=True)}`",
        "",
        "| queue_name | task_id | prior_status | attempt | source_run_id |",
        "|---|---|---|---:|---|",
    ]
    for item in followup.get("tasks", []):
        lines.append(
            f"| `{item.get('queue_name')}` | `{item.get('task_id')}` | `{item.get('prior_status')}` | "
            f"{item.get('attempt')} | `{item.get('source_run_id')}` |"
        )

    lines.extend(
        [
            "",
            "## Post-followup Queue",
            "",
            f"- Ready tasks: `{post_followup.get('task_count')}`",
            f"- Prior status counts: `{json.dumps(post_followup.get('prior_status_counts', {}), sort_keys=True)}`",
            "",
            "| queue_name | task_id | prior_status | attempt | manifest |",
            "|---|---|---|---:|---|",
        ]
    )
    for item in post_followup.get("tasks", []):
        lines.append(
            f"| `{item.get('queue_name')}` | `{item.get('task_id')}` | `{item.get('prior_status')}` | "
            f"{item.get('attempt')} | `{item.get('manifest')}` |"
        )

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a compact resume packet for the group2 hard followup and post-followup queues.")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--consistency-check", type=Path)
    parser.add_argument("--pending-followup", type=Path, required=True)
    parser.add_argument("--post-followup-summary", type=Path, required=True)
    parser.add_argument("--post-followup-queue", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()

    snapshot = read_json(args.snapshot.resolve())
    consistency_check = read_json(args.consistency_check.resolve()) if args.consistency_check else None
    pending_followup = read_json(args.pending_followup.resolve())
    post_followup_summary = read_json(args.post_followup_summary.resolve())
    post_followup_queue = read_json(args.post_followup_queue.resolve())

    followup_tasks, followup_prior_status_counts = summarize_followup_tasks(pending_followup)
    post_followup_tasks = summarize_post_followup_tasks(post_followup_summary, post_followup_queue)

    usage_limit_halt = snapshot.get("followup_usage_limit_halt") or {}
    payload = {
        "generated_at": snapshot.get("generated_at"),
        "monitor_running": bool((snapshot.get("monitor") or {}).get("running")),
        "followup_launcher_running": bool((snapshot.get("followup_launcher") or {}).get("running")),
        "post_followup_launcher_running": bool((snapshot.get("post_followup_launcher") or {}).get("running")),
        "next_auto_action": (snapshot.get("automation") or {}).get("next_auto_action"),
        "consistency_check": consistency_check,
        "usage_limit": {
            "blocked": bool((snapshot.get("automation") or {}).get("followup_usage_limit_blocked")),
            "pending_items": usage_limit_halt.get("pending_items"),
            "reset_at": usage_limit_halt.get("reset_at"),
        },
        "followup": {
            "pending_count": len(followup_tasks),
            "prior_status_counts": followup_prior_status_counts,
            "tasks": followup_tasks,
        },
        "post_followup": {
            "task_count": int(post_followup_summary.get("task_count") or 0),
            "prior_status_counts": dict(sorted((post_followup_summary.get("prior_status_counts") or {}).items())),
            "tasks": post_followup_tasks,
        },
    }

    write_json(args.output_json.resolve(), payload)
    write_markdown(args.output_md.resolve(), build_markdown(payload))
    print(
        json.dumps(
            {
                "followup_pending_count": payload["followup"]["pending_count"],
                "post_followup_task_count": payload["post_followup"]["task_count"],
                "output_json": str(args.output_json.resolve()),
                "output_md": str(args.output_md.resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
