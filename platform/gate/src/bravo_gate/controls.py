"""Six controls (04 §1, spec §8.5) as questions asked identically of source and target (FR-6, FR-12).

Each question needs one environment section (``needs``). A question applies to a (source, target) pair when
either side has that section; the side without it answers UNKNOWN (``not_configured``), which blocks.
Judging is a pure function of the observations (no LLM, NFR-5): same observations -> same rows and digest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .result import FAIL, PASS, UNKNOWN, Obs

RULE_VERSION = "gate-controls/2026-11-02.1"

# 4-state vocabulary for evidence text (review condition 3). "대체 불가" is an approved exception.
STATUS_KO = {PASS: "충족", FAIL: "보완 필요", "EXCEPTION": "대체 불가", UNKNOWN: "미검증"}


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


@dataclass(frozen=True, slots=True)
class Question:
    qid: str
    control_id: str
    question: str
    needs: str  # environment section: object_store | postgres | kubernetes | aws | network
    probe: str  # method name on the probe for that section


QUESTIONS: tuple[Question, ...] = (
    # IAM-01 / ISO-01 least privilege (object store behaviour, D-22 §4.4)
    Question("OBJ-01", "IAM-01", "Can an anonymous user read the attachment canary object?", "object_store", "obj01"),
    Question("OBJ-02", "IAM-01", "Can an anonymous user list the attachment bucket?", "object_store", "obj02"),
    Question("OBJ-03", "IAM-01", "Can an anonymous user write to the attachment bucket?", "object_store", "obj03"),
    Question("OBJ-04", "IAM-01", "Can the app credential read and write the attachment bucket (control path)?",
             "object_store", "obj04"),
    Question("OBJ-05", "IAM-01", "Is a different credential denied reading the attachment canary?", "object_store",
             "obj05"),
    Question("DB-ANON-READ", "IAM-01", "Can PUBLIC read the app tables?", "postgres", "anon_read"),
    Question("DB-AUTH", "IAM-01", "Can the app role log in without a password?", "postgres", "auth"),
    # SEC-01 / ENC-02 secrets
    Question("SEC-SECRET-READ-K8S", "SEC-01", "Can another service account read the app secret?", "kubernetes",
             "secret_read"),
    Question("SEC-SECRET-READ-AWS", "SEC-01", "Can another IAM principal read the app secret?", "aws", "secret_read"),
    Question("OBJ-ENC", "SEC-01", "Is the attachment bucket encrypted at rest with a customer-managed key?",
             "object_store", "obj_enc"),
    Question("SEC-ENC-CMK", "SEC-01", "Are app secrets encrypted with a customer-managed key?", "aws", "secret_cmk"),
    # ENC-01 transport
    Question("OBJ-06", "ENC-01", "Is plain HTTP to the attachment store rejected?", "object_store", "obj06"),
    Question("DB-TLS", "ENC-01", "Does the DB refuse plaintext and verify CA and hostname?", "postgres", "tls"),
    # AUD-01 audit
    Question("DB-AUDIT", "AUD-01", "Does the DB record logins, DDL and role changes?", "postgres", "audit"),
    Question("AUD-PLATFORM", "AUD-01", "Is control-plane audit logging on?", "aws", "audit"),
    # NET-01 network exposure
    Question("NET-DEFAULT-DENY", "NET-01", "Does the app namespace default-deny ingress and egress?", "kubernetes",
             "default_deny"),
    Question("NET-PATHS", "NET-01", "Are only allowed paths reachable (control path succeeds)?", "network", "paths"),
    # DR-01 backup and restore
    Question("DR-ARCHIVE", "DR-01", "Is WAL archiving running without failures?", "postgres", "archive"),
    Question("DR-RESTORE", "DR-01", "Did a restore into a new cluster come up healthy?", "kubernetes", "restore"),
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


def build_rows(src: SideResults, dst: SideResults) -> dict[str, Any]:
    """Pure: observations -> question rows, control rows, gate verdict and input digest."""
    qids = [q.qid for q in QUESTIONS if q.qid in src.observations or q.qid in dst.observations]
    questions = []
    for qid in qids:
        q = QUESTION_BY_ID[qid]
        so, do = src.observations[qid], dst.observations[qid]
        verdict, reason = judge_pair(so.verdict, do.verdict)
        questions.append({
            "control_id": qid, "parent_control": q.control_id, "question": q.question,
            "src": so.verdict, "dst": do.verdict,
            "src_status": STATUS_KO[so.verdict], "dst_status": STATUS_KO[do.verdict],
            "verdict": verdict, "reason": reason,
            "src_evidence": so.as_dict(), "dst_evidence": do.as_dict(),
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
