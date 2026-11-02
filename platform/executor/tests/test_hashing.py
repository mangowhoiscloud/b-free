"""Hash rule v1 (pure functions)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from bravo_executor.hashing import (
    HashOrderError,
    TableDigest,
    sequence_last_issued,
    serialize_row,
    serialize_value,
)

KEY = UUID("6f1c2b8e-1d2a-4c55-9b0e-3f6a7d8e9c10")
T0 = datetime(2026, 11, 2, 9, 41, 20, 859747, tzinfo=UTC)


def _row(i: int, body: str = "hello", ts: datetime = T0) -> tuple[object, ...]:
    return (i, "room-1", "alice", body, KEY, ts)


def test_row_serialisation_is_fixed() -> None:
    assert serialize_row(_row(7)) == (
        b"7|room-1|alice|hello|6f1c2b8e-1d2a-4c55-9b0e-3f6a7d8e9c10|2026-11-02T09:41:20.859747Z\n"
    )


def test_timestamps_are_normalised_to_utc_with_six_fraction_digits() -> None:
    kst = timezone(timedelta(hours=9))
    same_instant = T0.astimezone(kst)
    assert serialize_value(same_instant) == serialize_value(T0)
    assert serialize_value(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00.000000Z"


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError):
        serialize_value(datetime(2026, 1, 1))  # noqa: DTZ001


def test_separators_in_text_cannot_forge_a_row_boundary() -> None:
    # Without escaping, these two rows would serialise to the same bytes.
    a = serialize_row((1, "a|b", "c"))
    b = serialize_row((1, "a", "b|c"))
    assert a != b
    assert serialize_value("x\\|y\nz\r") == "x\\\\\\|y\\nz\\r"
    assert serialize_value(None) == "\\N"
    assert serialize_value(True) == "t"


def test_digest_counts_and_hashes_match_the_documented_rule() -> None:
    rows = [_row(1), _row(2, "bye")]
    d = TableDigest("messages").add_all(rows)
    assert d.rows == 2
    assert d.pk_hash == hashlib.sha256(b"1\n2\n").hexdigest()
    assert d.content_hash == hashlib.sha256(b"".join(serialize_row(r) for r in rows)).hexdigest()


def test_digest_detects_content_change_with_same_keys() -> None:
    a = TableDigest("messages").add_all([_row(1), _row(2)])
    b = TableDigest("messages").add_all([_row(1), _row(2, "tampered")])
    assert a.pk_hash == b.pk_hash
    assert a.content_hash != b.content_hash


def test_digest_detects_missing_row() -> None:
    a = TableDigest("messages").add_all([_row(1), _row(2), _row(3)])
    b = TableDigest("messages").add_all([_row(1), _row(3)])
    assert (a.rows, a.pk_hash) != (b.rows, b.pk_hash)


@pytest.mark.parametrize("ids", [[2, 1], [1, 1]])
def test_digest_rejects_rows_out_of_pk_order(ids: list[int]) -> None:
    d = TableDigest("messages")
    d.add(_row(ids[0]))
    with pytest.raises(HashOrderError):
        d.add(_row(ids[1]))


def test_empty_table_digest_is_stable() -> None:
    d = TableDigest("messages")
    assert d.as_dict() == {
        "rows": 0,
        "pk_hash": hashlib.sha256(b"").hexdigest(),
        "content_hash": hashlib.sha256(b"").hexdigest(),
    }


def test_sequence_last_issued() -> None:
    assert sequence_last_issued(1, False) == 0  # fresh sequence: nothing issued
    assert sequence_last_issued(203, True) == 203


def test_uuid_pk_order_is_canonical_text_and_pk_line_is_the_uuid() -> None:
    a, b = UUID("0000000a-0000-4000-8000-000000000000"), UUID("f0000000-0000-4000-8000-000000000000")
    d = TableDigest("attachments", pk_kind="text").add_all([(a, "x"), (b, "y")])
    assert d.pk_hash == hashlib.sha256(f"{a}\n{b}\n".encode()).hexdigest()
    with pytest.raises(HashOrderError):
        TableDigest("attachments", pk_kind="text").add_all([(b, "y"), (a, "x")])
    with pytest.raises(TypeError):
        TableDigest("attachments", pk_kind="text").add((1, "x"))


def test_integer_pk_hashes_are_unchanged_from_v1() -> None:
    # v1.1 only adds uuid PKs: an integer-PK digest is byte-identical to the v1 rule.
    rows = [_row(1), _row(2)]
    assert TableDigest("messages").add_all(rows).content_hash == hashlib.sha256(
        b"".join(serialize_row(r) for r in rows)
    ).hexdigest()
