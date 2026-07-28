# Reproducibility Artifacts

This directory contains the smallest output set we need to let others recompute the public result snapshot without rerunning the full campaign.

Included files:

- `trajectory_index_2026-07-26.jsonl`: the authoritative run index snapshot used for public aggregation
- `trajectory_summary_2026-07-26.json`: a lightweight global summary generated from the same index
- `group1_latest_task_status_2026-07-27.json`: latest pass/fail task-id split for the public `group1-150` slice, derived from the same trajectory index snapshot
- `benchmark_snapshot_2026-07-27.json`: compact full-benchmark campaign summary used by `docs/RESULTS_2026-07-28.md`
- `verification_summary_2026-07-28.json`: compact verification-audit summary used by `docs/RESULTS_2026-07-28.md`

These artifacts are intentionally summary-level only. They do not include:

- raw PoC binaries
- local server databases
- full per-run task directories
- temporary queue state or scheduler logs

Use `trajectory_index_2026-07-26.jsonl` together with `scripts/summarize_group_results.py` and the task splits in `splits/` to recompute the published `group1-150` metrics.

Use `benchmark_snapshot_2026-07-27.json` and `verification_summary_2026-07-28.json`
when you only need the later public summary-level benchmark and verification
numbers rather than the older frozen run-level artifact.
