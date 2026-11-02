"""Tool pins, OCI archive checks and the apply-phase refusals that happen before anything leaves the platform.

Tool binaries here are small shell scripts written by the test (real executables, real sha256), so the pin
check runs exactly as on the platform runner. Nothing is signed, imported or deployed in these tests.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from bravo_apply import build, config, tools
from bravo_apply.pipeline import Change, apply
from bravo_apply.runtime import MemoryRecorder, Result, Stop

REAL_LOCK = config.PROJECT_DIR / "tools.lock.json"


def _script(path: Path, text: str) -> Path:
    path.write_text(f"#!/bin/sh\necho '{text}'\n")
    path.chmod(0o755)
    return path


def _lock(tmp: Path, specs: dict[str, tuple[str, str]]) -> Path:
    entries = {}
    for name, (printed, version) in specs.items():
        p = _script(tmp / name, printed)
        entries[name] = {"version": version, "path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                         "version_cmd": ["--version"], "version_regex": r"version (\S+)"}
    out = tmp / "tools.lock.json"
    out.write_text(json.dumps({"tools": entries}))
    return out


def test_pin_ok_then_sha256_and_version_mismatch_and_missing(tmp_path: Path) -> None:
    lock = tools.load_lock(_lock(tmp_path, {"syft": ("syft version 1.55.0", "1.55.0")}))
    assert tools.check(lock["syft"])["ok"] is True
    p = Path(lock["syft"].path)
    p.write_text(p.read_text() + "# changed\n")
    assert tools.check(lock["syft"])["reason"] == "sha256_mismatch"
    lock2 = tools.load_lock(_lock(tmp_path, {"syft": ("syft version 1.54.0", "1.55.0")}))
    assert tools.check(lock2["syft"])["reason"] == "version_mismatch"
    p.unlink()
    assert tools.check(lock["syft"])["reason"] == "binary_missing"


@pytest.mark.skipif(not all(Path(t["path"]).is_file() for t in json.loads(REAL_LOCK.read_text())["tools"].values()),
                    reason="pinned tool binaries are not on this host (platform runner only)")
def test_real_lock_matches_this_runner() -> None:
    checks = tools.verify(tools.load_lock(REAL_LOCK))
    assert [c["name"] for c in checks if not c["ok"]] == []


def _oci_tar(path: Path, tamper: bool = False) -> str:
    manifest = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                           "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                      "digest": "sha256:" + "0" * 64, "size": 2}, "layers": []}).encode()
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    index = json.dumps({"schemaVersion": 2, "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                                                           "digest": digest, "size": len(manifest)}]}).encode()
    with tarfile.open(path, "w") as tf:
        for name, data in (("index.json", index),
                           (f"blobs/sha256/{digest[7:]}", manifest + (b" " if tamper else b""))):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return digest


def test_oci_archive_digest_is_checked_against_the_manifest_blob(tmp_path: Path) -> None:
    d = _oci_tar(tmp_path / "ok.tar")
    assert build.oci_manifest_digest(tmp_path / "ok.tar")["digest"] == d
    _oci_tar(tmp_path / "bad.tar", tamper=True)
    with pytest.raises(Stop) as e:
        build.oci_manifest_digest(tmp_path / "bad.tar")
    assert e.value.reason == "oci_manifest_hash_mismatch"


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config.Config:
    lock = _lock(tmp_path, {"cosign": ("cosign version v3.1.4", "v3.1.4"), "skopeo": ("skopeo version 1.24.1", "1.24.1"),
                            "kubectl": ("kubectl version v1.34.1", "v1.34.1"), "limactl": ("limactl version 1.2.1", "1.2.1")})
    monkeypatch.setenv("BRAVO_APPLY_TOOLS_LOCK", str(lock))
    monkeypatch.setenv("BRAVO_APPLY_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("BRAVO_APPLY_GATE_URL", raising=False)
    return config.load()


def _change(prepared: str, approved: str) -> Change:
    return Change(change_id="3e24d0f4-0000-4000-8000-000000000000", role="app_migration", target_env="target-onprem",
                  artifact=None, bound={}, approval={"approval_id": "a1", "image_digest": approved, "diff_digest": None,
                                                     "policy_diff_digest": None},
                  prepared={"image_digest": prepared})


def test_apply_refuses_when_approved_digest_differs_from_prepared(cfg: config.Config, tmp_path: Path) -> None:
    rec = MemoryRecorder()
    with pytest.raises(Stop) as e:
        apply(cfg, _change("sha256:" + "a" * 64, "sha256:" + "b" * 64), rec, tmp_path)
    assert (e.value.status, e.value.reason) == ("refused", "approved_digest_mismatch")
    assert [(r["name"], r["result"]) for r in rec.rows] == [("tools.verify", "OK"), ("approval.verify", "REFUSED")]


def test_apply_refuses_an_approval_that_names_a_policy_diff_the_artifact_lacks(cfg: config.Config, tmp_path: Path) -> None:
    ch = _change("sha256:" + "a" * 64, "sha256:" + "a" * 64)
    ch.approval["policy_diff_digest"] = "sha256:" + "c" * 64  # type: ignore[index]
    with pytest.raises(Stop) as e:
        apply(cfg, ch, MemoryRecorder(), tmp_path)
    assert e.value.reason == "approved_policy_diff_mismatch"


def test_apply_fails_when_the_approved_image_archive_is_missing_or_tampered(cfg: config.Config, tmp_path: Path) -> None:
    good = _oci_tar(tmp_path / "x.tar")
    rec = MemoryRecorder()
    with pytest.raises(Stop) as e:
        apply(cfg, _change(good, good), rec, tmp_path)
    assert (e.value.status, e.value.reason) == ("failed", "image_missing")
    dest = build.image_path(cfg.cache_dir, good)
    dest.parent.mkdir(parents=True)
    _oci_tar(dest, tamper=True)
    rec = MemoryRecorder()
    with pytest.raises(Stop) as e:
        apply(cfg, _change(good, good), rec, tmp_path)
    assert e.value.reason == "oci_manifest_hash_mismatch" and rec.rows[-1]["name"] == "image.locate"
    assert rec.rows[-1]["result"] == Result.REFUSED.value


def test_trivy_environment_carries_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from bravo_apply.runtime import clean_env

    for k in ("GITHUB_TOKEN", "VAULT_TOKEN", "AWS_SECRET_ACCESS_KEY", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "TRIVY_PASSWORD"):
        monkeypatch.setenv(k, "x")
    env = clean_env(keep=("GITHUB_TOKEN", "VAULT_TOKEN"))
    assert not {"GITHUB_TOKEN", "VAULT_TOKEN", "AWS_SECRET_ACCESS_KEY", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "TRIVY_PASSWORD"} & set(env)
    assert shutil.which("sh", path=env["PATH"])
