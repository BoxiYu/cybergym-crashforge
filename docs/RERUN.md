# Rerun Guide

This guide is for people who want to rerun the public CrashForge workflow instead of only recomputing the published metrics from frozen artifacts.

## What this reproduces

This repo supports two different reproducibility targets:

1. Metric recomputation from frozen outputs  
   This is exact for the published snapshot and is documented in [REPRODUCIBILITY.md](REPRODUCIBILITY.md).

2. Fresh benchmark reruns using the public harness  
   This reproduces the workflow, task slice, and aggregation logic, but not necessarily the exact same per-task outcomes. Model behavior, Codex CLI revisions, Docker image changes, and server-side timing can all shift results.

## Environment snapshot

The public snapshot published on July 26, 2026 was organized from an environment with:

- Python `3.12.3`
- `codex-cli 0.145.0`
- Docker `29.3.0`

You also need:

- a working local CyberGym checkout
- the CyberGym dataset and server data
- a local CyberGym submission server
- a PoC sqlite database used by that server

## Minimal rerun path

### 1. Create the Python environment

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

### 2. Start the local CyberGym server in binary-only mode

From the parent CyberGym workspace:

```bash
python -m cybergym.server \
  --host 127.0.0.1 \
  --port 18667 \
  --mask_map_path mask_map.json \
  --log_dir ./server_poc_benchboost \
  --db_path ./server_poc_benchboost/poc.db \
--binary_dir ./cybergym-server-data
```

Pull the runner image set first:

```bash
python scripts/server_data/download_binary_only_runners.py
```

### 3. Build a rerun queue for the public `group1-150` slice

```bash
python scripts/build_static_retry_queue.py \
  --trajectory-index ./artifacts/trajectory_index_2026-07-26.jsonl \
  --task-source ./rerun/task_sources/group1_150.json \
  --results-root ./codex_rescue_runs_local \
  --output-manifest-dir /tmp/crashforge_rerun/manifests \
  --output-queue-file /tmp/crashforge_rerun/queue.json \
  --output-root ./codex_rescue_runs_local/group1_rerun_single \
  --server http://127.0.0.1:18667 \
  --data-dir ../cybergym_data/data \
  --campaign group1_rerun
```

This rebuilds manifests using the frozen trajectory index as the retry-memory source.

Because the server is in binary-only mode, you do not need the full missing-image backfill workflow for per-task `vul` and `fix` images.

### 4. Launch the queue

```bash
python scripts/rescue_queue_launcher.py \
  --queue-file /tmp/crashforge_rerun/queue.json \
  --server http://127.0.0.1:18667 \
  --data-dir ../cybergym_data/data \
  --pocdb-path ../server_poc_benchboost/poc.db \
  --max-active-runners 4 \
  --poll-seconds 15 \
  --scheduler-log /tmp/crashforge_rerun/scheduler.log \
  --run-log-dir /tmp/crashforge_rerun/logs \
  --results-root ./codex_rescue_runs_local
```

### 5. Refresh the index and summarize the rerun

```bash
python scripts/refresh_rescue_trajectory_index.py \
  --results-root ./codex_rescue_runs_local \
  --output-jsonl ./codex_rescue_runs_local/trajectory_index.jsonl \
  --summary-json ./codex_rescue_runs_local/trajectory_summary.json
```

```bash
python scripts/summarize_group_results.py \
  --trajectory-index ./codex_rescue_runs_local/trajectory_index.jsonl \
  --group ./splits/group_01.md \
  --easy-group ./splits/group_01_easy.md \
  --hard-group ./splits/group_01_hard.md \
  --output-json ./docs/group1_rerun_snapshot.json
```

## Smaller public rerun slices

If you do not want to rerun the full `group1-150`, use one of the smaller task sources in `rerun/task_sources/`:

- `group1_easy.json`
- `group1_hard.json`
- `group1_easy_tail_2026-07-26.json`
- `focus123_local_deep_dive_2026-07-26.json`
- `focus123_freeze_for_now_2026-07-26.json`
- `focus123_grok_handoff_2026-07-26.json`

## Practical caveat

The public snapshot numbers are safest to cite from the frozen artifact path. Fresh reruns should be treated as workflow reproduction, not as guaranteed bit-for-bit outcome reproduction.
