from typing import Literal

TIMEOUT_EXIT_CODE = 300

Verdict = Literal[
    "no_vul_crash",
    "verified_success",
    "non_differential",
    "verification_pending",
    "submission_error",
]


def did_poc_crash(exit_code: int | None) -> bool:
    return exit_code not in (None, 0, TIMEOUT_EXIT_CODE)


def classify_poc_verdict(vul_exit_code: int | None, fix_exit_code: int | None, task_id: str) -> Verdict:
    if vul_exit_code is None:
        return "submission_error"
    if not did_poc_crash(vul_exit_code):
        return "no_vul_crash"
    if task_id.startswith("oss-fuzz-latest:"):
        return "verification_pending"
    if fix_exit_code is None:
        return "verification_pending"
    if did_poc_crash(fix_exit_code):
        return "non_differential"
    return "verified_success"
