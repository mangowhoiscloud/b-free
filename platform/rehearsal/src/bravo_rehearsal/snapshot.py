"""Customer repository snapshots for ``repo_read`` and the sandbox ``/src`` (13 4, 13 6 "비밀").

``repo_read`` reads only a snapshot of the customer's repository that was uploaded for the plan;
without one it is refused (``repo_unconfigured``). The snapshot is bound to the collector's
dependency inventory (``platform/collector``, ``GET /v1/inventory``): it must contain exactly the
files the collector scanned, and its ``input_digest`` (sha256 over ``"<relative path>\\0<file
sha256>\\n"`` in the collector's walk order) must equal the inventory's. Files that hold
environment secrets by convention (``.env``, ``.env.*``) are verified and then withheld; every
string ``repo_read`` returns is redacted as well.

Operator path (the collector's environment provides ``bravo_collector``):

    PYTHONPATH=platform/rehearsal/src uv run --project platform/collector \\
        python -m bravo_rehearsal.snapshot pack --repo apps/demo --out /tmp/demo.tar.gz
    python -m bravo_rehearsal.snapshot upload --file /tmp/demo.tar.gz --url http://rehearsal:8090 \\
        --tenant t1 --plan <plan uuid> --digest <input_digest printed by pack>

The upload is ``PUT /v1/snapshots/{tenant}/{plan}`` with the service API token and the
``X-Bravo-Inventory-Digest`` header.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILES = 10_000
MAX_FILE_BYTES = 1024 * 1024  # same cap as the collector's scan
WITHHELD = re.compile(r"(^|/)\.env(\..*)?$")
TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PLAN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")


class SnapshotRejected(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def walk(root: Path) -> list[tuple[Path, PurePosixPath]]:
    """The collector's walk order: top-down, sorted directories and files, no symlinks."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not (Path(dirpath) / d).is_symlink())
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            out.append((path, PurePosixPath(path.relative_to(root).as_posix())))
    return out


def tree_digest(files: list[tuple[Path, PurePosixPath]]) -> str:
    digest = hashlib.sha256()
    for path, rel in files:
        digest.update(f"{rel}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class Snapshot:
    root: Path  # the repository files (withheld files removed)
    manifest: dict[str, Any]


class SnapshotStore:
    def __init__(self, base: Path) -> None:
        self.base = base

    def _dir(self, tenant: str, plan: str) -> Path:
        if not TENANT.match(tenant) or not PLAN.match(plan):
            raise SnapshotRejected("bad_snapshot_key")
        return self.base / tenant / plan

    def get(self, tenant: str, plan: str, input_digest: str | None = None) -> Snapshot | None:
        try:
            folder = self._dir(tenant, plan.lower())
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        except (SnapshotRejected, OSError, ValueError):
            return None
        if input_digest is not None and manifest.get("input_digest") != input_digest:
            return None
        repo = folder / "repo"
        return Snapshot(repo, manifest) if repo.is_dir() else None

    def ingest(self, tenant: str, plan: str, archive: bytes, input_digest: str) -> dict[str, Any]:
        """Verify the archive against the inventory digest, withhold secret files, then store it atomically."""
        folder = self._dir(tenant, plan.lower())
        if not DIGEST.match(input_digest):
            raise SnapshotRejected("bad_inventory_digest")
        if len(archive) > MAX_ARCHIVE_BYTES:
            raise SnapshotRejected("snapshot_too_large")
        folder.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=folder.parent))
        try:
            repo = staging / "repo"
            repo.mkdir()
            count = 0
            try:
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
                    for member in tar:
                        count += 1
                        if count > MAX_FILES:
                            raise SnapshotRejected("snapshot_too_many_files")
                        if member.isfile() and member.size > MAX_FILE_BYTES:
                            raise SnapshotRejected("snapshot_file_too_large")
                        if not (member.isfile() or member.isdir()):
                            raise SnapshotRejected("snapshot_special_member")  # links, devices, fifos
                        tar.extract(member, repo, filter="data")
            except (tarfile.TarError, OSError) as exc:
                raise SnapshotRejected("snapshot_unreadable") from exc
            files = walk(repo)
            if tree_digest(files) != input_digest:
                raise SnapshotRejected("snapshot_digest_mismatch")  # not the files the collector scanned
            withheld = [str(rel) for _, rel in files if WITHHELD.search(str(rel))]
            for rel in withheld:
                (repo / rel).unlink()
            manifest = {"tenant_id": tenant, "plan_id": plan.lower(), "input_digest": input_digest,
                        "files": len(files) - len(withheld), "withheld": withheld}
            (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            old = folder.with_name(folder.name + ".old")
            if folder.exists():
                folder.rename(old)
            staging.rename(folder)
            shutil.rmtree(old, ignore_errors=True)
            return manifest
        finally:
            shutil.rmtree(staging, ignore_errors=True)


# Operator CLI ----------------------------------------------------------------------------------


def pack(repo: Path) -> tuple[bytes, str, int]:
    """Exactly the files the collector's inventory reads (``scanned_files``: secret-shaped names and files with
    secret-shaped content are left out there), checked against the collector's own ``input_digest``."""
    try:
        from bravo_collector.inventory.detector import scan, scanned_files
    except ImportError as exc:  # pragma: no cover - operator environment
        raise SystemExit("run with the collector's environment (uv run --project platform/collector)") from exc
    root = repo.resolve()
    files = scanned_files(root)
    digest = hashlib.sha256()
    for f in files:
        digest.update(f"{f.rel}\0{hashlib.sha256(f.raw).hexdigest()}\n".encode())
    inventory = scan(root)
    if inventory.input_digest != digest.hexdigest() or inventory.files_scanned != len(files):
        raise SystemExit("file selection differs from the collector's scan; update bravo_rehearsal.snapshot.pack")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in files:
            info = tarfile.TarInfo(str(f.rel))
            info.size, info.mode = len(f.raw), 0o644
            tar.addfile(info, io.BytesIO(f.raw))
    return buf.getvalue(), digest.hexdigest(), len(files)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m bravo_rehearsal.snapshot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack")
    p.add_argument("--repo", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    u = sub.add_parser("upload")
    u.add_argument("--file", required=True, type=Path)
    u.add_argument("--url", required=True)
    u.add_argument("--tenant", required=True)
    u.add_argument("--plan", required=True)
    u.add_argument("--digest", required=True)
    a = ap.parse_args()
    if a.cmd == "pack":
        data, digest, n = pack(a.repo)
        a.out.write_bytes(data)
        print(f"files={n}\ninput_digest={digest}\nout={a.out}")
        return
    import httpx

    token = os.environ.get("BRAVO_REHEARSAL_API_TOKEN", "")
    if not token:
        raise SystemExit("BRAVO_REHEARSAL_API_TOKEN is not set")
    r = httpx.put(f"{a.url.rstrip('/')}/v1/snapshots/{a.tenant}/{a.plan}", content=a.file.read_bytes(), timeout=60,
                  headers={"Authorization": f"Bearer {token}", "X-Bravo-Inventory-Digest": a.digest,
                           "Content-Type": "application/gzip"})
    print(r.status_code, r.text)
    sys.exit(0 if r.status_code == 200 else 1)


if __name__ == "__main__":
    main()
