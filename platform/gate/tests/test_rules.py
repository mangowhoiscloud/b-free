"""Pure rule tests: 04 §2 table, aggregation, determinism, contract schema, 4-state vocabulary."""

from __future__ import annotations

import json

import pytest

from bravo_gate.controls import CONTROL_IDS, STATUS_KO, SideResults, aggregate, build_rows, contract_rows, judge_pair
from bravo_gate.result import FAIL, PASS, UNKNOWN, Obs, worst


@pytest.mark.parametrize(
    ("src", "dst", "verdict", "reason"),
    [
        (PASS, PASS, "PASS", None),
        (PASS, FAIL, "BLOCK", "regression"),
        (FAIL, FAIL, "BLOCK", "fail_both"),
        (UNKNOWN, PASS, "BLOCK", "unknown"),
        (PASS, UNKNOWN, "BLOCK", "unknown"),
        (FAIL, PASS, "BLOCK", "not_equivalent"),
    ],
)
def test_judge_pair_table(src: str, dst: str, verdict: str, reason: str | None) -> None:
    assert judge_pair(src, dst) == (verdict, reason)


def test_aggregate_fail_beats_unknown_beats_pass() -> None:
    assert aggregate([PASS, UNKNOWN, FAIL]) == FAIL
    assert aggregate([PASS, UNKNOWN]) == UNKNOWN
    assert aggregate([PASS, PASS]) == PASS
    assert aggregate([]) == UNKNOWN


def test_worst_keeps_all_evidence() -> None:
    o = worst([Obs(PASS, "a"), Obs(UNKNOWN, "b"), Obs(FAIL, "c")])
    assert o.verdict == FAIL and o.reason == "c" and len(o.evidence["parts"]) == 3
    assert worst([]).verdict == UNKNOWN


def _sides() -> tuple[SideResults, SideResults]:
    src = SideResults("src", {"OBJ-01": Obs(PASS, "anonymous_read_denied", {"response": "403"}),
                              "DB-TLS": Obs(PASS, "ok")})
    dst = SideResults("dst", {"OBJ-01": Obs(FAIL, "anonymous_read_allowed", {"response": "200"}),
                              "DB-TLS": Obs(UNKNOWN, "ca_not_configured")})
    return src, dst


def test_same_input_same_output() -> None:
    a = build_rows(*_sides())
    b = build_rows(*_sides())
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["input_digest"] == b["input_digest"]


def test_digest_changes_with_input() -> None:
    src, dst = _sides()
    a = build_rows(src, dst)
    dst2 = SideResults("dst", {**dst.observations, "OBJ-01": Obs(PASS, "anonymous_read_denied", {"response": "403"})})
    assert build_rows(src, dst2)["input_digest"] != a["input_digest"]


def test_report_rows_and_contract_schema() -> None:
    r = build_rows(*_sides())
    assert r["gate_verdict"] == "BLOCK"
    q = {x["control_id"]: x for x in r["questions"]}
    assert q["OBJ-01"]["reason"] == "regression" and q["OBJ-01"]["dst_status"] == "보완 필요"
    assert q["DB-TLS"]["reason"] == "unknown" and q["DB-TLS"]["dst_status"] == "미검증"
    controls = {x["control_id"]: x for x in r["controls"]}
    assert tuple(controls) == CONTROL_IDS
    assert controls["IAM-01"]["src"] == PASS and controls["IAM-01"]["dst"] == FAIL
    assert controls["DR-01"]["reason"] == "no_applicable_question" and controls["DR-01"]["src"] == UNKNOWN
    for level in ("question", "control"):
        rows = contract_rows(r, level)
        assert rows and all(set(row) == {"control_id", "question", "src", "dst"} for row in rows)
        assert all(row["src"] in {PASS, FAIL, UNKNOWN} and row["dst"] in {PASS, FAIL, UNKNOWN} for row in rows)


def test_status_vocabulary() -> None:
    assert STATUS_KO == {PASS: "충족", FAIL: "보완 필요", "EXCEPTION": "대체 불가", UNKNOWN: "미검증"}


def test_gate_imports_no_llm_client() -> None:
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    banned = ("anthropic", "claude_agent_sdk", "openai", "langchain")
    for f in src.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for b in banned:
            assert f"import {b}" not in text and f"from {b}" not in text, f"{f}: {b}"
