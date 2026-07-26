# Contributing

## Scope

CrashForge is the standalone retry-and-scheduling harness extracted from a larger CyberGym workflow. The public repo should stay focused on:

- queue construction
- queue execution
- result indexing
- snapshot summarization
- Docker image backfill helpers

Avoid adding benchmark-internal data dumps, local run artifacts, or machine-specific helper scripts unless they are required to reproduce the public workflow.

## Development

Use Python 3.11+.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Pull Request Guidelines

- Keep CLI defaults generic. Avoid internal wave names like `focus123` or `wave9` in user-facing defaults unless they are part of a documented public benchmark split.
- Prefer repo-relative paths over machine-specific or workspace-specific paths.
- When updating public results, update both the prose snapshot in `docs/RESULTS_*.md` and the machine-readable snapshot JSON in `docs/`.
- Preserve the historical `rescue` naming inside scripts unless there is a strong reason to rename code. Public docs can continue to use the `CrashForge` name.

## Generated Artifacts

Do not commit local run outputs such as:

- `codex_rescue_runs_local/`
- scheduler logs
- queue state files
- temporary manifests or partition outputs
