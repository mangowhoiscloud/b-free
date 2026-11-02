"""판정 분류: PASS, FAIL, INFRA_ERROR. 인프라 실패를 FAIL과 섞지 않습니다(docs/architecture/03-evidence.md)."""
from enum import Enum


class Verdict(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INFRA_ERROR = "INFRA_ERROR"


def classify(reward: float | None, infra_failed: bool) -> Verdict:
    """검증기가 reward를 남기지 못했거나 환경이 실패하면 INFRA_ERROR입니다."""
    if infra_failed or reward is None:
        return Verdict.INFRA_ERROR
    return Verdict.PASS if reward >= 1 else Verdict.FAIL


def next_action(verdict: Verdict, attempts_counted: int, max_attempts: int = 3) -> str:
    """attempts_counted는 FAIL로 끝난 시도 수입니다. INFRA_ERROR는 세지 않습니다."""
    if verdict is Verdict.PASS:
        return "REQUEST_APPROVAL"
    if verdict is Verdict.INFRA_ERROR:
        return "RETRY_SAME_INPUT"
    return "RETRY" if attempts_counted < max_attempts else "NEEDS_HUMAN"
