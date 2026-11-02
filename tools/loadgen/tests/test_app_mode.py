"""App-mode pieces that decide verdicts: NFR-2 by room, merged worker stats, NFR-1 statistics."""

from __future__ import annotations

import uuid

from bravo_loadgen.app_load import OpStats
from bravo_loadgen.cutover_stats import summarize
from bravo_loadgen.generator import body_marker
from bravo_loadgen.nfr2 import check


def _req(rid: uuid.UUID, outcome: str) -> dict[str, object]:
    return {"request_id": str(rid), "outcome": outcome}


def test_nfr2_by_room_sees_loss_duplicates_and_foreign_rows() -> None:
    a, b, c, f = (uuid.uuid4() for _ in range(4))
    ok = check([_req(a, "acked"), _req(b, "acked")], {body_marker(a): 1, body_marker(b): 1})
    assert ok["ok"] and ok["found_exactly_once"] == 2 and ok["rows_in_room"] == 2

    lost = check([_req(a, "acked"), _req(b, "acked")], {body_marker(a): 1})
    assert not lost["ok"] and lost["lost"] == 1

    dup = check([_req(a, "acked")], {body_marker(a): 2})
    assert not dup["ok"] and dup["duplicated"] == 1

    # A failed request whose row did commit is counted, and the run is not OK (it failed).
    failed = check([_req(a, "acked"), _req(f, "failed")], {body_marker(a): 1, body_marker(f): 1})
    assert not failed["ok"] and failed["failed_but_committed"] == 1 and failed["rows_not_sent_by_run"] == 0

    foreign = check([_req(a, "acked")], {body_marker(a): 1, body_marker(c): 1})
    assert not foreign["ok"] and foreign["rows_not_sent_by_run"] == 1


def test_merged_workers_keep_counts_and_the_combined_gap() -> None:
    w1, w2 = OpStats(sent=2, ok=2, ok_at=[1.0, 3.0], latencies_ms=[10.0, 20.0]), OpStats(sent=1, ok=1, ok_at=[2.0],
                                                                                        latencies_ms=[30.0])
    w1.attempt_errors["503"] += 1
    w2.attempt_errors["503"] += 2
    w1.merge(w2)
    s = w1.summary(window_s=3.0)
    assert s["sent"] == 3 and s["ok"] == 3 and s["attempt_errors"] == {"503": 3}
    assert s["longest_ok_gap_ms"] == 1000.0  # 1.0 -> 2.0 -> 3.0 across workers, not 2000 within one
    assert s["latency_ms"]["max"] == 30.0 and s["achieved_ok_per_s"] == 1.0


def test_cutover_stats_only_count_done_runs() -> None:
    runs = [
        ("r1", {"cutover": {"status": "DONE"}, "write_pause_ms": 900, "nfr2": {"ok": True, "lost": 0}}),
        ("r2", {"cutover": {"status": "DONE"}, "write_pause_ms": 6100, "nfr2": {"ok": True, "lost": 0}}),
        ("r3", {"cutover": {"status": "ABORT_PRE_WRITE"}, "write_pause_ms": None, "nfr2": {"ok": True}}),
        ("r4", {"cutover": {"status": "DONE"}, "write_pause_ms": 1500, "nfr2": {"ok": False, "lost": 2}}),
    ]
    out = summarize(runs)
    assert out["done_runs"] == 3 and out["write_pause_ms"]["raw"] == [900, 6100, 1500]
    assert out["write_pause_ms"]["median"] == 1500 and out["write_pause_ms"]["max"] == 6100
    assert out["write_pause_ms"]["within_5000"] == 2
    assert out["nfr2"] == {"runs_ok": 3, "lost": 2, "duplicated": 0, "failed_requests": 0}
    assert out["runs"][2]["cutover_status"] == "ABORT_PRE_WRITE"
