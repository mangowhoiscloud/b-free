"""Attachment object copy between stores (D-04, 03-migration.md 4.1 step 5 and 4.2 step 5).

Copies the objects referenced by committed ``attachments`` rows whose ``store_id`` is the
source store into the target store, when they are missing there, and verifies the SHA-256
of every object on the target. Each object's outcome is recorded in the platform table
``attachment_copies`` (migrations/0002_attachment_copies.sql).

Restartable set-difference scan:
    todo = {object_key referenced by rows of the source store}
         - {object_key already VERIFIED for (source store, target store) in attachment_copies}
Every key in ``todo`` is checked on the target first; an object that is already there with the
right hash is recorded VERIFIED without copying. Objects are content-addressed
(``att/<sha256>``) and never overwritten: a target object with another hash is a FAILED
conflict, not something to replace. Puts use ``If-None-Match: *`` when the store supports it.

Stores are S3-compatible endpoints configured only from the environment:
    BRAVO_ATT_<SIDE>_S3_BUCKET            (required to enable copying; SIDE = SRC or DST)
    BRAVO_ATT_<SIDE>_S3_ENDPOINT_URL      (empty = AWS S3 default endpoint)
    BRAVO_ATT_<SIDE>_S3_REGION
    BRAVO_ATT_<SIDE>_S3_ADDRESSING_STYLE  (path | virtual | auto, default auto)
    BRAVO_ATT_<SIDE>_S3_ACCESS_KEY_ID / _S3_SECRET_ACCESS_KEY or _S3_SECRET_ACCESS_KEY_FILE
                                          (empty = SDK default credential chain)
    BRAVO_ATT_<SIDE>_S3_CONDITIONAL_PUT   (1 = send If-None-Match: *, default 1)

CLI (pre-copy before a cutover, or the final delta under freeze):
    python -m bravo_executor.attachment_copy --from src --to dst
In executor-0 (BRAVO_TOPOLOGY_FILE set) the stores and DSNs come from the topology and the delivered
secrets, the same ones the cutover uses at step 1b:
    kubectl exec executor-0 -c executor -- python -m bravo_executor.attachment_copy --from src --to dst
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

KEY_RE = re.compile(r"^att/([0-9a-f]{64})$")
VERIFIED = "VERIFIED"
FAILED = "FAILED"
COPYING = "COPYING"


class ObjectIntegrityError(ValueError):
    """An object's bytes do not hash to the SHA-256 its key and row promise."""


