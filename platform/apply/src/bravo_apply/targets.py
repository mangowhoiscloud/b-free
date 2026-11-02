"""Policy targets outside Kubernetes (P3): ``rgw_bucket_policy`` on the destination RGW and ``openbao_policy`` in the
platform OpenBao. Scope rules are pure and run before anything is applied; appliers write, then read back.

rgw_bucket_policy (destination ``object_store``):
  - the target name is the destination's attachment bucket; every Resource is that bucket or a key under it;
  - no Allow to an anonymous principal (``*``), no Allow with a wildcard action, no ``NotPrincipal`` / ``NotAction``
    in an Allow;
  - applied with the bucket owner's key from the platform OpenBao (``object_store.secret``, D-26) over a private
    kubectl port-forward to the RGW Service, as PutBucketPolicy; then GetBucketPolicy must return the same statements.
    The previous policy's digest and Sids are recorded (what was replaced, never the key).

openbao_policy (destination ``secret_store``):
  - the policy name is ``<policy_prefix><target name>`` (e.g. ``app-target-onprem-chat-db-read``);
  - every path is under ``<kv_mount>/data/<path_prefix>`` or ``<kv_mount>/metadata/<path_prefix>`` (the destination
    profile, B11), no glob before the prefix ends, capabilities only ``read`` / ``list``;
  - applied as ``PUT sys/policies/acl/<name>`` with the operator's custody token through the same port-forward the
    signer uses (``keys.sh``), then read back.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import ssl
import stat
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .runtime import Result, Step, Stop

S3_ARN = "arn:aws:s3:::"
OPENBAO_CAPS = {"read", "list"}
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else [v]


def _canon(doc: Any) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def digest(doc: Any) -> str:
    return "sha256:" + hashlib.sha256(_canon(doc).encode()).hexdigest()


# --- scope rules (pure) -----------------------------------------------------------------------------------------

def rgw_scope(name: str, doc: Any, store: dict[str, Any]) -> list[str]:
    """The target name is a label; the bucket is the destination's (every Resource must be in it)."""
    bucket = store.get("bucket")
    problems: list[str] = []
    if not bucket:
        problems.append(f"{name}:object_store_unconfigured")
    if not isinstance(doc, dict) or not isinstance(doc.get("Statement"), list | dict):
        return [*problems, f"{name}:not_a_bucket_policy"]
    for st in _as_list(doc["Statement"]):
        if not isinstance(st, dict):
            problems.append(f"{name}:statement_not_object")
            continue
        for r in _as_list(st.get("Resource") or []):
            if not (isinstance(r, str) and (r == S3_ARN + str(bucket) or r.startswith(f"{S3_ARN}{bucket}/"))):
                problems.append(f"{name}:resource_out_of_scope:{r}")
        if st.get("Effect") == "Allow":
            principal = st.get("Principal")
            aws = principal.get("AWS") if isinstance(principal, dict) else principal
            if "*" in [str(x) for x in _as_list(aws)]:
                problems.append(f"{name}:anonymous_allow")
            if any(a in ("*", "s3:*") for a in _as_list(st.get("Action") or [])):
                problems.append(f"{name}:wildcard_action")
            if "NotPrincipal" in st or "NotAction" in st:
                problems.append(f"{name}:negated_allow")
    return problems


def openbao_policy_name(name: str, store: dict[str, Any]) -> str:
    prefix = str(store.get("policy_prefix") or "")
    return name if name.startswith(prefix) else prefix + name


_HCL_PATH = re.compile(r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]\s*\}', re.S)


def normalize_openbao(doc: Any) -> Any:
    """The JSON form ``{"path": {p: {"capabilities": [...]}}}`` from either that form or HCL text in ``policy``
    (both seen in proposals). HCL with any stanza this cannot read is returned unchanged and refused by the scope."""
    if not isinstance(doc, dict) or isinstance(doc.get("path"), dict):
        return doc
    text = doc.get("policy") if isinstance(doc.get("policy"), str) else doc.get("hcl")
    if not isinstance(text, str):
        return doc
    found = _HCL_PATH.findall(text)
    if not found or len(found) != len(re.findall(r'\bpath\s+"', text)):
        return doc
    return {"path": {p: {"capabilities": sorted({c.strip().strip('"') for c in caps.split(",") if c.strip()})}
                     for p, caps in found}}


