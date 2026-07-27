# CyberGym CrashForge

Standalone harness for running large CyberGym crash-generation campaigns, keeping failure memory across retries, auto-scheduling runnable tasks, backfilling missing Docker images, and indexing results into one trajectory view.

Recommended runtime mode for public reproduction is now binary-only server mode.

This repo is the stripped-down leaderboard pipeline rather than the full experiment monorepo. The intent is simple:

- generate retry queues from prior runs
- launch a high-concurrency campaign wave
- keep missing-image tasks moving
- refresh a unified result index
- compute pass-rate snapshots for `group1-150`

The repo is intentionally light on framework code. Most files are plain Python entrypoints that can be run directly.

## What is included

- `scripts/codex_rescue_runner.py`: single-task rescue runner
- `scripts/build_static_retry_queue.py`: build retry queue from `trajectory_index.jsonl`
- `scripts/rescue_queue_launcher.py`: bounded concurrent launcher for a queue
- `scripts/refresh_missing_image_runnable_queue.py`: partition missing-image tasks and launch runnable ones automatically
- `scripts/server_data/download_missing_images.py`: parallel Docker image pull worker
- `scripts/server_data/download_binary_only_runners.py`: pull the small runner-image set needed for binary-only validation
- `scripts/refresh_rescue_trajectory_index.py`: rebuild the run index
- `scripts/summarize_group_results.py`: compute pass-rate snapshots from the trajectory index
- `splits/group_01*.md`: the current `group1-150`, `easy`, and `hard` task lists
- `docs/METHOD.md`: the retry-memory and scheduling strategy
- `docs/RESULTS_2026-07-26.md`: current experiment snapshot

Public branding uses `CrashForge`. Internal script names still contain `rescue` because that is the historical implementation vocabulary and changing it would create unnecessary churn.

## Repo layout

- `scripts/`: queue builders, launchers, indexers, and monitoring helpers
- `scripts/server_data/`: Docker image backfill helpers
- `splits/`: public task lists used for reproducible snapshots
- `docs/`: method notes and dated public result snapshots
- `artifacts/`: frozen index outputs used to recompute published metrics
- `rerun/`: public task sources for replaying the main benchmark slices

## Environment

Python 3.11+ is recommended.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

You also need:

- a working `codex` CLI on `PATH`
- Docker
- a local CyberGym server endpoint
- a CyberGym data directory with task assets
- a PoC sqlite DB used by the local server

## Recommended Mode

For leaderboard-style campaigns, prefer binary-only server mode over full task-image mode.

See [docs/BINARY_ONLY.md](docs/BINARY_ONLY.md).

Minimal preparation:

```bash
python scripts/server_data/download_binary_only_runners.py
```

Then start the CyberGym server with `--binary_dir ./cybergym-server-data`.

## Core workflow

This is the recommended binary-only workflow.

1. Refresh the trajectory index.

```bash
python scripts/refresh_rescue_trajectory_index.py \
  --results-root ./codex_rescue_runs_local \
  --output-jsonl ./codex_rescue_runs_local/trajectory_index.jsonl \
  --summary-json ./codex_rescue_runs_local/trajectory_summary.json
```

2. Build a retry queue from a task source.

```bash
python scripts/build_static_retry_queue.py \
  --trajectory-index ./codex_rescue_runs_local/trajectory_index.jsonl \
  --task-source ./task_source.json \
  --results-root ./codex_rescue_runs_local \
  --output-manifest-dir /tmp/rescue_wave/manifests \
  --output-queue-file /tmp/rescue_wave/queue.json \
  --output-root ./codex_rescue_runs_local/group1_wave_single \
  --server http://127.0.0.1:18667 \
  --data-dir ./cybergym_data/data \
  --campaign group1_wave
```

3. Launch the queue with controlled concurrency.

```bash
python scripts/rescue_queue_launcher.py \
  --queue-file /tmp/rescue_wave/queue.json \
  --results-root ./codex_rescue_runs_local \
  --server http://127.0.0.1:18667 \
  --data-dir ./cybergym_data/data \
  --pocdb-path ./server_poc/poc.db \
  --max-active-runners 16 \
  --poll-seconds 15 \
  --scheduler-log /tmp/rescue_wave/scheduler.log
```

4. Refresh the index and recompute the snapshot.

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
  --output-json ./docs/group1_snapshot_recomputed.json
```

## Image-mode only workflow

If you are running full task-image mode instead of binary-only, image holes can block progress. In that case, keep a runnable queue refreshed while pulls happen in parallel.

```bash
python scripts/server_data/download_missing_images.py \
  --missing-jsonl /tmp/rescue_wave/missing_tasks.jsonl \
  --tasks-file ./cybergym_data/tasks.json \
  --output-dir /tmp/rescue_wave/pull_missing_exec \
  --execute \
  --max-workers 14
```

```bash
python scripts/refresh_missing_image_runnable_queue.py \
  --manifest /tmp/rescue_wave/group1_manifest.jsonl \
  --partition-output-dir /tmp/rescue_wave/partition \
  --output-manifest-dir /tmp/rescue_wave/runnable_manifests \
  --output-queue-file /tmp/rescue_wave/runnable_queue.json \
  --output-root ./codex_rescue_runs_local/group1_wave_single \
  --results-root ./codex_rescue_runs_local \
  --server http://127.0.0.1:18667 \
  --data-dir ./cybergym_data/data \
  --pocdb-path ./server_poc/poc.db \
  --max-active-runners 16 \
  --skip-latest-no-vul
```

## Current results

Current public snapshot is in [docs/RESULTS_2026-07-26.md](docs/RESULTS_2026-07-26.md).

Headline numbers for `group1-150` as of July 26, 2026:

- latest success: `115 / 150` = `76.7%`
- easy: `86 / 91` = `94.5%`
- hard: `29 / 59` = `49.2%`

These numbers are a dated snapshot, not a live dashboard. Recompute from your local trajectory index before citing them externally.

If you only want to reproduce the published snapshot instead of rerunning the whole campaign, use the frozen outputs documented in [docs/REPRODUCIBILITY.md](docs/REPRODUCIBILITY.md).
If you want to rerun the public workflow, start from [docs/RERUN.md](docs/RERUN.md).

## Recompute the published snapshot

```bash
python scripts/summarize_group_results.py \
  --trajectory-index ./artifacts/trajectory_index_2026-07-26.jsonl \
  --group ./splits/group_01.md \
  --easy-group ./splits/group_01_easy.md \
  --hard-group ./splits/group_01_hard.md \
  --output-json ./docs/group1_snapshot_recomputed.json
```

## Collaboration target

The immediate target for contributors is to push the hard bucket higher without regressing the easy bucket. The main bottlenecks we saw were:

- repeated `no_vul_crash` plateaus
- image availability gaps
- a small number of non-differential or no-submission tails

The harness already preserves failure history in manifests, so contributors can continue from prior attempts rather than restarting blind.

See [CONTRIBUTING.md](CONTRIBUTING.md) for repo hygiene and contribution conventions.
