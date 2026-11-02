"""에이전트 개입 기준: [입력 결정론 | 비결정론] x [출력 결정론 | 비결정론] (ADR 0002)."""
from dataclasses import dataclass
from enum import Enum


class Quadrant(Enum):
    CODE_ONLY = "결정론 입력 x 결정론 출력"
    SUMMARIZE = "결정론 입력 x 비결정론 출력"
    FIX = "비결정론 입력 x 결정론 출력"
    ANALYZE = "비결정론 입력 x 비결정론 출력"


@dataclass(frozen=True)
class Policy:
    trial_kind: str | None  # None이면 Trial 없이 코드만 실행
    judge: str
    max_attempts: int


POLICY = {
    Quadrant.CODE_ONLY: Policy(None, "code", 1),
    Quadrant.SUMMARIZE: Policy("summarize", "code_check+human", 1),
    Quadrant.FIX: Policy("fix", "separate_verifier", 3),
    Quadrant.ANALYZE: Policy("analyze", "schema_check+human", 1),
}


def classify(input_deterministic: bool, output_code_judgeable: bool) -> Quadrant:
    """출력 축은 '합격을 코드가 같은 기준으로 판정할 수 있나'로 읽습니다(제안)."""
    if input_deterministic:
        return Quadrant.CODE_ONLY if output_code_judgeable else Quadrant.SUMMARIZE
    return Quadrant.FIX if output_code_judgeable else Quadrant.ANALYZE
