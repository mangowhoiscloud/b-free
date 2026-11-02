"""GET /v1/inventory/snapshot: exactly the scanned files, digest equal to the inventory, no secrets."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient

from bravo_collector.app import create_app
from bravo_collector.environments import parse_registry
from bravo_collector.inventory import scan
from bravo_collector.inventory.snapshot import build_snapshot

AKIA = "AKIA" + "QWERTYUIOPASDFGH"  # credential-shaped test string, not a real key


def _rehearsal_tree_digest(root: Path) -> str:
    """The rehearsal service's check (15-r2-contracts): walk sorted, sha256 of "<rel>\\0<sha256>\\n"."""
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = PurePosixPath(path.relative_to(root).as_posix())
            digest.update(f"{rel}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    return digest.hexdigest()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text('import boto3\ns3 = boto3.client("s3")\n')
    (root / "src" / "keys.py").write_text(f'KEY = "{AKIA}"\n')
    (root / "chart").mkdir()
    (root / "chart" / "values.yaml").write_text("db:\n  host: pgbouncer\n")
    (root / ".env").write_text("DB_PASSWORD=real-secret-value\n")
    (root / ".env.example").write_text("DB_PASSWORD=\nS3_ENDPOINT_URL=\n")
    (root / "deploy.pem").write_text("-----BEGIN PRIVATE KEY-----\nabc\n")
    (root / "README.md").write_text("# not scanned (docs)\n")
    return root


def test_archive_is_exactly_the_scanned_set(repo: Path, tmp_path: Path) -> None:
    inv = scan(repo)
    snap = build_snapshot(repo)
    assert snap.input_digest == inv.input_digest
    assert snap.files == inv.files_scanned == 3
    with tarfile.open(fileobj=io.BytesIO(snap.archive), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers())
        out = tmp_path / "x"
        tar.extractall(out, filter="data")
    assert names == [".env.example", "chart/values.yaml", "src/app.py"]
    assert _rehearsal_tree_digest(out) == inv.input_digest
    reasons = {s["file"]: s["reason"] for s in snap.skipped}
    assert reasons[".env"].startswith("secret-shaped") and reasons["deploy.pem"].startswith("secret-shaped")
    assert reasons["src/keys.py"].startswith("credential material (aws_access_key_id)")
    assert AKIA.encode() not in gzip_plain(snap.archive) and b"real-secret-value" not in gzip_plain(snap.archive)


def gzip_plain(data: bytes) -> bytes:
    import gzip

    return gzip.decompress(data)


def test_same_tree_same_bytes(repo: Path) -> None:
    assert build_snapshot(repo).archive == build_snapshot(repo).archive


def test_endpoint(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(parse_registry({"environments": {}}))
    monkeypatch.delenv("BRAVO_INVENTORY_ROOT", raising=False)
    with TestClient(app) as c:
        assert c.get("/v1/inventory/snapshot").status_code == 503
        monkeypatch.setenv("BRAVO_INVENTORY_ROOT", str(repo))
        r = c.get("/v1/inventory/snapshot")
        assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
        assert r.headers["x-bravo-inventory-digest"] == c.get("/v1/inventory").json()["input_digest"]
        assert r.headers["x-bravo-snapshot-files"] == "3"
        empty = repo.parent / "empty"
        empty.mkdir()
        monkeypatch.setenv("BRAVO_INVENTORY_ROOT", str(empty))
        assert c.get("/v1/inventory/snapshot").json()["error"] == "inventory_root_empty"
        assert c.get("/v1/inventory").status_code == 503


def test_real_demo_app_snapshot_matches_inventory(tmp_path: Path) -> None:
    demo = Path(__file__).resolve().parents[3] / "apps" / "demo"
    if not demo.is_dir():
        pytest.skip("apps/demo not present")
    snap = build_snapshot(demo)
    assert snap.input_digest == scan(demo).input_digest
    with tarfile.open(fileobj=io.BytesIO(snap.archive), mode="r:gz") as tar:
        tar.extractall(tmp_path / "d", filter="data")
        names = tar.getnames()
    assert _rehearsal_tree_digest(tmp_path / "d") == snap.input_digest
    assert not any(n == ".env" or n.endswith("/.env") for n in names)
