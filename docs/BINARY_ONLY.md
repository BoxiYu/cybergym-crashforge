# Binary-Only Mode

Binary-only mode is the recommended way to run CrashForge if your goal is leaderboard-style PoC generation and verification rather than full container-environment debugging.

## What changes

In image mode, the server validates PoCs against per-task runtime images such as:

- `n132/arvo:<id>-vul`
- `n132/arvo:<id>-fix`
- `cybergym/oss-fuzz:<id>-vul`
- `cybergym/oss-fuzz:<id>-fix`

In binary-only mode, the server instead mounts prepackaged binaries and runtime assets from `cybergym-server-data` and runs them inside a smaller runner container.

## Why switch

- Much lower storage requirement than full task-image mode
- No need to pull per-task `vul` and `fix` images
- Better fit for large retry campaigns such as `group1-150`

## What you still need

- `cybergym_data` for task generation and code analysis
- Docker
- `cybergym-server-data`
- the base runner images pulled by `scripts/server_data/download_binary_only_runners.py`

## Server startup

Start the local server with `--binary_dir`:

```bash
python -m cybergym.server \
  --host 127.0.0.1 \
  --port 18667 \
  --mask_map_path mask_map.json \
  --log_dir ./server_poc_benchboost \
  --db_path ./server_poc_benchboost/poc.db \
  --binary_dir ./cybergym-server-data
```

CrashForge detects this automatically from `/healthz`. When the server exposes a non-null `binary_dir`, the rescue pipeline switches runtime checks from per-task image mode to binary-asset mode.

## Pipeline impact

Recommended in binary-only mode:

1. build retry queue
2. launch queue
3. refresh trajectory index
4. summarize results
5. for leaderboard campaigns, prefer the split-wave helpers and campaign exporters in
   [LEADERBOARD_WORKFLOW.md](LEADERBOARD_WORKFLOW.md)

Usually unnecessary in binary-only mode:

- `scripts/server_data/download_missing_images.py`
- `scripts/refresh_missing_image_runnable_queue.py`
- other missing-image warmup or backfill loops

Those helpers are mainly for image-mode campaigns where per-task Docker images are missing. In binary-only mode the main prerequisite becomes binary asset completeness under `cybergym-server-data`.

## Preflight expectations

For each task family, CrashForge expects:

- `arvo/<id>/vul` and `arvo/<id>/fix` directories with binary, libs, and `out/`
- `oss-fuzz/<id>/vul` and `oss-fuzz/<id>/fix` directories with `metadata.json` and `out/`

It may still require one or more base runner images, but not the task-specific runtime images.

## Verification artifacts

In binary-only leaderboard mode, the authoritative verification artifacts live in
the server `log_dir`:

- `poc.db`
- bucketed `poc_id` directories containing `poc.bin`
- `output.vul`
- `output.fix`

Manual re-verification uses those saved artifacts rather than reconstructing the
candidate from a run directory. See [LEADERBOARD_WORKFLOW.md](LEADERBOARD_WORKFLOW.md).
