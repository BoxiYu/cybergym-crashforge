# CyberGym CrashForge

Standalone harness for running large CyberGym crash-generation campaigns, keeping failure memory across retries, auto-scheduling runnable tasks, backfilling missing Docker images, and indexing results into one trajectory view.

Recommended runtime mode for public reproduction is now binary-only server mode.

The repo now also includes the public-facing split-wave and verification workflow
used in the July 2026 leaderboard push.

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
- `scripts/project_routing.py`: repo/project-aware routing metadata and retry policy helpers
- `scripts/prepare_split_group_fresh_wave.py`: prepare a fresh split-group binary wave
- `scripts/prepare_split_group_retry_wave.py`: prepare a retry-only split-group wave
- `scripts/launch_split_group_binary_wave.sh`: launch a fresh split-group wave end to end
- `scripts/launch_split_group_retry_wave.sh`: launch a retry split-group wave end to end
- `scripts/export_split_group_coverage.py`: summarize canonical group coverage
- `scripts/export_split_wave_dashboard.py`: summarize active wave state
- `scripts/export_full_benchmark_campaign.py`: build a campaign-level markdown/json status report
- `scripts/export_stubborn_failure_report.py`: summarize the stubborn unsolved tail
- `scripts/export_stubborn_failure_task_source.py`: split stubborn tails into actionable task-source packets
- `scripts/build_official_priority_wave.py`: build a new-task official frontier queue from high-yield projects
- `scripts/build_official_failed_rerun_wave.py`: build an official rerun queue from clean submitted failures
- `scripts/build_official_mismatch_wave.py`: build an official mismatch-focused rerun queue from post-template local-vs-server gaps
- `scripts/export_official_quick_wins.py`: build operator-facing quick-win remediation queues from post-template official reports
- `scripts/export_official_quick_win_reruns.py`: turn those quick-win queues into rerunnable task lists
- `scripts/launch_official_frontier_wave.sh`: stage and launch the official frontier lane
- `scripts/launch_official_failed_rerun_wave.sh`: stage and launch the official failed-rerun lane
- `scripts/launch_official_mismatch_wave.sh`: stage and launch the official mismatch lane
- `scripts/launch_official_quick_win_priority_a.sh`: launch the format/submission-recovery quick-win lane
- `scripts/launch_official_quick_win_priority_b.sh`: launch the bundled-target-mismatch quick-win lane
- `scripts/export_official_submission_blockers.py`: compute official-rule blockers against the current target rate
- `scripts/export_official_post_template_watch.py`: watch post-template official conversion health
- `scripts/export_official_control_panel.py`: publish one compact official monitoring summary
- `scripts/split_wave_autopilot.py`: reference autopilot for multi-wave rebalancing
- `scripts/summarize_group_results.py`: compute pass-rate snapshots from the trajectory index
- `splits/group_*.md`: public split-group task lists used by the campaign helpers
- `docs/METHOD.md`: the retry-memory and scheduling strategy
- `docs/LEADERBOARD_WORKFLOW.md`: current leaderboard workflow, PoC storage, and manual verification flow
- `docs/RESULTS_2026-07-28.md`: latest full-benchmark experiment snapshot
- `docs/RESULTS_2026-07-26.md`: older `group1-150` snapshot

Public branding uses `CrashForge`. Internal script names still contain `rescue` because that is the historical implementation vocabulary and changing it would create unnecessary churn.

## Official campaign goal

When this repo is used for the current public leaderboard push, the operating
goal is:

- run the full `1507`-task CyberGym benchmark under the public `SUBMISSION.md`
  and `FAQ.md` rules
- improve official counted `final-submission` success rather than raw local
  `verified_success`
- use only our own legitimately produced successes, failures, and verification
  outputs for self-iteration
- keep enough trajectories, verification artifacts, and result summaries to
  support later submission packaging and review

Current working target: push full-benchmark official success toward `85%`
without using external answers, downloaded official testcases, reused saved
PoCs as agent inputs, patch leakage, or fix-side exposure.

The official frontier selector defaults to the public official scoreboard, and
fresh binary wave launchers default their server log roots to
`server_poc_<wave_name>` so concurrent official waves do not collide on a
shared generic log directory.

The official monitoring helpers also default to the current `85%` public goal
so blocker counts and control-panel summaries stay aligned with the live push.

## Sync cadence

Keep this repo synchronized with the active monorepo workflow:

- update immediately after script or workflow-contract changes
- update after each completed wave if operator instructions or verification
  commands changed
- update daily with the latest operational notes and experiment/result snapshots
- update compliance-facing docs before the next wave whenever a rule boundary
  changes

## Repo layout

- `scripts/`: queue builders, launchers, indexers, and monitoring helpers
- `scripts/server_data/`: Docker image backfill helpers
- `splits/`: public task lists used for reproducible snapshots and split-wave campaigns
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

For the full leaderboard-oriented flow, including split-wave launchers,
verification artifact layout, and teammate handoff guidance, see
[docs/LEADERBOARD_WORKFLOW.md](docs/LEADERBOARD_WORKFLOW.md).

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

## Split-wave leaderboard workflow

The current leaderboard stack adds a repo-aware split-wave layer on top of the
core queue flow:

1. prepare a fresh or retry-only wave from `splits/group_XX*.md`
2. launch that wave in binary-only mode
3. export coverage and dashboard state
4. rebalance follow-up waves with the reference autopilot
5. re-verify saved PoCs from `server_poc_*`

See [docs/LEADERBOARD_WORKFLOW.md](docs/LEADERBOARD_WORKFLOW.md) for the
current workflow and verification commands.

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

Latest public full-benchmark snapshot is in
[docs/RESULTS_2026-07-28.md](docs/RESULTS_2026-07-28.md).

Headline numbers as of July 27, 2026:

- full benchmark success: `1455 / 1507` = `96.5%`
- attempted coverage: `1498 / 1507` = `99.4%`
- remaining tasks: `52`
- unattempted tasks: `9`

The earlier `group1-150` public snapshot is still preserved in
[docs/RESULTS_2026-07-26.md](docs/RESULTS_2026-07-26.md).

These numbers are dated snapshots, not a live dashboard. Recompute from your
local trajectory index and campaign artifacts before citing them externally.

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
