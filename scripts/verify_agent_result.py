import argparse
import json
import logging
import os
import sys
from pathlib import Path

import httpx

API_KEY = os.getenv("CYBERGYM_API_KEY")
API_KEY_NAME = "X-API-Key"
logger = logging.getLogger(__name__)


def load_results(pocdb_path: Path, agent_id: str) -> list[dict]:
    from pocdb import PoCRecord, Session, init_engine

    engine = init_engine(pocdb_path)
    with Session(engine) as session:
        pocs = session.query(PoCRecord).filter(PoCRecord.agent_id == agent_id).all()
        return [poc.to_dict() for poc in pocs]


def run_verify(agent_id: str, server: str) -> dict:
    with httpx.Client(base_url=server, timeout=1200) as client:
        headers = {
            API_KEY_NAME: API_KEY,
        }
        response = client.post(
            "/verify-agent-pocs",
            json={"agent_id": agent_id},
            headers=headers,
        )
        logger.info("Verification response for agent %s: %s %s", agent_id, response.status_code, response.text)
        if response.status_code == 404:
            return {"status": "no_records", "response": response.text}
        response.raise_for_status()
        return {"status": "ok", "response": response.json()}


def validate_results(records: list[dict]) -> list[str]:
    issues = []
    for record in records:
        vul_exit_code = record.get("vul_exit_code")
        fix_exit_code = record.get("fix_exit_code")
        if vul_exit_code not in (None, 0, 300) and fix_exit_code is None:
            issues.append(
                f"poc_id={record.get('poc_id')} crashed on vulnerable target but has no fixed-target verdict yet"
            )
    return issues


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--server",
        type=str,
        required=True,
        help="The server to send the verification request to.",
    )
    parser.add_argument(
        "--agent_id",
        type=str,
        required=True,
        help="The agent ID to verify.",
    )
    parser.add_argument(
        "--pocdb_path",
        type=Path,
        required=True,
        help="The path to the PoC database.",
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    records = load_results(args.pocdb_path, args.agent_id)
    for record in records:
        print(json.dumps(record, default=str))

    if not records:
        sys.exit(0)

    if not API_KEY:
        logger.error("CYBERGYM_API_KEY is required when verification records exist")
        sys.exit(2)

    try:
        verify_result = run_verify(args.agent_id, args.server)
    except httpx.ReadTimeout:
        logger.error("Verification request timed out for agent %s", args.agent_id)
        sys.exit(3)
    except httpx.HTTPStatusError as exc:
        logger.error("Verification request failed for agent %s: %s", args.agent_id, exc)
        sys.exit(4)
    except Exception as exc:  # noqa: BLE001
        logger.error("Error during verification for agent %s: %s", args.agent_id, exc)
        sys.exit(5)

    refreshed_records = load_results(args.pocdb_path, args.agent_id)
    issues = validate_results(refreshed_records)
    for record in refreshed_records:
        print(json.dumps(record, default=str))

    if verify_result["status"] == "no_records" and refreshed_records:
        logger.error("Server reported no records for agent %s but local database still has records", args.agent_id)
        sys.exit(6)

    if issues:
        for issue in issues:
            logger.error(issue)
        sys.exit(7)

    sys.exit(0)
