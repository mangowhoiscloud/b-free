"""Gate rule table (04 2) and determinism (NFR-5)."""

from __future__ import annotations

import random

import pytest

from bravo_api.gate import GatePolicy, Verdict, evaluate, judge_control

POLICY = GatePolicy(required_controls=("DB-ANON-READ", "DB-AUTH"), max_lag_bytes=65536)
READY = {"lag_bytes": 0, "sub_state": "streaming", "schema_hash": {"src": "h1", "dst": "h1"}}


def controls(anon: tuple[str, str], auth: tuple[str, str] = ("PASS", "PASS")) -> list[dict[str, str]]:
    return [
        {"control_id": "DB-ANON-READ", "src": anon[0], "dst": anon[1]},
        {"control_id": "DB-AUTH", "src": auth[0], "dst": auth[1]},
    ]


@pytest.mark.parametrize(
    ("src", "dst", "verdict", "reason"),
    [
        ("PASS", "PASS", "PASS", None),
        ("PASS", "FAIL", "BLOCK", "regression"),
        ("FAIL", "FAIL", "BLOCK", "fail_both"),
        ("UNKNOWN", "PASS", "BLOCK", "unknown"),
        ("PASS", "UNKNOWN", "BLOCK", "unknown"),
        ("UNKNOWN", "UNKNOWN", "BLOCK", "unknown"),
        ("FAIL", "PASS", "BLOCK", "not_equivalent"),
    ],
)
def test_rule_table(src: str, dst: str, verdict: str, reason: str | None) -> None:
    assert judge_control(src, dst) == (verdict, reason)


def test_all_pass_and_ready_passes() -> None:
    r = evaluate(controls(("PASS", "PASS")), READY, POLICY)
    assert r.verdict is Verdict.PASS and r.reasons == ()


def test_planted_regression_blocks() -> None:
    r = evaluate(controls(("PASS", "FAIL")), READY, POLICY)
    assert r.verdict is Verdict.BLOCK and r.reasons == ("DB-ANON-READ:regression",)


def test_same_input_same_verdict_and_digest() -> None:
    inputs = controls(("PASS", "FAIL"), ("FAIL", "FAIL"))
    a = evaluate(inputs, READY, POLICY)
    shuffled = list(reversed(inputs))
    b = evaluate(shuffled, dict(reversed(list(READY.items()))), POLICY)
    assert (a.verdict, a.reasons, a.input_digest) == (b.verdict, b.reasons, b.input_digest)
    for _ in range(20):
        random.shuffle(shuffled)
        assert evaluate(shuffled, READY, POLICY) == a


def test_executor_unreachable_is_unknown_block() -> None:
    r = evaluate(None, None, POLICY, ("executor_unreachable",))
    assert r.verdict is Verdict.BLOCK
    assert "executor_unreachable" in r.reasons
    assert all(c.src == "UNKNOWN" and c.dst == "UNKNOWN" and c.reason == "unknown" for c in r.controls)
    assert r == evaluate(None, None, POLICY, ("executor_unreachable",))


def test_missing_required_control_blocks() -> None:
    r = evaluate([{"control_id": "DB-AUTH", "src": "PASS", "dst": "PASS"}], READY, POLICY)
    assert r.verdict is Verdict.BLOCK and "DB-ANON-READ:control_missing" in r.reasons


def test_garbage_values_are_unknown() -> None:
    r = evaluate(
        [{"control_id": "DB-ANON-READ", "src": "pass", "dst": 1}, {"control_id": "DB-AUTH", "src": "PASS", "dst": "PASS"}],
        READY,
        POLICY,
    )
    assert "DB-ANON-READ:unknown" in r.reasons


def test_duplicate_control_id_fails_closed() -> None:
    dup = controls(("PASS", "PASS")) + [{"control_id": "DB-AUTH", "src": "PASS", "dst": "PASS"}]
    assert "DB-AUTH:unknown" in evaluate(dup, READY, POLICY).reasons


@pytest.mark.parametrize(
    ("readiness", "reason"),
    [
        ({**READY, "lag_bytes": 65537}, "readiness:replication_lag"),
        ({**READY, "lag_bytes": None}, "readiness:lag_unknown"),
        ({**READY, "lag_bytes": True}, "readiness:lag_unknown"),
        ({**READY, "schema_hash": {"src": "h1", "dst": "h2"}}, "readiness:schema_mismatch"),
        ({**READY, "schema_hash": {"src": None, "dst": None}}, "readiness:schema_hash_unknown"),
    ],
)
def test_readiness_thresholds(readiness: dict, reason: str) -> None:
    r = evaluate(controls(("PASS", "PASS")), readiness, POLICY)
    assert r.verdict is Verdict.BLOCK and r.reasons == (reason,)


