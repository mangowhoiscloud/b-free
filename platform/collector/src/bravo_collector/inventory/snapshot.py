"""Repository snapshot for the rehearsal (S2, 15-r2-contracts: ``GET /v1/inventory/snapshot``).

The archive holds exactly the files the inventory scanned (``detector.scanned_files``), so its tree digest
equals the inventory ``input_digest`` and the rehearsal service accepts it (``PUT /v1/snapshots/{tenant}/{plan}``
with ``X-Bravo-Inventory-Digest``). Secret-shaped files and files with credential material are not in that set
(D-26); they are listed in ``skipped`` by name only. The archive is deterministic: walk order, mtime 0,
uid/gid 0, mode 0644, gzip without name or time, so the same tree gives the same bytes.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

from ..redact import find_content_credentials
from .detector import files_digest, scanned_files

# Same limits as the rehearsal service's ingest (platform/rehearsal snapshot.py).
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILES = 10_000


class SnapshotError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RepoSnapshot:
    archive: bytes  # tar.gz
    input_digest: str
    files: int
    skipped: list[dict[str, str]]


def build_snapshot(root: Path) -> RepoSnapshot:
    root = Path(root)
    if not root.is_dir():
        raise SnapshotError("inventory_root_missing")
    skipped: list[dict[str, str]] = []
    files = scanned_files(root.resolve(), skipped)
    if len(files) > MAX_FILES:
        raise SnapshotError("snapshot_too_many_files")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for f in files:
            # Credential guard on the exact bytes that leave (the scan already excluded such files).
            if find_content_credentials(f.raw.decode("utf-8")):
                raise SnapshotError("snapshot_credential_material")
            info = tarfile.TarInfo(str(f.rel))
            info.size, info.mtime, info.mode = len(f.raw), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(f.raw))
    data = buf.getvalue()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise SnapshotError("snapshot_too_large")
    return RepoSnapshot(data, files_digest(files), len(files), sorted(skipped, key=lambda s: s["file"]))
