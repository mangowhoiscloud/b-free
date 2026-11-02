"""Attachment copy job: pure rules, and a run against real S3-compatible stores when configured.

The store test needs two real S3-compatible endpoints (for example AWS S3 and Ceph RGW):
BRAVO_TEST_ATT_SRC_S3_* and BRAVO_TEST_ATT_DST_S3_* (same suffixes as attachment_copy.py).
Without them it is skipped; there is no stand-in store.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from bravo_executor.attachment_copy import (
    AttachmentCopier,
    ObjectIntegrityError,
    ObjectStore,
    Referenced,
    StoreConfig,
    plan_copy,
    verify_object,
)

DATA = b"hello attachment"
SHA = hashlib.sha256(DATA).hexdigest()
KEY = f"att/{SHA}"


def test_verify_object_checks_key_row_and_content() -> None:
    assert verify_object(KEY, SHA, DATA) == SHA
    with pytest.raises(ObjectIntegrityError):
        verify_object(KEY, SHA, DATA + b"x")
    with pytest.raises(ObjectIntegrityError):
        verify_object("att/" + "0" * 64, SHA, DATA)
    with pytest.raises(ObjectIntegrityError):
        verify_object("other/" + SHA, SHA, DATA)


def test_plan_is_the_sorted_set_difference() -> None:
    a = Referenced("att/" + "a" * 64, "a" * 64, 1)
    b = Referenced("att/" + "b" * 64, "b" * 64, 1)
    c = Referenced("att/" + "c" * 64, "c" * 64, 1)
    assert plan_copy([c, a, b, a], [b.object_key]) == [a, c]
    assert plan_copy([a], [a.object_key]) == []


def test_rows_that_disagree_on_a_key_hash_are_rejected() -> None:
    with pytest.raises(ObjectIntegrityError):
        plan_copy([Referenced(KEY, SHA, 1), Referenced(KEY, "f" * 64, 1)], [])


def test_store_config_is_off_without_bucket_and_never_describes_secrets() -> None:
    assert StoreConfig.from_env("src", "src", {}) is None
    cfg = StoreConfig.from_env(
        "dst", "dst",
        {"BRAVO_ATT_DST_S3_BUCKET": "b", "BRAVO_ATT_DST_S3_ENDPOINT_URL": "https://rgw.example",
         "BRAVO_ATT_DST_S3_ACCESS_KEY_ID": "AK", "BRAVO_ATT_DST_S3_SECRET_ACCESS_KEY": "SECRET"},
    )
    assert cfg is not None and cfg.conditional_put is True
    assert "SECRET" not in str(cfg.describe()) and "AK" not in str(cfg.describe())


def _test_store(side: str, store_id: str) -> StoreConfig | None:
    prefix = f"BRAVO_TEST_ATT_{side.upper()}_S3_"
    env = {k.replace("BRAVO_TEST_ATT_", "BRAVO_ATT_"): v for k, v in os.environ.items() if k.startswith(prefix)}
    return StoreConfig.from_env(side, store_id, env)


async def test_copy_against_real_stores_is_restartable(
    pool: AsyncConnectionPool[AsyncConnection], chat_dsn: str
) -> None:
    src_cfg, dst_cfg = _test_store("src", "src"), _test_store("dst", "dst")
    if src_cfg is None or dst_cfg is None:
        pytest.skip("no real S3-compatible endpoints configured (BRAVO_TEST_ATT_SRC_S3_*, BRAVO_TEST_ATT_DST_S3_*)")
    source, target = ObjectStore(src_cfg), ObjectStore(dst_cfg)
    payloads = [uuid.uuid4().bytes * 8 for _ in range(3)]
    async with await AsyncConnection.connect(chat_dsn, autocommit=True) as chat:
        for data in payloads:
            sha = hashlib.sha256(data).hexdigest()
            await source.put(f"att/{sha}", data, sha)
            await chat.execute(
                """INSERT INTO attachments (logical_id, idem_key, room, author, object_key, sha256, size_bytes,
                   content_type, filename, store_id) VALUES (%s, %s, 'r', 'a', %s, %s, %s, 'x', 'f', 'src')""",
                (uuid.uuid4(), uuid.uuid4(), f"att/{sha}", sha, len(data)),
            )
        copier = AttachmentCopier(pool, source, target)
        first = await copier.run(chat)
        assert first.complete and first.referenced == 3 and first.copied + first.verified_in_place == 3
        again = await copier.run(chat)  # restart: nothing left to do
        assert again.complete and again.already_verified == 3 and again.copied == 0
    for data in payloads:
        sha = hashlib.sha256(data).hexdigest()
        got = await target.get(f"att/{sha}")
        assert got is not None and verify_object(f"att/{sha}", sha, got) == sha


def test_reference_set_hash_and_reconcile_rules() -> None:
    """Contract 15 §11.3: the final reference set hash is order- and duplicate-free; a target object must
    exist with the row's size and, when present, the row's sha256 metadata."""
    from bravo_executor.attachment_copy import Referenced, reconcile_one, reference_set_sha256

    a = Referenced("att/" + "a" * 64, "a" * 64, 3)
    b = Referenced("att/" + "b" * 64, "b" * 64, 5)
    assert reference_set_sha256([a, b]) == reference_set_sha256([b, a, a])
    assert reference_set_sha256([a]) != reference_set_sha256([a, b])
    assert reconcile_one(a, None) == "MISSING"
    assert reconcile_one(a, {"size": 4, "metadata": {}}) == "SIZE"
    assert reconcile_one(a, {"size": 3, "metadata": {"sha256": "b" * 64}}) == "SHA256"
    assert reconcile_one(a, {"size": 3, "metadata": {"sha256": "a" * 64}}) is None
    assert reconcile_one(a, {"size": 3, "metadata": {}}) is None
