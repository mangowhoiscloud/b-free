"""Trusted build: base source at a git commit + the approved diff -> buildx OCI archive -> syft SBOM -> trivy scan.

The OCI archive is the content-addressed artifact of the change: stored as ``<cache>/images/<hex>.oci.tar`` and
checked against the manifest digest before every later use (sign, import, deploy).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .runtime import Result, Step, Stop, clean_env, run

REL_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,511}$")


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return run(["git", "-C", str(repo), *args], env=clean_env(), input_bytes=input_bytes, timeout=120).stdout


def materialize(repo: Path, ref: str, subdir: str, dest: Path) -> dict[str, Any]:
    """Extract ``subdir`` at ``ref`` (a commit, never the working tree) into ``dest``. Returns provenance."""
    commit = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    tree = git(repo, "rev-parse", f"{commit}:{subdir}").decode().strip()
    epoch = int(git(repo, "log", "-1", "--format=%ct", commit).decode().strip())
    raw = git(repo, "archive", "--format=tar", commit, subdir)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        tf.extractall(dest, filter="data")
    app = dest / subdir
    return {"repo_commit": commit, "git_tree": tree, "subdir": subdir, "source_date_epoch": epoch,
            "source_digest": tree_digest(app), "files": sum(1 for p in app.rglob("*") if p.is_file())}


def tree_digest(root: Path) -> str:
    """sha256 over sorted (relative path, file sha256): same idea as the collector inventory input_digest."""
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return "sha256:" + h.hexdigest()


def diff_paths(app_dir: Path, diff: str) -> list[str]:
    out = run(["git", "apply", "--numstat", "-p1", "-"], env=clean_env(), input_bytes=diff.encode(), cwd=app_dir).stdout
    paths = []
    for line in out.decode().splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            paths.append(parts[2])
    return paths


def apply_diff(app_dir: Path, diff: str, files: list[str] | None) -> Step:
    paths = diff_paths(app_dir, diff)
    bad = [p for p in paths if not REL_PATH.match(p) or ".." in PurePosixPath(p).parts]
    if not paths or bad:
        raise Stop("prepare_failed", "diff_path_rejected", Result.REFUSED, {"paths": paths[:50], "rejected": bad[:20]})
    if files is not None and sorted(set(files)) != sorted(set(paths)):
        raise Stop("prepare_failed", "diff_files_mismatch", Result.REFUSED,
                   {"diff_paths": sorted(set(paths)), "artifact_files": sorted(set(files))})
    run(["git", "apply", "--check", "-p1", "-"], env=clean_env(), input_bytes=diff.encode(), cwd=app_dir)
    run(["git", "apply", "-p1", "-"], env=clean_env(), input_bytes=diff.encode(), cwd=app_dir)
    return Step(detail={"files": sorted(set(paths)), "source_digest_after": tree_digest(app_dir)})


def oci_manifest_digest(tar_path: Path) -> dict[str, Any]:
    """Read index.json of an OCI archive; check the single manifest blob hashes to its digest."""
    with tarfile.open(tar_path) as tf:
        index = json.load(_member(tf, "index.json"))
        manifests = index.get("manifests") or []
        if len(manifests) != 1:
            raise Stop("failed", "oci_archive_not_single_manifest", Result.FAIL, {"manifests": len(manifests)})
        desc = manifests[0]
        digest = desc["digest"]
        blob = _member(tf, f"blobs/sha256/{digest.split(':', 1)[1]}").read()
    if "sha256:" + hashlib.sha256(blob).hexdigest() != digest:
        raise Stop("refused", "oci_manifest_hash_mismatch", Result.REFUSED, {"digest": digest})
    manifest = json.loads(blob)
    return {"digest": digest, "media_type": desc.get("mediaType"), "platform": desc.get("platform"),
            "name": (desc.get("annotations") or {}).get("io.containerd.image.name"),
            "config_digest": manifest["config"]["digest"], "layers": len(manifest.get("layers") or []),
            "size_bytes": tar_path.stat().st_size}


def _member(tf: tarfile.TarFile, name: str) -> io.BufferedReader:
    f = tf.extractfile(name)
    if f is None:
        raise Stop("failed", "oci_archive_member_missing", Result.FAIL, {"member": name})
    return f  # type: ignore[return-value]


def image_path(cache_dir: Path, digest: str) -> Path:
    return cache_dir / "images" / f"{digest.split(':', 1)[1]}.oci.tar"


def buildx(app_dir: Path, dockerfile: str, platform: str, name: str, epoch: int, cache_dir: Path, work: Path,
           buildx_path: str) -> dict[str, Any]:
    out_tar, meta = work / "image.oci.tar", work / "buildx-metadata.json"
    argv = [buildx_path, "build", "--platform", platform, "--provenance=false", "--sbom=false",
            "--output", f"type=oci,dest={out_tar},name={name},rewrite-timestamp=true",
            "--metadata-file", str(meta), "-f", str(app_dir / dockerfile), str(app_dir)]
    run(argv, env=clean_env({"SOURCE_DATE_EPOCH": str(epoch)}, keep=("DOCKER_CONTEXT", "DOCKER_HOST")), timeout=1800)
    built = json.loads(meta.read_text())["containerimage.digest"]
    info = oci_manifest_digest(out_tar)
    if info["digest"] != built:
        raise Stop("prepare_failed", "buildx_digest_disagrees_with_archive", Result.FAIL,
                   {"buildx": built, "archive": info["digest"]})
    final = image_path(cache_dir, built)
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(out_tar), final)
    return {**info, "archive": str(final)}


def syft_sbom(syft: str, tar: Path, out: Path) -> dict[str, Any]:
    run([syft, "scan", f"oci-archive:{tar}", "-o", f"spdx-json={out}", "-q"],
        env=clean_env({"SYFT_CHECK_FOR_APP_UPDATE": "false"}), timeout=900)
    raw = out.read_bytes()
    doc = json.loads(raw)
    return {"format": "spdx-json", "spdx_version": doc.get("spdxVersion"), "packages": len(doc.get("packages") or []),
            "sbom_digest": "sha256:" + hashlib.sha256(raw).hexdigest(), "file": out.name}


def trivy_scan(trivy: str, tar: Path, out: Path, policy: dict[str, Any]) -> dict[str, Any]:
    # No secrets, tokens or OIDC in trivy's environment (CVE-2026-33634): clean_env drops them all.
    env = clean_env()
    # trivy --input reads a docker-archive tar or an OCI layout directory, not an OCI tar: unpack the layout.
    with tempfile.TemporaryDirectory(prefix="bravo-apply-oci-") as layout:
        with tarfile.open(tar) as tf:
            tf.extractall(layout, filter="data")
        run([trivy, "image", "--input", layout, "--scanners", "vuln", "--format", "json", "--output", str(out),
             "--quiet", "--skip-version-check"], env=env, timeout=1800)
    report = json.loads(out.read_text())
    meta = json.loads(run([trivy, "version", "--format", "json"], env=env, timeout=30).stdout or b"{}")
    counts: dict[str, int] = {}
    blocking = []
    block = set(policy.get("block_severities") or [])
    for res in report.get("Results") or []:
        for v in res.get("Vulnerabilities") or []:
            sev = v.get("Severity", "UNKNOWN")
            counts[sev] = counts.get(sev, 0) + 1
            if sev in block and (v.get("FixedVersion") or not policy.get("fixable_only", True)):
                blocking.append({"id": v.get("VulnerabilityID"), "pkg": v.get("PkgName"),
                                 "installed": v.get("InstalledVersion"), "fixed": v.get("FixedVersion")})
    return {"scanner": "trivy", "version": meta.get("Version"), "db": meta.get("VulnerabilityDB"),
            "counts": counts, "policy": {**policy, "result": "fail" if blocking else "pass", "blocking": blocking[:20],
                                         "blocking_total": len(blocking)},
            "artifact": report.get("ArtifactName"), "file": out.name}
