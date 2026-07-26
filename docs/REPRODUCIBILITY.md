# Reproducibility

The simplest way to reproduce the published `group1-150` numbers is to use the frozen trajectory index artifact instead of rerunning the full benchmark campaign.

## Included outputs

- `artifacts/trajectory_index_2026-07-26.jsonl`
- `artifacts/trajectory_summary_2026-07-26.json`
- `docs/group1_snapshot_2026-07-26.json`

## Recompute the public snapshot

```bash
python scripts/summarize_group_results.py \
  --trajectory-index ./artifacts/trajectory_index_2026-07-26.jsonl \
  --group ./splits/group_01.md \
  --easy-group ./splits/group_01_easy.md \
  --hard-group ./splits/group_01_hard.md \
  --output-json ./docs/group1_snapshot_recomputed.json
```

For the July 26, 2026 snapshot, the recomputed headline numbers should be:

- `group1-150`: `115 / 150` = `76.7%`
- `easy`: `86 / 91` = `94.5%`
- `hard`: `29 / 59` = `49.2%`

## Why this is enough

`trajectory_index_2026-07-26.jsonl` is the compact run-level source of truth used by the public summarization scripts. It contains the per-run task ids, timestamps, statuses, and verdict aggregates needed to reconstruct the latest-task and ever-task metrics for the published splits.

## What is not included

This repo does not publish raw evaluation payloads such as PoC binaries, local sqlite databases, or full run directories. Those are much heavier and are not required to recompute the public aggregate metrics.