def openbao_scope(name: str, doc: Any, store: dict[str, Any]) -> list[str]:
    kv, pfx = str(store.get("kv_mount") or "secret"), str(store.get("path_prefix") or "")
    problems: list[str] = []
    if not pfx or not store.get("policy_prefix"):
        return [f"{name}:secret_store_unconfigured"]
    if not _NAME.match(openbao_policy_name(name, store)):
        problems.append(f"{name}:policy_name_invalid")
    doc = normalize_openbao(doc)
    paths = (doc or {}).get("path") if isinstance(doc, dict) else None
    if not isinstance(paths, dict) or not paths:
        return [*problems, f"{name}:no_paths"]
    for path, body in paths.items():
        p = str(path).strip().lstrip("/")
        allowed = (f"{kv}/data/{pfx}", f"{kv}/metadata/{pfx}")
        if not p.startswith(allowed) or any(ch in p[: len(allowed[0])] for ch in "*+"):
            problems.append(f"{name}:path_out_of_scope:{p}")
        caps = set((body or {}).get("capabilities") or []) if isinstance(body, dict) else set()
        if not caps or caps - OPENBAO_CAPS:
            problems.append(f"{name}:capabilities_out_of_scope:{sorted(caps - OPENBAO_CAPS) or 'none'}")
    return problems


def openbao_hcl(doc: dict[str, Any]) -> str:
    lines = ["# Applied by bravo-apply (T31) from an approved change; read-only paths of one destination."]
    for path, body in sorted(doc["path"].items()):
        caps = ", ".join(json.dumps(c) for c in sorted(set(body["capabilities"])))
        lines.append(f'path {json.dumps(str(path).strip().lstrip("/"))} {{ capabilities = [{caps}] }}')
    return "\n".join(lines) + "\n"


# --- S3 SigV4 (stdlib) -----------------------------------------------------------------------------------------

def sign(method: str, host: str, path: str, query: str, payload_sha256: str, access_key: str, secret_key: str,
         amz_date: str, region: str = "us-east-1") -> dict[str, str]:
    """AWS Signature Version 4 for S3, signed headers host, x-amz-content-sha256, x-amz-date."""
    headers = {"host": host, "x-amz-content-sha256": payload_sha256, "x-amz-date": amz_date}
    names = ";".join(sorted(headers))
    canonical = "\n".join([method, path, query, "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), names,
                           payload_sha256])
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret_key).encode()
    for part in (amz_date[:8], region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"x-amz-content-sha256": payload_sha256, "x-amz-date": amz_date,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={names}, "
                             f"Signature={signature}"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class PortForward:
    """``kubectl port-forward`` for this process only; closed on exit."""

    def __init__(self, kubectl: str, context: str, namespace: str, target: str, remote: int) -> None:
        self.argv = [kubectl, "--context", context, "-n", namespace, "port-forward", target]
        self.remote = remote
        self.proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> int:
        self.port = _free_port()
        self.proc = subprocess.Popen([*self.argv, f"{self.port}:{self.remote}"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return self.port
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.25)
        self.__exit__()
        raise Stop("failed", "port_forward_failed", Result.NOT_CONNECTED, {})

    def __exit__(self, *_: Any) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Rgw:
    def __init__(self, port: int, bucket: str, ca_pem: str, access_key: str, secret_key: str) -> None:
        self.host, self.port, self.bucket = "localhost", port, bucket  # the RGW certificate names localhost
        self._ca = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)  # public CA certificate
        self._ca.write(ca_pem)
        self._ca.close()
        self.ctx = ssl.create_default_context(cafile=self._ca.name)
        self.ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)  # the install's CA has no AKI
        self._ak, self._sk = access_key, secret_key

    def close(self) -> None:
        Path(self._ca.name).unlink(missing_ok=True)

    def policy_call(self, method: str, body: bytes = b"") -> tuple[int, bytes]:
        path = "/" + urllib.parse.quote(self.bucket)
        amz = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        headers = sign(method, f"{self.host}:{self.port}", path, "policy=", hashlib.sha256(body).hexdigest(),
                       self._ak, self._sk, amz)
        conn = http.client.HTTPSConnection(self.host, self.port, context=self.ctx, timeout=15)
        try:
            conn.request(method, path + "?policy=", body=body or None, headers=headers)
            r = conn.getresponse()
            return r.status, r.read(262144)
        finally:
            conn.close()