def verify_object(object_key: str, expected_sha256: str, data: bytes) -> str:
    """Return the SHA-256 of ``data`` if it equals the row's sha256 and the key's hash, else raise."""
    m = KEY_RE.match(object_key)
    if m is None:
        raise ObjectIntegrityError(f"{object_key}: key is not att/<sha256>")
    if m.group(1) != expected_sha256:
        raise ObjectIntegrityError(f"{object_key}: key does not match row sha256 {expected_sha256}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ObjectIntegrityError(f"{object_key}: content sha256 {actual} != {expected_sha256}")
    return actual


@dataclass(frozen=True, slots=True)
class Referenced:
    object_key: str
    sha256: str
    size_bytes: int


def plan_copy(referenced: Iterable[Referenced], verified_keys: Iterable[str]) -> list[Referenced]:
    """Set difference, deterministic order: referenced keys that are not yet VERIFIED."""
    done = set(verified_keys)
    unique: dict[str, Referenced] = {}
    for ref in referenced:
        prev = unique.get(ref.object_key)
        if prev is not None and prev.sha256 != ref.sha256:
            raise ObjectIntegrityError(f"{ref.object_key}: rows disagree on sha256")
        unique[ref.object_key] = ref
    return [unique[k] for k in sorted(unique) if k not in done]


@dataclass(frozen=True, slots=True)
class StoreConfig:
    store_id: str
    bucket: str
    endpoint_url: str | None
    region: str | None
    addressing_style: str
    access_key_id: str | None
    secret_access_key: str | None
    conditional_put: bool

    @classmethod
    def from_env(cls, side: str, store_id: str, env: Mapping[str, str] | None = None) -> StoreConfig | None:
        e = os.environ if env is None else env
        p = f"BRAVO_ATT_{side.upper()}_S3_"
        bucket = (e.get(p + "BUCKET") or "").strip()
        if not bucket:
            return None
        secret = (e.get(p + "SECRET_ACCESS_KEY") or "").strip() or None
        secret_file = (e.get(p + "SECRET_ACCESS_KEY_FILE") or "").strip()
        if secret is None and secret_file:
            secret = Path(secret_file).read_text(encoding="utf-8").strip()
        return cls(
            store_id=store_id,
            bucket=bucket,
            endpoint_url=(e.get(p + "ENDPOINT_URL") or "").strip() or None,
            region=(e.get(p + "REGION") or "").strip() or None,
            addressing_style=(e.get(p + "ADDRESSING_STYLE") or "auto").strip(),
            access_key_id=(e.get(p + "ACCESS_KEY_ID") or "").strip() or None,
            secret_access_key=secret,
            conditional_put=(e.get(p + "CONDITIONAL_PUT") or "1").strip() == "1",
        )

    def describe(self) -> dict[str, Any]:
        """For evidence and logs: no credentials."""
        return {"store_id": self.store_id, "bucket": self.bucket, "endpoint": self.endpoint_url or "aws-default"}


class ObjectStore:
    """Minimal S3 client (boto3, run in a worker thread)."""

    def __init__(self, cfg: StoreConfig) -> None:
        import boto3
        from botocore.config import Config

        self.cfg = cfg
        self._s3 = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint_url,
            region_name=cfg.region,
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            config=Config(
                s3={"addressing_style": cfg.addressing_style},
                retries={"max_attempts": 3, "mode": "standard"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    async def get(self, key: str) -> bytes | None:
        def _get() -> bytes | None:
            try:
                return self._s3.get_object(Bucket=self.cfg.bucket, Key=key)["Body"].read()
            except self._s3.exceptions.NoSuchKey:
                return None
            except Exception as exc:  # botocore ClientError 404 on some S3-compatible stores
                if getattr(exc, "response", {}).get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise

        return await asyncio.to_thread(_get)

    async def put(self, key: str, data: bytes, sha256: str) -> None:
        def _put() -> None:
            extra: dict[str, Any] = {}
            if self.cfg.conditional_put:
                extra["IfNoneMatch"] = "*"
            self._s3.put_object(
                Bucket=self.cfg.bucket, Key=key, Body=data, ContentType="application/octet-stream",
                Metadata={"sha256": sha256}, **extra,
            )

        await asyncio.to_thread(_put)


@dataclass(slots=True)
class CopyReport:
    source: dict[str, Any]
    target: dict[str, Any]
    referenced: int = 0
    already_verified: int = 0
    copied: int = 0
    verified_in_place: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failed and self.already_verified + self.copied + self.verified_in_place == self.referenced

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "referenced": self.referenced,
            "already_verified": self.already_verified,
            "copied": self.copied,
            "verified_in_place": self.verified_in_place,
            "failed": self.failed[:20],
            "failed_count": len(self.failed),
            "complete": self.complete,
        }


class AttachmentCopier:
    def __init__(self, pool: AsyncConnectionPool[AsyncConnection], source: ObjectStore, target: ObjectStore) -> None:
        self._pool = pool
        self.source = source
        self.target = target

    async def _referenced(self, chat: AsyncConnection) -> list[Referenced]:
        cur = await chat.execute(
            "SELECT object_key, sha256, size_bytes FROM attachments WHERE store_id = %s ORDER BY object_key",
            (self.source.cfg.store_id,),
        )
        return [Referenced(r[0], r[1], int(r[2])) for r in await cur.fetchall()]

    async def _verified_keys(self) -> list[str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT object_key FROM attachment_copies
                WHERE source_store = %s AND target_store = %s AND status = %s
                """,
                (self.source.cfg.store_id, self.target.cfg.store_id, VERIFIED),
            )
            return [r[0] for r in await cur.fetchall()]

    async def _record(self, ref: Referenced, status: str, copied: bool, error: str | None = None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO attachment_copies
                  (source_store, target_store, object_key, sha256, size_bytes, status, copied, error, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (source_store, target_store, object_key) DO UPDATE
                  SET status = EXCLUDED.status, copied = attachment_copies.copied OR EXCLUDED.copied,
                      error = EXCLUDED.error, attempts = attachment_copies.attempts + 1, updated_at = now()
                """,
                (self.source.cfg.store_id, self.target.cfg.store_id, ref.object_key, ref.sha256, ref.size_bytes,
                 status, copied, error),
            )

    async def run(self, chat: AsyncConnection) -> CopyReport:
        referenced = await self._referenced(chat)
        verified = await self._verified_keys()
        todo = plan_copy(referenced, verified)
        report = CopyReport(self.source.cfg.describe(), self.target.cfg.describe())
        report.referenced = len({r.object_key for r in referenced})
        report.already_verified = report.referenced - len(todo)
        for ref in todo:
            await self._record(ref, COPYING, False)
            try:
                existing = await self.target.get(ref.object_key)
                if existing is not None:
                    verify_object(ref.object_key, ref.sha256, existing)  # conflict raises, never overwritten
                    await self._record(ref, VERIFIED, False)
                    report.verified_in_place += 1
                    continue
                data = await self.source.get(ref.object_key)
                if data is None:
                    raise ObjectIntegrityError(f"{ref.object_key}: missing in source store")
                verify_object(ref.object_key, ref.sha256, data)
                await self.target.put(ref.object_key, data, ref.sha256)
                written = await self.target.get(ref.object_key)
                if written is None:
                    raise ObjectIntegrityError(f"{ref.object_key}: not readable on target after put")
                verify_object(ref.object_key, ref.sha256, written)
                await self._record(ref, VERIFIED, True)
                report.copied += 1
            except Exception as exc:  # noqa: BLE001 - each object fails alone; the run reports it
                msg = f"{type(exc).__name__}: {str(exc)[:200]}"
                await self._record(ref, FAILED, False, msg)
                report.failed.append({"object_key": ref.object_key, "error": msg})
        return report


def copier_from_env(
    pool: AsyncConnectionPool[AsyncConnection], from_side: str, to_side: str, store_ids: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> AttachmentCopier | None:
    src_cfg = StoreConfig.from_env(from_side, store_ids[from_side], env)
    dst_cfg = StoreConfig.from_env(to_side, store_ids[to_side], env)
    if src_cfg is None or dst_cfg is None:
        return None
    return AttachmentCopier(pool, ObjectStore(src_cfg), ObjectStore(dst_cfg))


async def _cli(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .config import Settings

    topology = os.environ.get("BRAVO_TOPOLOGY_FILE", "").strip()
    if topology:
        from .topology import FileSecrets, settings_from_topology

        secrets = FileSecrets(Path(os.environ["BRAVO_SECRETS_DIR"]))
        s = await settings_from_topology(Path(topology), secrets.field, os.environ["PLATFORM_DB_DSN"],
                                         require_entries=False)
    else:
        s = Settings.from_env()
    pool: AsyncConnectionPool[AsyncConnection] = AsyncConnectionPool(
        s.platform_dsn, connection_class=AsyncConnection, min_size=1, max_size=2, open=False
    )
    await pool.open(wait=True, timeout=30)
    try:
        stores = dict(s.object_stores)
        copier = (AttachmentCopier(pool, ObjectStore(stores[args.from_side]), ObjectStore(stores[args.to_side]))
                  if args.from_side in stores and args.to_side in stores
                  else copier_from_env(pool, args.from_side, args.to_side,
                                       {"src": s.src_store_id, "dst": s.dst_store_id}))
        if copier is None:
            hint = "set BRAVO_ATT_SRC_S3_BUCKET and BRAVO_ATT_DST_S3_BUCKET"
            print(json.dumps({"status": "NOT_CONFIGURED", "hint": hint}))
            return 2
        dsn = s.src_dsn if args.from_side == "src" else s.dst_dsn
        async with await AsyncConnection.connect(dsn, autocommit=True) as chat:
            report = await copier.run(chat)
        print(json.dumps(report.as_dict()))
        return 0 if report.complete else 1
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bravo-attachment-copy", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="from_side", choices=("src", "dst"), required=True)
    parser.add_argument("--to", dest="to_side", choices=("src", "dst"), required=True)
    args = parser.parse_args(argv)
    if args.from_side == args.to_side:
        parser.error("--from and --to must differ")
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    sys.exit(asyncio.run(_cli(args)))


if __name__ == "__main__":
    main()
