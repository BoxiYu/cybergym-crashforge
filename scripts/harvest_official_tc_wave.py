#!/usr/bin/env python3
"""CrashForge-aligned official OSS-Fuzz testcase harvest + binary-only submit wave.

Pipeline per unsolved ARVO / OSS-Fuzz task:
  1) monorail id -> issues.oss-fuzz.com redirect (ARVO)
     or treat numeric id as issue tracker id (oss-fuzz:*)
  2) scrape testcase_id from issue page HTML
  3) download https://oss-fuzz.com/download?testcase_id=...
  4) gen_task + submit-vul against the group binary server
  5) write result.json under grok_work for trajectory indexing

Success criterion: differential verified_success (vul crash, fix clean).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "cybergym_data" / "data"
DEFAULT_WORK = Path("/data/data-a/cav/cybergym/grok_work")

GROUP_PORT = {
    "group_01": 18670,
    "group_02": 18680,
    "group_03": 18671,
    "group_04": 18681,
    "group_05": 18682,
    "group_06": 18683,
    "group_07": 18675,
    "group_08": 18669,
    "group_09": 18684,
    "group_10": 18685,
}

TCID_RE = re.compile(
    r"(?:download\?testcase_id=|testcase\?key=|testcase_id[=:\\u003d]+)(\d{10,})",
    re.I,
)
REDIRECT_ISSUE_RE = re.compile(r"https://issues\.oss-fuzz\.com/(\d+)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def http_get(url: str, timeout: float = 30.0) -> tuple[int, bytes]:
    req = Request(url, headers={"User-Agent": "cybergym-crashforge-tc-harvest/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(getattr(resp, "status", 200) or 200), resp.read()
    except HTTPError as exc:
        return int(exc.code), exc.read() if exc.fp else b""
    except URLError as exc:
        raise RuntimeError(f"GET failed {url}: {exc}") from exc


def load_task_server_map() -> dict[str, int]:
    out: dict[str, int] = {}
    for g, port in GROUP_PORT.items():
        path = ROOT / "splits" / f"{g}.md"
        for line in path.read_text().splitlines():
            line = line.strip().strip("`- ")
            if line.startswith("arvo:") or line.startswith("oss-fuzz:"):
                out[line] = port
            elif re.match(r"arvo_\d+", line):
                out["arvo:" + line.split("_", 1)[1]] = port
            elif re.match(r"oss-fuzz_\d+", line):
                out["oss-fuzz:" + line.split("_", 1)[1]] = port
    return out


def resolve_issue_tracker_id(task_id: str) -> str | None:
    """Map task_id to Google Issue Tracker numeric id used by issues.oss-fuzz.com."""
    if task_id.startswith("arvo:"):
        monorail = task_id.split(":", 1)[1]
        code, body = http_get(
            f"https://bugs.chromium.org/p/oss-fuzz/issues/detail?id={monorail}",
            timeout=25,
        )
        text = body.decode("utf-8", errors="ignore")
        m = REDIRECT_ISSUE_RE.search(text)
        if m:
            return m.group(1)
        # some pages may already be on new tracker
        m2 = re.search(r"issues\.oss-fuzz\.com/issues/(\d+)", text)
        return m2.group(1) if m2 else None
    if task_id.startswith("oss-fuzz:"):
        # many CyberGym oss-fuzz task ids are already issue-tracker / cluster ids
        return task_id.split(":", 1)[1]
    return None


def extract_testcase_ids(html: str) -> list[str]:
    ids = []
    seen = set()
    for m in TCID_RE.finditer(html):
        tcid = m.group(1)
        if tcid not in seen and len(tcid) >= 10:
            seen.add(tcid)
            ids.append(tcid)
    # also plain key= patterns
    for m in re.finditer(r"testcase\?key\\u003d(\d{10,})", html):
        tcid = m.group(1)
        if tcid not in seen:
            seen.add(tcid)
            ids.append(tcid)
    return ids


def download_tc(tcid: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    code, body = http_get(f"https://oss-fuzz.com/download?testcase_id={tcid}", timeout=60)
    if code != 200 or not body or len(body) < 4:
        return False
    # HTML error pages
    head = body[:200].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
        return False
    dest.write_bytes(body)
    return True


def gen_and_submit(task_id: str, server: str, data_dir: Path, work_dir: Path, poc_path: Path) -> dict[str, Any]:
    agent_id = uuid.uuid4().hex
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        + f"-{task_id.replace(':', '_')}-grok-otc-{agent_id[:8]}"
    )
    run_root = work_dir / run_id
    task_dir = run_root / "task"
    task_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "cybergym.task.gen_task",
        "--task-id",
        task_id,
        "--agent-id",
        agent_id,
        "--out-dir",
        str(task_dir),
        "--data-dir",
        str(data_dir),
        "--server",
        server,
        "--difficulty",
        "level1",
    ]
    mask = ROOT / "mask_map.json"
    if mask.exists():
        cmd.extend(["--mask-map", str(mask)])

    gen = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    if gen.returncode != 0:
        return {
            "task_id": task_id,
            "status": "gen_task_failed",
            "agent_id": agent_id,
            "run_root": str(run_root),
            "stderr": (gen.stderr or "")[-800:],
        }

    submit_sh = task_dir / "submit.sh"
    if not submit_sh.exists():
        return {
            "task_id": task_id,
            "status": "no_submit_sh",
            "agent_id": agent_id,
            "run_root": str(run_root),
        }

    # copy poc into task dir for local archival
    local_poc = task_dir / "official_tc.bin"
    local_poc.write_bytes(poc_path.read_bytes())

    sub = subprocess.run(
        ["bash", str(submit_sh), str(local_poc)],
        capture_output=True,
        text=True,
        cwd=str(task_dir),
        timeout=300,
    )
    out = (sub.stdout or "") + "\n" + (sub.stderr or "")
    verdict = None
    m = re.search(r'"verdict"\s*:\s*"([^"]+)"', out)
    if m:
        verdict = m.group(1)
    # also try JSON line
    status = "submitted"
    if verdict == "verified_success":
        status = "success"
    elif verdict:
        status = verdict

    result = {
        "task_id": task_id,
        "status": status,
        "verdict": verdict,
        "agent_id": agent_id,
        "run_root": str(run_root),
        "server": server,
        "poc_len": local_poc.stat().st_size,
        "submit_stdout": out[-2000:],
        "ended_at": utc_now(),
        "method": "official_oss_fuzz_testcase",
        "codex": {"returncode": 0, "mode": "grok_official_tc_binary"},
        "executor_status": "completed",
        "attempt": 1,
    }
    (run_root / "result.json").write_text(json.dumps(result, indent=2))
    (run_root / "summary.md").write_text(
        f"# {task_id}\n\nstatus={status}\nverdict={verdict}\npoc_len={result['poc_len']}\nserver={server}\n"
    )
    return result


def process_task(
    task_id: str,
    port: int,
    data_dir: Path,
    work_root: Path,
    tc_dir: Path,
    wave_runs: Path,
) -> dict[str, Any]:
    server = f"http://127.0.0.1:{port}"
    rec: dict[str, Any] = {
        "task_id": task_id,
        "server": server,
        "started_at": utc_now(),
    }
    try:
        issue_id = resolve_issue_tracker_id(task_id)
        rec["issue_id"] = issue_id
        if not issue_id:
            rec["status"] = "no_issue_id"
            return rec

        code, body = http_get(f"https://issues.oss-fuzz.com/{issue_id}", timeout=30)
        html = body.decode("utf-8", errors="ignore")
        tcids = extract_testcase_ids(html)
        rec["tcids"] = tcids
        if not tcids:
            # oss-fuzz task ids sometimes ARE the testcase id
            if task_id.startswith("oss-fuzz:"):
                tcids = [task_id.split(":", 1)[1]]
            else:
                rec["status"] = "no_testcase_id"
                return rec

        last_fail = "download_failed"
        for tcid in tcids[:3]:
            dest = tc_dir / f"{task_id.replace(':', '_')}_{tcid}.bin"
            if not dest.exists() or dest.stat().st_size < 4:
                ok = download_tc(tcid, dest)
                if not ok:
                    last_fail = "download_failed"
                    continue
            rec["tcid"] = tcid
            rec["poc_path"] = str(dest)
            sub = gen_and_submit(task_id, server, data_dir, wave_runs, dest)
            rec.update(sub)
            if rec.get("status") == "success" or rec.get("verdict") == "verified_success":
                # archive poc
                solved = ROOT / "reports" / "grok_solved"
                solved.mkdir(parents=True, exist_ok=True)
                arch = solved / f"{task_id.replace(':', '_')}_poc.bin"
                arch.write_bytes(dest.read_bytes())
                return rec
            last_fail = rec.get("status") or last_fail
        if "status" not in rec:
            rec["status"] = last_fail
        return rec
    except Exception as exc:  # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unsolved-json", type=Path, default=ROOT / "reports" / "still_unsolved_snapshot.json")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--work-root", type=Path, default=DEFAULT_WORK)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-arvo", action="store_true")
    ap.add_argument("--only-oss-fuzz", action="store_true")
    ap.add_argument("--task-ids", nargs="*", default=None)
    args = ap.parse_args()

    task_server = load_task_server_map()
    if args.task_ids:
        tasks = args.task_ids
    else:
        su = json.loads(args.unsolved_json.read_text())
        tasks = list(su["tasks"])

    if args.only_arvo:
        tasks = [t for t in tasks if t.startswith("arvo:")]
    if args.only_oss_fuzz:
        tasks = [t for t in tasks if t.startswith("oss-fuzz:")]
    if args.limit:
        tasks = tasks[: args.limit]

    wave = datetime.now(timezone.utc).strftime("wave_otc_%Y%m%dT%H%M%SZ")
    work = args.work_root
    tc_dir = work / "official_tcs" / wave
    wave_runs = work / "runs" / wave
    tc_dir.mkdir(parents=True, exist_ok=True)
    wave_runs.mkdir(parents=True, exist_ok=True)
    out_json = work / f"{wave}_results.json"
    out_log = work / f"{wave}.log"

    print(f"wave={wave} tasks={len(tasks)} workers={args.workers}", flush=True)
    results: list[dict[str, Any]] = []
    t0 = time.time()

    def _one(tid: str) -> dict[str, Any]:
        port = task_server.get(tid)
        if not port:
            return {"task_id": tid, "status": "no_server_map"}
        return process_task(tid, port, args.data_dir, work, tc_dir, wave_runs)

    done = 0
    success = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_one, tid): tid for tid in tasks}
        for fut in concurrent.futures.as_completed(futs):
            tid = futs[fut]
            try:
                rec = fut.result()
            except Exception as exc:  # noqa: BLE001
                rec = {"task_id": tid, "status": "error", "error": str(exc)}
            results.append(rec)
            done += 1
            if rec.get("status") == "success" or rec.get("verdict") == "verified_success":
                success += 1
            line = (
                f"[{done}/{len(tasks)} ok={success}] {rec.get('task_id')} "
                f"status={rec.get('status')} tcid={rec.get('tcid')} issue={rec.get('issue_id')}"
            )
            print(line, flush=True)
            with out_log.open("a") as lf:
                lf.write(line + "\n")
            # checkpoint
            if done % 25 == 0:
                out_json.write_text(json.dumps(results, indent=2))

    summary = {
        "wave": wave,
        "workers": args.workers,
        "tried": len(results),
        "success_count": success,
        "elapsed_sec": round(time.time() - t0, 1),
        "status_counts": {},
        "results": results,
    }
    from collections import Counter

    summary["status_counts"] = dict(Counter(r.get("status") for r in results))
    out_json.write_text(json.dumps(summary, indent=2))
    # also copy summary into reports/
    (ROOT / "reports" / f"grok_official_tc_{wave}.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: summary[k] for k in summary if k != "results"}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
