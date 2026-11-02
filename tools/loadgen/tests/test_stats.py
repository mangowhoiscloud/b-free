"""Run statistics and the NFR-2 verdict (pure functions)."""

from __future__ import annotations

import uuid

import pytest

from bravo_loadgen.stats import RunStats, evaluate_nfr2, percentile


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 99) == 99.0
    assert percentile(values, 100) == 100.0
    assert percentile([], 50) is None
    with pytest.raises(ValueError):
        percentile(values, 101)


def test_ack_gap_and_counts() -> None:
    s = RunStats(sent=3)
    s.record_ack(10.0, 5.0, on_conflict=False)
    s.record_ack(10.2, 150.0, on_conflict=True)
    s.record_ack(10.25, 2.0, on_conflict=False)
    out = s.summary()
    assert out["acked"] == 3 and out["acked_on_conflict"] == 1
    assert out["max_latency_ms"] == 150.0
    assert out["longest_ack_gap_ms"] == pytest.approx(200.0)


def test_nfr2_ok_only_when_every_ack_is_present_exactly_once() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ok = evaluate_nfr2([a, b], {a: 1, b: 1}, failed=0, sent=2)
    assert ok.ok and ok.found_once == 2 and ok.accounted

    lost = evaluate_nfr2([a, b], {a: 1}, failed=0, sent=2)
    assert not lost.ok and lost.lost == [str(b)]

    dup = evaluate_nfr2([a, c], {a: 1, c: 2}, failed=0, sent=2)
    assert not dup.ok and dup.duplicated == [str(c)]

    failed = evaluate_nfr2([a], {a: 1}, failed=1, sent=2)
    assert not failed.ok and failed.as_dict()["failed_requests"] == 1


def test_nfr2_is_not_ok_without_traffic_or_with_unaccounted_requests() -> None:
    assert not evaluate_nfr2([], {}, failed=0, sent=0).ok  # nothing acked proves nothing
    a = uuid.uuid4()
    unaccounted = evaluate_nfr2([a], {a: 1}, failed=0, sent=3)  # two requests vanished
    assert not unaccounted.ok and not unaccounted.accounted
