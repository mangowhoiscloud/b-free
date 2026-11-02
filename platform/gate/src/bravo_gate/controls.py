"""Six controls (04 §1, spec §8.5) as questions asked identically of source and target (FR-6, FR-12).

Each question needs one environment section (``needs``). A question applies to a (source, target) pair when
either side has that section; the side without it answers UNKNOWN (``not_configured``), which blocks.
Every question is provider-neutral (2026-11-03.1): the ``platform`` questions ask the same thing of EKS, RKE2 and
GKE and only the probe behind them differs (probes/neutral.py). Renamed IDs: SEC-SECRET-READ-AWS ->
SEC-SECRET-STORE-READ, SEC-ENC-CMK -> SEC-SECRET-AT-REST, AUD-PLATFORM -> AUD-CONTROL-PLANE.
Judging is a pure function of the observations (no LLM, NFR-5): same observations -> same rows and digest.
2026-11-03.2: every row says how it was measured (``method``: behavioural | config | simulation, or a ``+`` pair;
``src_method`` / ``dst_method`` when the two sides differ, e.g. OBJ-04 on AWS is an IAM simulation) and with what
(``src_tool`` / ``dst_tool``), and which spec §8.5 test it answers (``spec_test``). New questions: SEC-GIT-PLAINTEXT
(gitleaks), OBJ-PLAIN-EGRESS (compensating control for OBJ-06), NET-EXTERNAL-SCAN (outside port scan).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .result import FAIL, PASS, UNKNOWN, Obs

RULE_VERSION = "gate-controls/2026-11-03.2"

# 4-state vocabulary for evidence text (review condition 3). "대체 불가" is an approved exception.
# The gate never emits EXCEPTION itself: the platform API turns a row into EXCEPTION ("예외 승인") only for an
# approved control exception bound to that row's destination evidence digest (spec S1 5b, contract 15 §9).
STATUS_KO = {PASS: "충족", FAIL: "보완 필요", "EXCEPTION": "대체 불가", UNKNOWN: "미검증"}


def evidence_digest(obs: Obs) -> str:
    """``sha256:`` + sha256 of one side's evidence object ``{verdict, reason, evidence}`` as canonical JSON
    (sorted keys, ``,``/``:`` separators, UTF-8). Evidence holds no timestamps, so the same observation gives
    the same digest. A control exception (contract 15 §9) is bound to the destination's digest."""
    body = json.dumps(obs.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Control:
    control_id: str
    aliases: tuple[str, ...]
    name_ko: str
    question: str


CONTROLS: tuple[Control, ...] = (
    Control("SEC-01", ("ENC-02",), "비밀 저장",
            "Are app secrets protected from other identities and encrypted with a customer key?"),
    Control("ENC-01", (), "전송 암호화", "Is every hop encrypted with TLS and plaintext refused?"),
    Control("AUD-01", (), "감사 로그", "Are audit events (login, DDL, privilege change, control plane) recorded?"),
    Control("NET-01", (), "네트워크 노출", "Are only allowed paths reachable, with default deny?"),
    Control("IAM-01", ("ISO-01",), "최소 권한", "Are anonymous and other identities denied access to app resources?"),
    Control("DR-01", (), "백업 · 복구", "Is backup running and has a restore into a new environment succeeded?"),
)
CONTROL_IDS = tuple(c.control_id for c in CONTROLS)


BEHAVIOURAL, CONFIG, SIMULATION = "behavioural", "config", "simulation"
"""How a side was measured. behavioural: a real request or connection and its answer. config: settings, state or
files read. simulation: a policy engine asked what it would decide (IAM simulator, SubjectAccessReview,
has_table_privilege); no real access is attempted."""


@dataclass(frozen=True, slots=True)
class Question:
    qid: str
    control_id: str
    question: str
    needs: str  # environment section: object_store | postgres | kubernetes | platform | network | repository
    probe: str  # method name on the probe for that section
    method: str = BEHAVIOURAL
    tool: str = ""
    spec_test: str = ""  # the spec §8.5 test (Korean, as written there) this question answers
    # platform.kind -> (method, tool) where the probe differs (e.g. eks: IAM simulation instead of a real request)
    by_kind: tuple[tuple[str, str, str], ...] = ()

    def method_for(self, kind: str | None) -> tuple[str, str]:
        for k, m, t in self.by_kind:
            if k == kind:
                return m, t
        return self.method, self.tool


_S3 = "S3 HTTP 요청(boto3)"
_SIM_S3 = ("eks", SIMULATION, "iam:SimulatePrincipalPolicy(버킷 정책 · KMS 키 정책 포함)")
_PG = "PostgreSQL 접속(psycopg) · 카탈로그"

QUESTIONS: tuple[Question, ...] = (
    # IAM-01 / ISO-01 least privilege (object store behaviour, D-22 §4.4)
    Question("OBJ-01", "IAM-01", "Can an anonymous user read the attachment canary object?", "object_store", "obj01",
             BEHAVIOURAL, _S3, "다른 앱 · 다른 역할의 자원 접근 거부(거부 시험)"),
    Question("OBJ-02", "IAM-01", "Can an anonymous user list the attachment bucket?", "object_store", "obj02",
             BEHAVIOURAL, _S3, "다른 앱 · 다른 역할의 자원 접근 거부(거부 시험)"),
    Question("OBJ-03", "IAM-01", "Can an anonymous user write to the attachment bucket?", "object_store", "obj03",
             BEHAVIOURAL, _S3, "다른 앱 · 다른 역할의 자원 접근 거부(거부 시험)"),
    Question("OBJ-04", "IAM-01", "Can the app credential read and write the attachment bucket (control path)?",
             "object_store", "obj04", BEHAVIOURAL, _S3, "거부 시험의 대조군(앱 자격은 읽고 쓴다)", (_SIM_S3,)),
    Question("OBJ-05", "IAM-01", "Is a different credential denied reading the attachment canary?", "object_store",
             "obj05", BEHAVIOURAL, _S3, "다른 앱 · 다른 역할의 자원 접근 거부(거부 시험)"),
    Question("DB-ANON-READ", "IAM-01", "Can PUBLIC read the app tables?", "postgres", "anon_read",
             SIMULATION, "has_table_privilege('public', …)", "다른 역할의 자원 접근 거부"),
    Question("DB-AUTH", "IAM-01", "Can the app role log in without a password?", "postgres", "auth",
             f"{BEHAVIOURAL}+{CONFIG}", "비밀번호 없는 실제 로그인 시도 · pg_hba 규칙 · password_encryption",
             "다른 역할의 자원 접근 거부"),
    # SEC-01 / ENC-02 secrets
    Question("SEC-SECRET-READ-K8S", "SEC-01", "Can another service account read the app secret?", "kubernetes",
             "secret_read", SIMULATION, "SubjectAccessReview(kubectl auth can-i와 같은 판단)",
             "다른 서비스 계정의 Secret 읽기 거부(kubectl 시험)"),
    Question("SEC-SECRET-STORE-READ", "SEC-01",
             "Can another identity read the app secrets in their store of record (Secrets Manager or OpenBao)?",
             "platform", "secret_store_read", BEHAVIOURAL, "OpenBao: 권한 없는 역할로 실제 값 읽기(403)",
             "다른 서비스 계정의 Secret 읽기 거부",
             (("eks", SIMULATION, "iam:SimulatePrincipalPolicy(secretsmanager:GetSecretValue, 자원 정책 포함)"),)),
    Question("OBJ-ENC", "SEC-01", "Is the attachment bucket encrypted at rest with a customer-managed key?",
             "object_store", "obj_enc", CONFIG, "GetBucketEncryption · 객체별 HEAD(SSE 헤더)", "저장소 암호화"),
    Question("SEC-SECRET-AT-REST", "SEC-01",
             "Are app secrets at rest under a customer-controlled key, in the store and in the cluster?",
             "platform", "secret_at_rest", CONFIG, "OpenBao 상태 · kube-apiserver Secret 저장 변환자", "저장소 암호화",
             (("eks", CONFIG, "Secrets Manager KmsKeyId · EKS encryptionConfig · KMS DescribeKey"),
              ("gke", CONFIG, "OpenBao 상태 · GKE databaseEncryption(Cloud KMS)"))),
    Question("SEC-GIT-PLAINTEXT", "SEC-01", "Does the customer repository hold plaintext secrets (gitleaks)?",
             "repository", "git_plaintext", CONFIG, "gitleaks dir(배포된 커밋의 고객 저장소)",
             "Git 평문 비밀 0건(gitleaks)"),
    # ENC-01 transport
    Question("OBJ-06", "ENC-01", "Is plain HTTP to the attachment store rejected?", "object_store", "obj06",
             BEHAVIOURAL, "S3 GET over http://", "구간마다 TLS, 평문 · 다운그레이드 거부"),
    Question("OBJ-PLAIN-EGRESS", "ENC-01",
             "From an app Pod, is plain HTTP to the attachment store blocked, and is the app's store endpoint https?",
             "network", "plain_egress", f"{BEHAVIOURAL}+{CONFIG}",
             "앱 라벨 시험 Pod의 TCP 연결(TLS 포트 · 평문 포트) · 앱 Deployment의 엔드포인트 설정",
             "평문 거부(앱 쪽 경로). OBJ-06 예외의 대체 통제"),
    Question("DB-TLS", "ENC-01", "Does the DB refuse plaintext and verify CA and hostname?", "postgres", "tls",
             f"{BEHAVIOURAL}+{CONFIG}", "sslmode=disable 접속 시도 · verify-full 접속 · ssl 설정",
             "앱–pooler–DB 구간 CA · hostname 검증, TLS 다운그레이드 거부(psql verify-full)"),
    # AUD-01 audit
    Question("DB-AUDIT", "AUD-01", "Does the DB record logins, DDL and role changes?", "postgres", "audit",
             CONFIG, "pgaudit 설정 · log_connections", "정해 둔 이벤트(로그인, DDL, 권한 변경) 기록(pgaudit)"),
    Question("AUD-CONTROL-PLANE", "AUD-01", "Is control-plane audit logging on and recording now?", "platform",
             "control_plane_audit", CONFIG, "", "RKE2 · CloudTrail 감사 기록",
             (("eks", CONFIG, "CloudTrail 상태 · 이벤트 선택자 · EKS audit 로그 설정"),
              ("rke2", f"{CONFIG}+{BEHAVIOURAL}",
               "kube-apiserver 감사 플래그 · 시험 중 apiserver_audit_event_total 증가"),
              ("gke", BEHAVIOURAL, "Cloud Audit Logs 관리 활동 최근 1시간 항목 검색"))),
    # NET-01 network exposure
    Question("NET-DEFAULT-DENY", "NET-01", "Does the app namespace default-deny ingress and egress?", "kubernetes",
             "default_deny", CONFIG, "NetworkPolicy 목록", "허용하지 않은 경로 거부(NetworkPolicy)"),
    Question("NET-PATHS", "NET-01",
             "From the app namespace, does the control path connect and a disallowed path fail?", "network", "paths",
             BEHAVIOURAL, "앱 라벨 시험 Pod의 TCP 연결", "허용하지 않은 경로 거부(NetworkPolicy 시험)"),
    Question("NET-EXTERNAL-SCAN", "NET-01",
             "Does an outside port scan of the environment's public addresses show only the published entry points?",
             "network", "external_scan", BEHAVIOURAL, "플랫폼에서 TCP connect 스캔(nmap -sT -Pn과 같은 방식)",
             "외부 포트 스캔에 공개 진입점만 보임(nmap)"),
    # DR-01 backup and restore
    Question("DR-ARCHIVE", "DR-01", "Is WAL archiving running without failures?", "postgres", "archive",
             CONFIG, "pg_stat_archiver", "CNPG 백업"),
    Question("DR-RESTORE", "DR-01", "Did a restore into a new cluster come up healthy?", "kubernetes", "restore",
             BEHAVIOURAL, "CNPG bootstrap.recovery로 만든 클러스터의 상태", "새 환경으로 실제 복원 성공(CNPG 복원)"),
)
QUESTION_BY_ID = {q.qid: q for q in QUESTIONS}


def judge_pair(src: str, dst: str) -> tuple[str, str | None]:
    """04 §2 table. Same rule as the platform API gate."""
    if src == UNKNOWN or dst == UNKNOWN:
        return "BLOCK", "unknown"
    if src == PASS and dst == PASS:
        return "PASS", None
    if src == PASS and dst == FAIL:
        return "BLOCK", "regression"
    if src == FAIL and dst == FAIL:
        return "BLOCK", "fail_both"
    return "BLOCK", "not_equivalent"


def aggregate(verdicts: list[str]) -> str:
    if not verdicts:
        return UNKNOWN
    if FAIL in verdicts:
        return FAIL
    if UNKNOWN in verdicts:
        return UNKNOWN
    return PASS


@dataclass(frozen=True, slots=True)
class SideResults:
    environment_id: str
    observations: dict[str, Obs]  # qid -> observation, only applicable questions
    kind: str | None = None  # platform.kind (eks | rke2 | gke): picks the per-provider method label


def pair_method(src: str, dst: str) -> str:
    return src if src == dst else "mixed"


def build_rows(src: SideResults, dst: SideResults) -> dict[str, Any]:
    """Pure: observations -> question rows, control rows, gate verdict and input digest."""
    qids = [q.qid for q in QUESTIONS if q.qid in src.observations or q.qid in dst.observations]
    questions = []
    for qid in qids:
        q = QUESTION_BY_ID[qid]
        so, do = src.observations[qid], dst.observations[qid]
        verdict, reason = judge_pair(so.verdict, do.verdict)
        (sm, st), (dm, dt) = q.method_for(src.kind), q.method_for(dst.kind)
        questions.append({
            "control_id": qid, "parent_control": q.control_id, "question": q.question,
            "method": pair_method(sm, dm), "src_method": sm, "dst_method": dm, "src_tool": st, "dst_tool": dt,
            "spec_test": q.spec_test,
            "src": so.verdict, "dst": do.verdict,
            "src_status": STATUS_KO[so.verdict], "dst_status": STATUS_KO[do.verdict],
            "verdict": verdict, "reason": reason,
            "src_evidence": so.as_dict(), "dst_evidence": do.as_dict(),
            "src_evidence_digest": evidence_digest(so), "dst_evidence_digest": evidence_digest(do),
        })
    controls = []
    for c in CONTROLS:
        members = [r for r in questions if r["parent_control"] == c.control_id]
        s = aggregate([r["src"] for r in members])
        d = aggregate([r["dst"] for r in members])
        verdict, reason = judge_pair(s, d)
        controls.append({
            "control_id": c.control_id, "aliases": list(c.aliases), "name_ko": c.name_ko, "question": c.question,
            "src": s, "dst": d, "src_status": STATUS_KO[s], "dst_status": STATUS_KO[d],
            "verdict": verdict, "reason": reason if members else "no_applicable_question",
            "questions": [r["control_id"] for r in members],
        })
    blocked = [r["control_id"] for r in controls if r["verdict"] != "PASS"]
    digest_input = {"rule_version": RULE_VERSION, "source": src.environment_id, "target": dst.environment_id,
                    "src": {k: v.as_dict() for k, v in sorted(src.observations.items())},
                    "dst": {k: v.as_dict() for k, v in sorted(dst.observations.items())}}
    digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "rule_version": RULE_VERSION,
        "source": src.environment_id,
        "target": dst.environment_id,
        "gate_verdict": "BLOCK" if blocked else "PASS",
        "blocked_controls": blocked,
        "controls": controls,
        "questions": questions,
        "input_digest": digest,
    }


def contract_rows(report: dict[str, Any], level: str) -> list[dict[str, str]]:
    """CONTRACT.md §6 schema, exactly ``[{control_id, question, src, dst}]``."""
    rows = report["questions"] if level == "question" else report["controls"]
    return [{"control_id": r["control_id"], "question": r["question"], "src": r["src"], "dst": r["dst"]}
            for r in rows]


ProbeFn = Callable[[], Obs]
