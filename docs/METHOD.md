# Method

This harness is built around one assumption: the main failure mode in benchmark solving is not lack of single-shot intelligence, but loss of memory and weak scheduling.

## Core ideas

1. Keep retry memory.

Each retry manifest carries prior status counts, prior verdict counts, and a small set of recent source run roots. The runner uses that to rewrite the next prompt so the agent does not restart blind after repeated `no_vul_crash` or `fix_also_crashes`.

2. Separate queue construction from execution.

`build_static_retry_queue.py` decides what should run next. `rescue_queue_launcher.py` decides how much runs in parallel. That makes it easy to change selection policy without rewriting the runner.

3. Keep runnable tasks flowing even when some tasks are blocked on Docker images.

`refresh_missing_image_runnable_queue.py` partitions a manifest into:

- already solved
- missing-image blocked
- immediately runnable

While `download_missing_images.py` works through image pulls, the refresh loop keeps launching anything that is already unblocked.

4. Rebuild one authoritative trajectory index.

`refresh_rescue_trajectory_index.py` scans result directories into one `trajectory_index.jsonl`. That becomes the source of truth for:

- retry queue generation
- pass-rate snapshots
- active/missing/invalid run detection

## Why this helps leaderboard progress

- It reduces wasted retries on already-solved or already-active tasks.
- It targets plateau states explicitly instead of treating every retry as a fresh attempt.
- It turns image availability from a hard stop into a background pipeline concern.
- It gives a stable metric loop: run wave, refresh index, summarize pass rate, pick the next frontier.

## Practical reading of the metrics

Publicly, we refer to this stack as `CrashForge`. Internally, many script names still use `rescue`; that is an implementation detail rather than the product name.

When this harness improves the board, it usually shows up in this order:

1. attempted coverage rises toward 100%
2. easy bucket saturates
3. hard bucket becomes dominated by `no_vul_crash`
4. future gains depend on better local reasoning over the hard tails, not on more random retries
