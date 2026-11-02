"""실패 코드. 상태 머신은 code만 보고 전이를 정합니다(재시도, INFRA_ERROR, NEEDS_HUMAN)."""


class AgentRunError(Exception):
    """에이전트 실행이 제안서를 내지 못했습니다. 가짜 결과를 만들지 않고 이 예외로 끝냅니다."""

    # 인프라 쪽 실패(INFRA_ERROR 상한 2회로 셈)와 에이전트 쪽 실패(검증 불합격 3회로 셈)를 나눕니다.
    INFRA = frozenset({
        "llm_unconfigured", "evidence_unconfigured", "sdk_unavailable", "unexpected_tools",
        "timeout", "sdk_error", "allowlist_invalid", "envelope_invalid", "unexpected_mcp_servers", "mcp_unavailable",
        "unexpected_plugins", "unexpected_credentials", "llm_auth_failed",
    })

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    @property
    def infra(self) -> bool:
        return self.code in self.INFRA
