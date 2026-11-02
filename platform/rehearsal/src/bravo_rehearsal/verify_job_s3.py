"""Platform-held test of the behavioural verification Job (B7). Runs inside the Job Pod, standard library only.

The service (``behavioral.py``) puts this file, ``params.json`` and the RGW CA into a ConfigMap and the test bucket
owner's key into a Secret of the Job's namespace. The agent never sees any of them. The service never imports this
module; tests import ``sign`` only.

Steps, all against the rehearsal test bucket (never the destination's attachment bucket):

0. egress: TCP to the internet and the metadata address must fail (the Job's NetworkPolicy allows RGW only);
1. reset: delete any bucket policy left behind, then PUT this run's canary object as the owner;
2. control: a known-open policy (unsigned s3:GetObject on the canary) must give an unsigned GET 200. Otherwise this
   bucket cannot show an open policy and a later refusal proves nothing;
3. per proposed policy: PutBucketPolicy must be accepted, an unsigned GET of the canary must be refused with
   AccessDenied, a signed GET with the owner's key must return 200; then the policy is deleted;
4. cleanup: delete the policy and the canary.
The last stdout line is one JSON object; the service reads it from the Pod log. Values of the key are never printed.
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
import sys
import urllib.parse

DENIED = ("AccessDenied", "AllAccessDisabled")  # same set as the gate (platform/gate probes/objstore.py, audit A5)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def sign(method: str, host: str, path: str, query: str, payload_sha256: str, access_key: str, secret_key: str,
         amz_date: str, region: str = "us-east-1") -> dict[str, str]:
    """AWS Signature Version 4 headers for S3 with signed headers host, x-amz-content-sha256, x-amz-date.

    ``path`` is already URI-encoded; ``query`` is the canonical query string (e.g. ``policy=``)."""
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


class Rgw:
    def __init__(self, endpoint: str, bucket: str, cafile: str, access_key: str, secret_key: str) -> None:
        u = urllib.parse.urlsplit(endpoint)
        self.host, self.port, self.bucket = str(u.hostname), u.port or 443, bucket
        self.host_header = self.host if self.port == 443 else f"{self.host}:{self.port}"
        self.ctx = ssl.create_default_context(cafile=cafile)
        self.ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)  # the install's CA has no AKI
        self._ak, self._sk = access_key, secret_key

    def call(self, method: str, key: str = "", query: str = "", body: bytes = b"", signed: bool = True) -> dict:
        path = "/" + self.bucket + ("/" + urllib.parse.quote(key, safe="/-_.~") if key else "")
        headers: dict[str, str] = {}
        if signed:
            amz = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
            headers = sign(method, self.host_header, path, query, hashlib.sha256(body).hexdigest(), self._ak,
                           self._sk, amz)
        conn = http.client.HTTPSConnection(self.host, self.port, context=self.ctx, timeout=10)
        try:
            conn.request(method, path + (f"?{query}" if query else ""), body=body or None, headers=headers)
            resp = conn.getresponse()
            data = resp.read(65536)
        except (OSError, http.client.HTTPException) as exc:
            return {"status": 0, "code": type(exc).__name__}
        finally:
            conn.close()
        m = re.search(rb"<Code>([^<]{1,64})</Code>", data)
        return {"status": resp.status, "code": m.group(1).decode() if m else ""}


def text(r: dict | None) -> str:
    return "not_run" if r is None else f"{r['status']} {r['code']}".strip()


def egress(targets: list) -> list[dict]:
    out = []
    for host, port in targets:
        try:
            socket.create_connection((host, int(port)), 3).close()
            out.append({"target": f"{host}:{port}", "blocked": False})
        except OSError as exc:
            out.append({"target": f"{host}:{port}", "blocked": True, "error": type(exc).__name__})
    return out


def run(p: dict, s3: Rgw) -> dict:
    out: dict = {"run": p["run"], "bucket": p["bucket"], "egress": egress(p.get("egress_canaries") or [])}
    key = p["canary_key"]
    out["reset"] = text(s3.call("DELETE", query="policy="))
    put = s3.call("PUT", key=key, body=b"bravo rehearsal verification canary\n")
    out["canary_put"] = text(put)
    if put["status"] != 200:
        out["error"] = "canary_put_failed"
        return out
    c_put = s3.call("PUT", query="policy=", body=json.dumps(p["control_policy"]).encode())
    c_get = s3.call("GET", key=key, signed=False)
    c_del = s3.call("DELETE", query="policy=")
    out["control"] = {"policy_put": text(c_put), "anonymous_get": text(c_get), "policy_delete": text(c_del),
                      "ok": c_put["status"] in (200, 204) and c_get["status"] == 200}
    results = []
    for item in p["policies"] if out["control"]["ok"] else []:
        put_p = s3.call("PUT", query="policy=", body=json.dumps(item["policy"]).encode())
        applied = put_p["status"] in (200, 204)
        anon = s3.call("GET", key=key, signed=False) if applied else None
        auth = s3.call("GET", key=key) if applied else None
        results.append({"index": item["index"], "policy_delete": text(s3.call("DELETE", query="policy=")),
                        "checks": [
                            {"name": "policy_applied", "ok": applied, "detail": text(put_p)},
                            {"name": "anonymous_get_denied", "ok": bool(anon and anon["status"] == 403
                                                                        and anon["code"] in DENIED),
                             "detail": text(anon)},
                            {"name": "authenticated_get_allowed", "ok": bool(auth and auth["status"] == 200),
                             "detail": text(auth)}]})
    out["policies"] = results
    out["cleanup"] = {"policy_delete": text(s3.call("DELETE", query="policy=")),
                      "canary_delete": text(s3.call("DELETE", key=key))}
    return out


def main() -> int:
    with open("/cfg/params.json", encoding="utf-8") as f:
        p = json.load(f)
    s3 = Rgw(p["endpoint"], p["bucket"], "/cfg/ca.crt", os.environ["S3_ACCESS_KEY_ID"], os.environ["S3_SECRET_ACCESS_KEY"])
    print(json.dumps(run(p, s3), sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
