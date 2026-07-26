# Reproducibility Artifacts

This directory contains the smallest output set we need to let others recompute the public result snapshot without rerunning the full campaign.

Included files:

- `trajectory_index_2026-07-26.jsonl`: the authoritative run index snapshot used for public aggregation
- `trajectory_summary_2026-07-26.json`: a lightweight global summary generated from the same index

These artifacts are intentionally summary-level only. They do not include:

- raw PoC binaries
- local server databases
- full per-run task directories
- temporary queue state or scheduler logs

Use `trajectory_index_2026-07-26.jsonl` together with `scripts/summarize_group_results.py` and the task splits in `splits/` to recompute the published `group1-150` metrics.