class CustodyBao:
    """The operator's custody token (``~/.bravo/custody/openbao-init.json``, mode 600) through a private port-forward
    to the platform OpenBao, as ``keys.sh`` and ``deploy/cloud/db_roles.py`` do. Values never leave this process."""

    def __init__(self, kubectl: str) -> None:
        cdir = Path(os.environ.get("BRAVO_KEY_CUSTODY_DIR", str(Path.home() / ".bravo/custody")))
        custody = cdir / "openbao-init.json"
        if not custody.is_file() or stat.S_IMODE(custody.stat().st_mode) != 0o600:
            raise Stop("failed", "custody_unavailable", Result.NOT_CONNECTED, {"why": "custody file missing or not 600"})
        doc = json.loads(custody.read_text())
        target = doc.get("bao_target", "")
        if target not in ("platform-rke2", "platform-eks"):
            raise Stop("failed", "openbao_target_unsupported", Result.NOT_CONNECTED, {"target": target})
        self._token = str(doc["admin_token"])
        self._ctx = ssl.create_default_context(cafile=str(cdir / target / "ca.crt"))
        self._ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        self.pf = PortForward(kubectl, os.environ.get("BRAVO_PLATFORM_CONTEXT", "bravo-" + target.replace("-eks", "")),
                              "openbao", "pod/openbao-0", 8200)

    def call(self, port: int, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        conn = http.client.HTTPSConnection("127.0.0.1", port, context=self._ctx, timeout=15)
        try:
            conn.request(method, f"/v1/{path}", body=json.dumps(body).encode() if body is not None else None,
                         headers={"X-Vault-Token": self._token, "Content-Type": "application/json"})
            r = conn.getresponse()
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
        finally:
            conn.close()


# --- appliers ---------------------------------------------------------------------------------------------------

def _statements(raw: bytes) -> list[Any]:
    try:
        return sorted(_canon(s) for s in _as_list(json.loads(raw).get("Statement") or []))
    except (ValueError, AttributeError):
        return []


def apply_rgw(items: list[tuple[str, dict[str, Any]]], dest: dict[str, Any], kubectl: str, context: str,
              bao: CustodyBao) -> list[dict[str, Any]]:
    store = dest["object_store"]
    with bao.pf as bport:
        status, body = bao.call(bport, "GET", f"{dest.get('kv_mount', 'secret')}/data/{store['secret']}")
    if status != 200:
        raise Stop("failed", "bucket_owner_key_unavailable", Result.FAIL, {"status": status, "path": store["secret"]})
    rec = (body or {}).get("data", {}).get("data", {})
    ns, svc = store["rgw_service"].split("/", 1)
    out = []
    with PortForward(kubectl, context, ns, f"svc/{svc}", 443) as port:
        rgw = Rgw(port, store["bucket"], rec["ca_pem"], rec["access_key_id"], rec["secret_access_key"])
        try:
            for name, doc in items:
                before_status, before = rgw.policy_call("GET")
                prev = json.loads(before) if before_status == 200 else None
                raw = json.dumps(doc).encode()
                put_status, put_body = rgw.policy_call("PUT", raw)
                if put_status not in (200, 204):
                    raise Stop("failed", "bucket_policy_put_failed", Result.FAIL,
                               {"target": name, "status": put_status, "body": put_body[:300].decode(errors="replace")})
                get_status, got = rgw.policy_call("GET")
                same = get_status == 200 and _statements(got) == _statements(raw)
                if not same:
                    raise Stop("failed", "bucket_policy_readback_mismatch", Result.FAIL, {"target": name})
                out.append({"type": "rgw_bucket_policy", "target": name, "bucket": store["bucket"],
                            "applied_digest": digest(doc), "readback": "equal",
                            "replaced": {"digest": digest(prev) if prev else None,
                                         "sids": [s.get("Sid") for s in _as_list((prev or {}).get("Statement") or [])
                                                  if isinstance(s, dict)]}})
        finally:
            rgw.close()
    return out


def apply_openbao(items: list[tuple[str, dict[str, Any]]], dest: dict[str, Any], bao: CustodyBao) -> list[dict[str, Any]]:
    store = dest["secret_store"]
    out = []
    with bao.pf as port:
        for name, doc in items:
            pname = openbao_policy_name(name, store)
            hcl = openbao_hcl(doc)
            prev_status, prev = bao.call(port, "GET", f"sys/policies/acl/{pname}")
            status, _ = bao.call(port, "PUT", f"sys/policies/acl/{pname}", {"policy": hcl})
            if status not in (200, 204):
                raise Stop("failed", "openbao_policy_put_failed", Result.FAIL, {"policy": pname, "status": status})
            got_status, got = bao.call(port, "GET", f"sys/policies/acl/{pname}")
            if got_status != 200 or ((got or {}).get("data") or {}).get("policy") != hcl:
                raise Stop("failed", "openbao_policy_readback_mismatch", Result.FAIL, {"policy": pname})
            out.append({"type": "openbao_policy", "target": name, "policy": pname,
                        "applied_digest": "sha256:" + hashlib.sha256(hcl.encode()).hexdigest(), "readback": "equal",
                        "replaced": prev_status == 200})
    return out


def apply_all(rgw_items: list[tuple[str, dict[str, Any]]], bao_items: list[tuple[str, dict[str, Any]]],
              dest: dict[str, Any], kubectl: str, context: str, bao: CustodyBao | None = None) -> Step:
    if not rgw_items and not bao_items:
        return Step(Result.SKIPPED, {"reason": "no_external_policy"})
    bao = bao or CustodyBao(kubectl)
    applied = (apply_rgw(rgw_items, dest, kubectl, context, bao) if rgw_items else []) + \
              (apply_openbao(bao_items, dest, bao) if bao_items else [])
    return Step(detail={"applied": applied})
