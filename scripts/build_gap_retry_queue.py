from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_RETRYABLE_STATUSES = (
    "fix_also_crashes",
    "no_submission",
    "no_vul_crash",
    "codex_failed",
    "task_assets_failed",
    "task_generation_failed",
)

STATUS_PRIORITY = {
    "task_assets_failed": 0,
    "task_generation_failed": 1,
    "fix_also_crashes": 2,
    "no_submission": 3,
    "codex_failed": 4,
    "no_vul_crash": 5,
}
DEFAULT_CUMULATIVE_NO_VUL_EXCLUSION_THRESHOLD = 8


@dataclass
class LatestResult:
    mtime: float
    task_id: str
    status: str
    failure_category: str
    attempt: int
    run_id: str
    run_root: Path
    server: str | None
    data_dir: str | None
    difficulty: str | None
    result: dict[str, Any]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_result_timestamp(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00").replace(" ", "T")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def result_order_key(payload: dict[str, Any], result_path: Path) -> tuple[float, float, float]:
    started_at = parse_result_timestamp(payload.get("started_at"))
    ended_at = parse_result_timestamp(payload.get("ended_at")) or parse_result_timestamp(payload.get("finished_at"))
    file_mtime = result_path.stat().st_mtime
    primary = started_at or ended_at or file_mtime
    secondary = ended_at or file_mtime
    return (primary, secondary, file_mtime)


def load_queue_items(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path)
    if isinstance(payload, dict):
        return list(payload.get("items", []))
    if isinstance(payload, list):
        return list(payload)
    raise ValueError(f"unsupported queue payload in {path}")


def load_manifest_entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def collect_covered_task_ids(queue_paths: list[Path]) -> set[str]:
    covered: set[str] = set()
    for queue_path in queue_paths:
        for item in load_queue_items(queue_path):
            manifest_path = Path(item["manifest"])
            for entry in load_manifest_entries(manifest_path):
                task_id = entry.get("task_id")
                if task_id:
                    covered.add(task_id)
    return covered


def load_latest_results(results_root: Path) -> dict[str, LatestResult]:
    latest: dict[str, LatestResult] = {}
    for result_path in results_root.rglob("result.json"):
        try:
            payload = read_json(result_path)
        except Exception:
            continue
        task_id = payload.get("task_id")
        status = payload.get("status")
        if not task_id or not status:
            continue
        order_key = result_order_key(payload, result_path)
        logical_timestamp = order_key[0]
        task_data = payload.get("task_data") or {}
        rec = LatestResult(
            mtime=logical_timestamp,
            task_id=task_id,
            status=status,
            failure_category=payload.get("failure_category") or status,
            attempt=int(payload.get("attempt", 1)),
            run_id=payload.get("run_id") or result_path.parent.name,
            run_root=result_path.parent.resolve(),
            server=payload.get("server"),
            data_dir=task_data.get("data_dir"),
            difficulty=payload.get("difficulty"),
            result=payload,
        )
        prev = latest.get(task_id)
        if prev is None or order_key > result_order_key(prev.result, prev.run_root / "result.json"):
            latest[task_id] = rec
    return latest


def collect_successful_task_ids(results_root: Path) -> set[str]:
    successful: set[str] = set()
    for result_path in results_root.rglob("result.json"):
        try:
            payload = read_json(result_path)
        except Exception:
            continue
        task_id = payload.get("task_id")
        if not task_id:
            continue
        if payload.get("status") == "success" or any(
            record.get("verdict") == "verified_success" for record in (payload.get("records") or [])
        ):
            successful.add(task_id)
    return successful


def collect_cumulative_no_vul_submission_counts(results_root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result_path in results_root.rglob("result.json"):
        try:
            payload = read_json(result_path)
        except Exception:
            continue
        task_id = payload.get("task_id")
        if not task_id:
            continue
        no_vul_count = sum(1 for record in (payload.get("records") or []) if record.get("verdict") == "no_vul_crash")
        if no_vul_count <= 0:
            continue
        counts[task_id] = counts.get(task_id, 0) + no_vul_count
    return counts


def looks_like_manifest_entry(entry: dict[str, Any]) -> bool:
    return bool(
        entry.get("task_id")
        and (
            entry.get("codex_bin")
            or entry.get("api_key_env")
            or entry.get("source_run_id")
            or entry.get("codex_timeout_seconds")
        )
    )


def build_template_index(search_roots: list[Path]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for root in search_roots:
        for path in sorted(root.rglob("*.jsonl")):
            try:
                for raw_line in path.read_text(encoding="utf-8").splitlines():
                    if not raw_line.strip():
                        continue
                    try:
                        entry = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict) or not looks_like_manifest_entry(entry):
                        continue
                    task_id = entry["task_id"]
                    index.setdefault(task_id, entry)
            except OSError:
                continue
    return index


def build_retry_manifest_entry(
    *,
    latest: LatestResult,
    template: dict[str, Any],
    campaign: str,
    server_override: str | None,
    data_dir_override: str | None,
) -> dict[str, Any]:
    entry = dict(template)
    entry["task_id"] = latest.task_id
    entry["attempt"] = latest.attempt + 1
    entry["campaign"] = campaign
    entry["prior_status"] = latest.status
    entry["failure_category"] = latest.failure_category
    entry["parent_run_id"] = latest.run_id
    entry["source_run_id"] = latest.run_id
    entry["source_run_root"] = str(latest.run_root)
    entry["retryable"] = True
    if server_override or latest.server:
        entry["server"] = server_override or latest.server
    if data_dir_override or latest.data_dir:
        entry["data_dir"] = data_dir_override or latest.data_dir
    if latest.difficulty:
        entry["difficulty"] = latest.difficulty
    return entry


def task_slug(task_id: str) -> str:
    return task_id.replace(":", "_")


def latest_submission_count(latest: LatestResult) -> int:
    records = latest.result.get("records")
    if isinstance(records, list) and records:
        return len(records)
    codex = latest.result.get("codex") or {}
    metrics = codex.get("watchdog_metrics") or {}
    try:
        return int(metrics.get("submit_completed") or 0)
    except (TypeError, ValueError):
        return 0


def gap_sort_key(
    row: LatestResult,
    *,
    cumulative_no_vul_counts: dict[str, int] | None = None,
) -> tuple[int, int, int, int, int | float, float, str]:
    status_priority = STATUS_PRIORITY.get(row.status, 999)
    if row.status == "no_vul_crash":
        submissions = latest_submission_count(row)
        cumulative_no_vul = (cumulative_no_vul_counts or {}).get(row.task_id, submissions)
        cumulative_exhausted_bucket = 0 if cumulative_no_vul <= 6 else 1
        current_exhausted_bucket = 0 if submissions <= 3 else 1
        return (status_priority, cumulative_exhausted_bucket, current_exhausted_bucket, cumulative_no_vul, submissions, -row.mtime, row.task_id)
    return (status_priority, 0, 0, 0, 0, -row.mtime, row.task_id)


def select_gap_tasks(
    *,
    latest_results: dict[str, LatestResult],
    covered_task_ids: set[str],
    retryable_statuses: set[str],
    successful_task_ids: set[str],
    cumulative_no_vul_counts: dict[str, int] | None = None,
    cumulative_no_vul_exclusion_threshold: int = DEFAULT_CUMULATIVE_NO_VUL_EXCLUSION_THRESHOLD,
) -> list[LatestResult]:
    rows = [
        latest
        for latest in latest_results.values()
        if latest.status in retryable_statuses
        and latest.task_id not in covered_task_ids
        and latest.task_id not in successful_task_ids
        and (cumulative_no_vul_counts or {}).get(latest.task_id, 0) < cumulative_no_vul_exclusion_threshold
    ]
    return sorted(
        rows,
        key=lambda row: gap_sort_key(row, cumulative_no_vul_counts=cumulative_no_vul_counts),
    )


def write_gap_queue(
    *,
    gap_tasks: list[LatestResult],
    template_index: dict[str, dict[str, Any]],
    output_manifest_dir: Path,
    output_queue_file: Path,
    output_root: str,
    campaign: str,
    server_override: str | None,
    data_dir_override: str | None,
) -> dict[str, Any]:
    output_manifest_dir.mkdir(parents=True, exist_ok=True)
    output_queue_file.parent.mkdir(parents=True, exist_ok=True)

    items: list[dict[str, Any]] = []
    missing_templates: list[str] = []
    written_manifests: list[str] = []
    written_task_ids: list[str] = []
    for index, latest in enumerate(gap_tasks, start=1):
        template = template_index.get(latest.task_id)
        if template is None:
            missing_templates.append(latest.task_id)
            continue
        entry = build_retry_manifest_entry(
            latest=latest,
            template=template,
            campaign=campaign,
            server_override=server_override,
            data_dir_override=data_dir_override,
        )
        manifest_path = output_manifest_dir / f"{index:02d}_{task_slug(latest.task_id)}.jsonl"
        manifest_path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
        written_manifests.append(str(manifest_path))
        written_task_ids.append(latest.task_id)
        items.append(
            {
                "manifest": str(manifest_path),
                "output_root": output_root,
                "name": f"{index:02d}_{task_slug(latest.task_id)}",
            }
        )

    output_queue_file.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "queue_file": str(output_queue_file),
        "items": len(items),
        "task_ids": written_task_ids,
        "missing_templates": missing_templates,
        "written_manifests": written_manifests,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a follow-up queue for retryable tasks not covered by existing rescue queues.")
    parser.add_argument("--results-root", type=Path, default=Path("codex_rescue_runs_local"))
    parser.add_argument("--manifest-search-root", type=Path, action="append", required=True)
    parser.add_argument("--existing-queue", type=Path, action="append", default=[])
    parser.add_argument("--output-manifest-dir", type=Path, required=True)
    parser.add_argument("--output-queue-file", type=Path, required=True)
    parser.add_argument("--output-root", default="./codex_rescue_runs_local/wave7_gap_single")
    parser.add_argument("--campaign", default="wave7_gap_retryable")
    parser.add_argument("--server")
    parser.add_argument("--data-dir")
    parser.add_argument("--retryable-status", action="append", default=list(DEFAULT_RETRYABLE_STATUSES))
    parser.add_argument(
        "--cumulative-no-vul-exclusion-threshold",
        type=int,
        default=DEFAULT_CUMULATIVE_NO_VUL_EXCLUSION_THRESHOLD,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    latest_results = load_latest_results(args.results_root)
    successful_task_ids = collect_successful_task_ids(args.results_root)
    cumulative_no_vul_counts = collect_cumulative_no_vul_submission_counts(args.results_root)
    covered = collect_covered_task_ids(args.existing_queue)
    template_index = build_template_index(args.manifest_search_root)
    gap_tasks = select_gap_tasks(
        latest_results=latest_results,
        covered_task_ids=covered,
        retryable_statuses=set(args.retryable_status),
        successful_task_ids=successful_task_ids,
        cumulative_no_vul_counts=cumulative_no_vul_counts,
        cumulative_no_vul_exclusion_threshold=args.cumulative_no_vul_exclusion_threshold,
    )
    summary = write_gap_queue(
        gap_tasks=gap_tasks,
        template_index=template_index,
        output_manifest_dir=args.output_manifest_dir,
        output_queue_file=args.output_queue_file,
        output_root=args.output_root,
        campaign=args.campaign,
        server_override=args.server,
        data_dir_override=args.data_dir,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["missing_templates"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