def test_lag_at_threshold_passes() -> None:
    assert evaluate(controls(("PASS", "PASS")), {**READY, "lag_bytes": 65536}, POLICY).verdict is Verdict.PASS


# Control exception (spec S1 5b, contract 15 §9.4) ------------------------------------------------------------

D1 = "sha256:" + "d1" * 32
D2 = "sha256:" + "d2" * 32


def obj06(src: str, dst: str, digest: str | None = D1) -> dict[str, str | None]:
    return {"control_id": "OBJ-06", "src": src, "dst": dst, "evidence_digest": digest}


def ex(src: str = "PASS", dst: str = "FAIL", digest: str = D1):
    from bravo_api.gate import ControlException

    return ControlException("e-1", "OBJ-06", src, dst, digest, "2026-11-09T00:00:00+00:00", "approver@bravo.test")


def test_matching_exception_is_shown_apart_and_left_out_of_parity() -> None:
    blocked = evaluate([*controls(("PASS", "PASS")), obj06("PASS", "FAIL")], READY, POLICY)
    assert blocked.verdict is Verdict.BLOCK and blocked.reasons == ("OBJ-06:regression",)
    r = evaluate([*controls(("PASS", "PASS")), obj06("PASS", "FAIL")], READY, POLICY, exceptions=(ex(),))
    row = next(c for c in r.controls if c.control_id == "OBJ-06")
    assert (row.verdict, row.reason, row.exception_id, row.evidence_digest) == ("EXCEPTION", "exception_approved", "e-1", D1)
    assert r.verdict is Verdict.PASS and r.reasons == ()
    assert [e["exception_id"] for e in r.exceptions] == ["e-1"]
    assert r.input_digest != blocked.input_digest  # the applied exception is a gate input
    assert r == evaluate([obj06("PASS", "FAIL"), *controls(("PASS", "PASS"))], READY, POLICY, exceptions=(ex(),))


@pytest.mark.parametrize(
    ("row", "exc"),
    [
        (obj06("PASS", "UNKNOWN"), ex(dst="FAIL")),  # 5a: UNKNOWN on the destination never becomes an exception
        (obj06("UNKNOWN", "FAIL"), ex()),  # nor UNKNOWN on the source
        (obj06("PASS", "FAIL", D2), ex()),  # other evidence
        (obj06("PASS", "FAIL", None), ex()),  # no evidence
        (obj06("FAIL", "FAIL"), ex()),  # other pair (the exception was for regression)
        (obj06("PASS", "PASS"), ex()),  # fixed: passes on its own
    ],
)
def test_exception_applies_only_to_the_same_measured_fail(row: dict, exc) -> None:
    r = evaluate([*controls(("PASS", "PASS")), row], READY, POLICY, exceptions=(exc,))
    got = next(c for c in r.controls if c.control_id == "OBJ-06")
    assert got.verdict != "EXCEPTION" and r.exceptions == ()
    if row["dst"] != "PASS" or row["src"] != "PASS":
        assert r.verdict is Verdict.BLOCK


def test_unknown_elsewhere_still_blocks_with_an_exception() -> None:
    r = evaluate([*controls(("PASS", "UNKNOWN")), obj06("PASS", "FAIL")], READY, POLICY, exceptions=(ex(),))
    assert r.verdict is Verdict.BLOCK and r.reasons == ("DB-ANON-READ:unknown",)


def test_drift_marks_only_measured_changes() -> None:
    from bravo_api.gate import exception_drift

    open_ex = [{"exception_id": "e-1", "control_id": "OBJ-06", "src": "PASS", "dst": "FAIL", "evidence_digest": D1}]
    same = evaluate([obj06("PASS", "FAIL")], READY, POLICY)
    assert exception_drift(same, open_ex) == []
    assert exception_drift(evaluate([obj06("PASS", "FAIL", D2)], READY, POLICY), open_ex)[0][1] == "evidence_changed"
    assert exception_drift(evaluate([obj06("PASS", "PASS")], READY, POLICY), open_ex)[0][1] == "verdict_changed"
    assert exception_drift(evaluate([obj06("PASS", "UNKNOWN")], READY, POLICY), open_ex) == []  # not measured
    assert exception_drift(evaluate([], READY, POLICY), open_ex) == []  # gate unreachable: row missing
