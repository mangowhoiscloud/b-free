"""역할별 출력 스키마(정본). SDK output_format의 JSON Schema는 여기서 만듭니다.

에이전트 Pod 안의 검증은 빠른 반려용입니다. 플랫폼 도구 계층이 같은 모델로 다시 검증합니다(Pod는 신뢰하지 않음).
"""
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

Risk = Literal["low", "medium", "high"]
ControlId = Literal["SEC-01", "ENC-02", "ENC-01", "AUD-01", "NET-01", "IAM-01", "ISO-01", "DR-01"]
EvidenceRef = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")]


def _no_parent(v: str) -> str:
    if ".." in v.split("/"):
        raise ValueError("parent directory segment")
    return v


# 저장소 루트 기준 상대 경로. 절대 경로, 숨김 디렉터리로 시작, `..` 구간을 거부합니다.
RelPath = Annotated[str, Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,511}$"), AfterValidator(_no_parent)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProposalBase(_Strict):
    plan_id: str = Field(pattern=r"^p-[a-z0-9-]{1,64}$")
    trial_id: str = Field(pattern=r"^t-[a-z0-9-]{1,64}$")
    attempt: int = Field(ge=1, le=3)
    rationale: str = Field(min_length=1, max_length=4000)
    risk: Risk
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=64)


# ---- Discovery (FR-2 애매한 항목 분류) ----
class Classification(_Strict):
    dependency_id: str = Field(min_length=1, max_length=128)
    grade: Literal["config_swap", "adapter", "code_change", "no_change"]
    confidence: Literal["low", "medium", "high"]
    reason: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=16)


class DiscoveryProposal(ProposalBase):
    role: Literal["discovery"]
    step: Literal["classify"]
    items: list[Classification] = Field(min_length=1, max_length=200)


# ---- Governance (6개 통제 대응, 정책 번역) ----
class SourceRef(_Strict):
    type: Literal["aws_iam_policy", "aws_iam_role", "aws_s3_bucket_policy",
                  "aws_secretsmanager_secret", "aws_kms_key"]
    id: str = Field(min_length=1, max_length=256)


class TargetRef(_Strict):
    # 목적지 저장소 엔진은 D-22. 제품 이름 대신 정책 형식으로 적습니다.
    type: Literal["k8s_rbac", "rgw_bucket_policy", "gcs_iam_policy", "s3_bucket_policy",
                  "openbao_policy", "external_secret"]
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,62}$")


class Translation(_Strict):
    source: SourceRef
    target: TargetRef
    document: dict = Field(description="목적지 형식의 정책 또는 매니페스트(JSON)")
    control_ids: list[ControlId] = Field(min_length=1, max_length=8)


class ExceptionJustification(_Strict):
    control_id: ControlId
    justification: str = Field(min_length=1, max_length=2000)


class GovernanceProposal(ProposalBase):
    role: Literal["governance"]
    step: Literal["translate"]
    translations: list[Translation] = Field(min_length=1, max_length=50)
    exception_justifications: list[ExceptionJustification] = Field(default_factory=list, max_length=8)
    audit_summary: str = Field(min_length=1, max_length=4000)


# ---- App Migration (직접 SDK 호출 코드 수정, 설정 교체) ----
class SdkCallChange(_Strict):
    file: RelPath
    line: int = Field(ge=1)
    before_api: str = Field(max_length=200)
    after: str = Field(max_length=200)


class ConfigChange(_Strict):
    key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,62}$")
    before: str = Field(max_length=512)
    after: str = Field(max_length=512)


class AppMigrationProposal(ProposalBase):
    role: Literal["app_migration"]
    step: Literal["fix"]
    diff: str = Field(min_length=1, max_length=200_000)
    files: list[RelPath] = Field(min_length=1, max_length=50)
    sdk_calls_replaced: list[SdkCallChange] = Field(default_factory=list, max_length=100)
    config_changes: list[ConfigChange] = Field(default_factory=list, max_length=50)


# ---- CI (고정 템플릿 안의 컨테이너화 · 빌드 실패 수정) ----
class BuildParams(_Strict):
    template: Literal["python-asgi", "python-wsgi", "python-worker"]
    python_version: Literal["3.12"]
    entry: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*(:[A-Za-z_][A-Za-z0-9_]*)?$")
    port: int = Field(ge=1024, le=65535)
    requirements: RelPath


class CiProposal(ProposalBase):
    role: Literal["ci"]
    step: Literal["containerize", "fix_build"]
    build_params: BuildParams
    helm_values: dict = Field(description="templates/bravo-app values.schema.json을 통과해야 함. imageRef 금지")
    diff: str | None = Field(default=None, max_length=200_000)
    dependency_requests: list[str] = Field(default_factory=list, max_length=20,
                                           description="새 의존성은 사람 승인 뒤 신뢰된 파이프라인이 받음")


# ---- DB (준비 상태 점검, 계획만) ----
class DbFinding(_Strict):
    check: Literal["server_version", "extensions", "collation", "primary_keys", "sequences",
                   "logical_decoding", "replication_slots", "replication_lag", "pgbouncer"]
    status: Literal["ready", "action_needed", "unknown"]
    detail: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=16)


class DbPlan(_Strict):
    kind: Literal["enable_logical_decoding", "pgbouncer_insertion", "sequence_alignment",
                  "replica_identity", "extension_install", "collation_fix", "slot_cleanup_request"]
    target: Literal["source", "destination"]
    steps: list[str] = Field(min_length=1, max_length=20)
    sql: str | None = Field(default=None, max_length=20_000,
                            description="db_rehearse로 합성 DB에서만 리허설. 실제 DB에는 Executor가 승인 뒤 실행")
    requires_restart: bool


class DbProposal(ProposalBase):
    role: Literal["db"]
    step: Literal["db_readiness"]
    findings: list[DbFinding] = Field(min_length=1, max_length=50)
    plans: list[DbPlan] = Field(default_factory=list, max_length=20)


# ---- Report (승인 요약 P1, 사후 리포트) ----
class Claim(_Strict):
    text: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=16)


class ReportProposal(ProposalBase):
    role: Literal["report"]
    step: Literal["approval_summary", "post_migration_report"]
    summary: str = Field(min_length=1, max_length=8000)
    claims: list[Claim] = Field(min_length=1, max_length=50)


OUTPUT_MODELS: dict[str, type[ProposalBase]] = {
    "discovery": DiscoveryProposal,
    "governance": GovernanceProposal,
    "app_migration": AppMigrationProposal,
    "ci": CiProposal,
    "db": DbProposal,
    "report": ReportProposal,
}

AnyProposal = Union[DiscoveryProposal, GovernanceProposal, AppMigrationProposal, CiProposal,
                    DbProposal, ReportProposal]


def output_schema(role: str) -> dict:
    """SDK output_format에 넘길 JSON Schema."""
    return OUTPUT_MODELS[role].model_json_schema()


def referenced_evidence(p: ProposalBase) -> set[str]:
    """제안서가 인용한 모든 증거 ID. 봉투에 없는 ID를 인용하면 반려합니다."""
    refs = set(p.evidence_refs)
    for field in ("items", "findings", "claims"):
        for x in getattr(p, field, None) or []:
            refs.update(x.evidence_refs)
    return refs
