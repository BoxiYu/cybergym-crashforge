# Leaderboard Workflow

CrashForge now includes the public-facing version of the leaderboard tooling we
use locally for large binary-only CyberGym campaigns.

This document covers:

- which scripts make up the current workflow
- how split-wave campaigns are prepared and launched
- where verified PoCs are stored
- how to manually re-verify a saved PoC
- what to share with a teammate for reproduction

## Scope

The repo now contains two layers of tooling:

1. Core retry/rescue primitives
   - single-task runner
   - retry queue builder
   - bounded queue launcher
   - trajectory index refresh
2. Campaign-level leaderboard helpers
   - split-group wave preparation
   - split-group launch scripts
   - coverage/dashboard/campaign exporters
   - stubborn-failure summaries
   - reference autopilot and `group2_hard` automation scripts

The campaign-level scripts are the same family of tools used in the July 2026
leaderboard push. Some of them are still campaign-shaped rather than perfectly
generic, but they are included so contributors can see and reuse the real
workflow rather than a simplified toy version.

## Main components

### Core execution

- `scripts/codex_rescue_runner.py`
- `scripts/build_static_retry_queue.py`
- `scripts/rescue_queue_launcher.py`
- `scripts/refresh_rescue_trajectory_index.py`
- `scripts/verify_agent_result.py`

### Split-wave preparation and launch

- `scripts/project_routing.py`
- `scripts/prepare_split_group_fresh_wave.py`
- `scripts/prepare_split_group_retry_wave.py`
- `scripts/launch_split_group_binary_wave.sh`
- `scripts/launch_split_group_retry_wave.sh`
- `scripts/control_split_wave_launcher.sh`

### Campaign status and exports

- `scripts/export_split_group_coverage.py`
- `scripts/export_split_wave_dashboard.py`
- `scripts/export_full_benchmark_campaign.py`
- `scripts/export_static_retry_queue_summary.py`
- `scripts/export_stubborn_failure_report.py`
- `scripts/export_stubborn_failure_task_source.py`

### Reference higher-level automation

- `scripts/split_wave_autopilot.py`
- `scripts/control_split_wave_autopilot.sh`
- `scripts/launch_group2_hard_binary_wave.sh`
- `scripts/monitor_group2_hard_wave.sh`
- `scripts/control_group2_hard_followup.sh`
- `scripts/control_group2_hard_monitor.sh`
- `scripts/control_group2_hard_post_followup.sh`

## Binary-only assumptions

For the leaderboard workflow, assume:

- local CyberGym server started with `--binary_dir ./cybergym-server-data`
- task assets under `./cybergym_data`
- run outputs under `./codex_rescue_runs_local`
- verification artifacts under `./server_poc*`

See [BINARY_ONLY.md](BINARY_ONLY.md) for the underlying server mode.

## Split-wave workflow

### 1. Refresh the trajectory index

```bash
python scripts/refresh_rescue_trajectory_index.py \
  --results-root ./codex_rescue_runs_local \
  --output-jsonl ./codex_rescue_runs_local/trajectory_index.jsonl \
  --summary-json ./codex_rescue_runs_local/trajectory_summary.json
```

### 2. Prepare a fresh split-group wave

```bash
python scripts/prepare_split_group_fresh_wave.py \
  --group-file ./splits/group_04.md \
  --wave-dir ./reports/group4_wave \
  --output-root ./codex_rescue_runs_local/group4_wave_single \
  --server http://127.0.0.1:18681 \
  --data-dir ./cybergym_data/data \
  --campaign group4_wave
```

This uses `project_routing.py` to sort tasks by repo/project yield before
writing manifests.

### 3. Launch the wave

```bash
bash scripts/launch_split_group_binary_wave.sh \
  ./splits/group_04.md \
  group4_wave
```

### 4. Prepare a retry-only wave over the still-unsolved tail

```bash
python scripts/prepare_split_group_retry_wave.py \
  --group-file ./splits/group_04.md \
  --wave-dir ./reports/group4_wave \
  --output-root ./codex_rescue_runs_local/group4_wave_single \
  --server http://127.0.0.1:18681 \
  --data-dir ./cybergym_data/data \
  --campaign group4_retry
```

### 5. Export coverage and campaign summaries

```bash
python scripts/export_split_group_coverage.py \
  --output-json ./reports/split_group_coverage.json
```

```bash
python scripts/export_split_wave_dashboard.py \
  --output-json ./reports/split_wave_dashboard.json
```

```bash
python scripts/export_full_benchmark_campaign.py \
  --dashboard-json ./reports/split_wave_dashboard.json \
  --coverage-json ./reports/split_group_coverage.json \
  --output-json ./reports/full_benchmark_campaign.json \
  --output-md ./reports/full_benchmark_campaign.md
```

## Official Level1 queue mix

For the official-rule public push, we now recommend two parallel queue types:

1. `frontier`: new tasks chosen from projects that already convert well
2. `failed_rerun`: clean, evidence-complete submitted failures that are close to a counted success

The frontier selector is:

- `scripts/build_official_priority_wave.py`

It now supports `--max-per-project` so one strong repository does not dominate
the full batch.

Convenience launchers:

```bash
bash scripts/launch_official_frontier_wave.sh
```

```bash
bash scripts/launch_official_failed_rerun_wave.sh
```

These are intended for the official-only lane. They should be paired with the
local rule that the agent only sees the current task's allowed Level1 inputs,
not other run directories, saved PoCs, patch files, or fix-side assets.

## Where verified PoCs are stored

The authoritative PoC store is the server `log_dir`, not the run directory.

Typical layout:

```text
server_poc_group_04_binary/
├── poc.db
├── server.log
└── <poc_id[:2]>/<poc_id[2:4]>/<poc_id>/
    ├── poc.bin
    ├── output.vul
    └── output.fix
```

The run-side artifacts under `codex_rescue_runs_local/...` are still important
for debugging and writeups, but the saved `poc.bin` under `server_poc_*` is what
manual re-verification reuses later.

## Manual verification

Verify all PoCs tied to one `agent_id`:

```bash
export CYBERGYM_API_KEY='cybergym-030a0cd7-5908-4862-8ab9-91f2bfc7b56d'

python scripts/verify_agent_result.py \
  --server http://127.0.0.1:18681 \
  --pocdb_path ./server_poc_group_04_binary/poc.db \
  --agent_id codex-rescue-20260727163557-2a1ec670
```

Re-verify one saved PoC by `poc_id`:

```bash
curl -sS -X POST http://127.0.0.1:18681/verify-poc \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $CYBERGYM_API_KEY" \
  -d '{"poc_id":"replace_with_real_poc_id"}'
```

Find `poc_id` values in the sqlite DB:

```bash
sqlite3 ./server_poc_group_04_binary/poc.db \
  "select task_id, agent_id, poc_id, vul_exit_code, fix_exit_code from poc_records;"
```

## What to hand to a teammate

Minimum package for reproducible re-verification:

- `task_id`
- `poc.bin`
- `output.vul`
- `output.fix`
- the relevant `poc.db` row or exported metadata
- the matching `cybergym-server-data` version

Full debugging package:

- everything above
- `codex_rescue_runs_local/.../result.json`
- `codex_rescue_runs_local/.../summary.json`
- `codex_rescue_runs_local/.../codex_events.jsonl`
- `codex_rescue_runs_local/.../retained_task_files/`

## Common mistakes

- treating `result.json` alone as proof of a durable solve
- keeping only run-side artifacts and losing `server_poc_*`
- verifying against the wrong loopback server or wrong `poc.db`
- moving active `server_poc_*` directories during a live campaign
