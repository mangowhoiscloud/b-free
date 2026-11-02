"""Receipt pass-through and the console summary (no combined or invented hashes)."""

from __future__ import annotations

from bravo_api.events import receipt_body


def table(name: str, rows: int, h: str) -> dict:
    return {
        "name": name,
        "rows": {"src": rows, "dst": rows},
        "pk_hash": {"src": h, "dst": h},
        "content_hash": {"src": h + "c", "dst": h + "c"},
        "match": True,
    }


def test_full_body_is_kept_and_summary_added() -> None:
    body = {
        "verdict": "MATCH",
        "hash_rule": "v1",
        "tables": [table("messages", 5, "m"), table("bravo_barrier", 1, "b")],
        "sequences": [{"name": "messages_id_seq", "src_last": 5, "dst_set": 5, "dst_before": 0, "match": True}],
        "watermark": {"source_lsn": "0/1"},
    }
    out = receipt_body(body)
    assert {k: out[k] for k in body} == body
    assert out["rows"] == {"src": 5, "dst": 5} and out["pk_hash"] == {"src": "m", "dst": "m"}
    assert out["seq"] == {"src": 5, "dst": 5}


def test_several_app_tables_sum_rows_and_leave_hashes_null() -> None:
    out = receipt_body({"verdict": "MATCH", "tables": [table("a", 2, "x"), table("b", 3, "y")]})
    assert out["rows"] == {"src": 5, "dst": 5}
    assert out["pk_hash"] is None and out["content_hash"] is None
    assert out["summary_of"] == {"tables": ["a", "b"]}


def test_executor_flat_fields_win() -> None:
    out = receipt_body({"verdict": "MATCH", "rows": {"src": 1, "dst": 1}, "tables": [table("a", 9, "x")]})
    assert out["rows"] == {"src": 1, "dst": 1}


def test_non_dict_body() -> None:
    assert receipt_body(None) == {} and receipt_body([1]) == {}
