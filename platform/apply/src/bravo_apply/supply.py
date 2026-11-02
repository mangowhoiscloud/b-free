"""Staging registry, cosign signature with the platform OpenBao transit key, and signature verification.

The staging registry is a localhost-only CNCF distribution container that holds the candidate image and its
cosign signature (cosign signs registry objects). It is not a destination. Signing runs as
``keys.sh with-signer -- cosign sign --key hashivault://<key>``: the 5 min signer token exists only in cosign's
environment. Verification uses the exported public key only (no OpenBao token).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .runtime import CommandError, Result, Stop, clean_env, run

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json", "application/vnd.docker.distribution.manifest.list.v2+json"])


def _get(url: str, method: str = "GET", timeout: float = 5) -> tuple[int, dict[str, str]]:
    req = urllib.request.Request(url, method=method, headers={"Accept": MANIFEST_ACCEPT})  # noqa: S310 - fixed http://127.0.0.1
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {})


def ensure_registry(cfg: dict[str, Any]) -> dict[str, Any]:
    addr, name = cfg["address"], cfg["container"]
    try:
        code, _ = _get(f"http://{addr}/v2/")
    except OSError:
        code = 0
    if code != 200:
        inspect = run(["docker", "inspect", "-f", "{{.State.Running}}", name], env=clean_env(), check=False)
        if inspect.returncode == 0 and inspect.stdout.strip() == b"true":
            raise Stop("failed", "staging_registry_unhealthy", Result.FAIL, {"address": addr})
        if inspect.returncode == 0:
            run(["docker", "start", name], env=clean_env())
        else:
            host, port = addr.rsplit(":", 1)
            run(["docker", "run", "-d", "--name", name, "--restart", "unless-stopped", "-p", f"{host}:{port}:5000",
                 "-v", f"{cfg['volume']}:/var/lib/registry", "--label", "bravo.dev/component=t31-staging-registry",
                 cfg["image"]], env=clean_env(), timeout=300)
        for _ in range(40):
            try:
                if _get(f"http://{addr}/v2/")[0] == 200:
                    break
            except OSError:
                pass
            run(["sleep", "0.25"], env=clean_env())
        else:
            raise Stop("failed", "staging_registry_unreachable", Result.FAIL, {"address": addr})
    return {"address": addr, "image": cfg["image"], "label": cfg["label"]}


def registry_digest(addr: str, repo: str, digest: str) -> str | None:
    code, headers = _get(f"http://{addr}/v2/{repo}/manifests/{digest}", method="HEAD")
    if code != 200:
        return None
    return {k.lower(): v for k, v in headers.items()}.get("docker-content-digest")


def push(skopeo: str, tar: Path, addr: str, repo: str, tag: str, digest: str) -> dict[str, Any]:
    dest = f"docker://{addr}/{repo}:{tag}"
    run([skopeo, "copy", "--preserve-digests", "--dest-tls-verify=false", f"oci-archive:{tar}", dest],
        env=clean_env(), timeout=900)
    got = registry_digest(addr, repo, digest)
    if got != digest:
        raise Stop("failed", "staging_digest_mismatch", Result.REFUSED, {"expected": digest, "registry": got})
    return {"ref": f"{addr}/{repo}@{digest}", "tag": tag, "registry_digest": got}


def sign(keys_sh: Path, cosign: str, key_ref: str, ref: str, annotations: dict[str, str], signing_config: Path) -> dict[str, Any]:
    # signing_config lists no Fulcio, Rekor or TSA (cosign signing-config create with no services): the key is the
    # platform transit key and nothing is sent to a public transparency log (private repo, D-26).
    argv = ["bash", str(keys_sh), "with-signer", "--", cosign, "sign", "--key", key_ref, "--signing-config", str(signing_config),
            "--allow-http-registry", "--allow-insecure-registry", "-y"]
    for k, v in sorted(annotations.items()):
        argv += ["-a", f"{k}={v}"]
    argv.append(ref)
    # keys.sh reads the custody file under HOME and reaches OpenBao through kubectl port-forward.
    try:
        run(argv, env=clean_env(keep=("KUBECONFIG", "BRAVO_KEY_CUSTODY_DIR")), timeout=300)
    except CommandError as exc:
        reason = "signer_unreachable" if "OpenBao" in exc.stderr or "port-forward" in exc.stderr else "cosign_sign_failed"
        raise Stop("failed", reason, Result.NOT_CONNECTED if reason == "signer_unreachable" else Result.FAIL,
                   {"stderr": exc.stderr}) from exc
    return {"key_ref": key_ref, "ref": ref, "signing_config": signing_config.name, "tlog": "none (no Rekor/TSA in signing config)",
            "annotations": annotations}


def verify(cosign: str, public_key: Path, ref: str, digest: str, annotations: dict[str, str]) -> dict[str, Any]:
    argv = [cosign, "verify", "--key", str(public_key), "--insecure-ignore-tlog=true", "--allow-http-registry",
            "--allow-insecure-registry", "-o", "json"]
    for k, v in sorted(annotations.items()):
        argv += ["-a", f"{k}={v}"]
    argv.append(ref)
    proc = run(argv, env=clean_env(), timeout=120, check=False)
    if proc.returncode != 0:
        raise Stop("refused", "signature_invalid", Result.REFUSED, {"stderr": proc.stderr.decode(errors="replace")[-400:]})
    sigs = json.loads(proc.stdout)
    digests = sorted({(s.get("critical") or {}).get("image", {}).get("docker-manifest-digest") for s in sigs})
    if digests != [digest]:
        raise Stop("refused", "signature_digest_mismatch", Result.REFUSED, {"signed": digests, "expected": digest})
    return {"verified": True, "signatures": len(sigs), "signed_digest": digest, "public_key": public_key.name,
            "checks": "cosign claims + key (tlog ignored: signatures are not uploaded to a public log)",
            "stderr": proc.stderr.decode(errors="replace").strip()[-400:]}
