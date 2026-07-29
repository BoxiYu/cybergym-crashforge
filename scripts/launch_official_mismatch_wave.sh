#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SOURCE_DIR="${1:-$ROOT/reports/official_level1_mismatch_wave_2026-07-29_d1_source}"
WAVE_NAME="${2:-official_level1_mismatch_wave_2026-07-29_d1}"

mkdir -p "$SOURCE_DIR"

python3 "$ROOT/scripts/build_official_mismatch_wave.py" \
  --mismatch-json "$ROOT/reports/official_level1_post_template_mismatches_2026-07-29.json" \
  --reports-root "$ROOT/reports" \
  --output-dir "$SOURCE_DIR" \
  --count "${OFFICIAL_MISMATCH_COUNT:-12}"

if [[ ! -s "$SOURCE_DIR/tasks.md" ]]; then
  echo "No official mismatch tasks were selected." >&2
  exit 1
fi

export SPLIT_GROUP_POLICY_PROFILE="${SPLIT_GROUP_POLICY_PROFILE:-official_level1}"
export SPLIT_GROUP_BINARY_PORT="${SPLIT_GROUP_BINARY_PORT:-18853}"
export SPLIT_GROUP_SERVER_LOG_DIR="${SPLIT_GROUP_SERVER_LOG_DIR:-$ROOT/server_poc_${WAVE_NAME}}"
export SPLIT_GROUP_CODEX_TIMEOUT_SECONDS="${SPLIT_GROUP_CODEX_TIMEOUT_SECONDS:-3600}"
export SPLIT_GROUP_MAX_ACTIVE="${SPLIT_GROUP_MAX_ACTIVE:-6}"
export SPLIT_GROUP_GLOBAL_MAX_ACTIVE="${SPLIT_GROUP_GLOBAL_MAX_ACTIVE:-48}"
export SPLIT_GROUP_GLOBAL_RUNNER_HEADROOM="${SPLIT_GROUP_GLOBAL_RUNNER_HEADROOM:-1}"
export SPLIT_GROUP_LOADAVG_LIMIT_FACTOR="${SPLIT_GROUP_LOADAVG_LIMIT_FACTOR:-6.0}"
export SPLIT_GROUP_MIN_MEM_AVAILABLE_MB="${SPLIT_GROUP_MIN_MEM_AVAILABLE_MB:-2048}"
export SPLIT_GROUP_MIN_SWAP_FREE_MB="${SPLIT_GROUP_MIN_SWAP_FREE_MB:-0}"
export SPLIT_GROUP_IGNORE_USAGE_LIMIT="${SPLIT_GROUP_IGNORE_USAGE_LIMIT:-1}"
export SPLIT_GROUP_CAMPAIGN="${SPLIT_GROUP_CAMPAIGN:-$WAVE_NAME}"

exec "$ROOT/scripts/launch_split_group_binary_wave.sh" "$SOURCE_DIR/tasks.md" "$WAVE_NAME"
