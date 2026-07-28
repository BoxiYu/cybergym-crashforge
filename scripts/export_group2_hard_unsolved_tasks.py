from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["task_id\tlatest_status\tlatest_success\tever_success"]
    for row in rows:
        lines.append(
            "\t".join(
                [
                    str(row.get("task_id") or ""),
                    str(row.get("latest_status") or ""),
                    str(int(bool(row.get("latest_success")))),
                    str(int(bool(row.get("ever_success")))),
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export currently-unsolved group2 hard tasks from the compact status snapshot.")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-tsv", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    snapshot = read_json(args.snapshot.resolve())
    rows = [
        row
        for row in (snapshot.get("tasks") or [])
        if isinstance(row, dict) and row.get("latest_status") != "success"
    ]
    payload = {
        "snapshot": str(args.snapshot.resolve()),
        "generated_at": snapshot.get("generated_at"),
        "unsolved_count": len(rows),
        "unsolved_tasks": rows,
    }
    write_json(args.output_json.resolve(), payload)
    write_tsv(args.output_tsv.resolve(), rows)
    print(
        json.dumps(
            {
                "unsolved_count": len(rows),
                "output_json": str(args.output_json.resolve()),
                "output_tsv": str(args.output_tsv.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
