"""Default encryption (SSE-KMS) of the on-prem RGW attachment bucket, with the key in the platform OpenBao (OBJ-ENC, D-26).

  uv run --project platform/gate python deploy/cloud/rgw_encryption.py --context bravo-onprem            # state only
  uv run --project platform/gate python deploy/cloud/rgw_encryption.py --context bravo-onprem --apply    # set default
  uv run --project platform/gate python deploy/cloud/rgw_encryption.py --context bravo-onprem --proof    # one object
  uv run --project platform/gate python deploy/cloud/rgw_encryption.py --context bravo-onprem --outage   # link down
  uv run --project platform/gate python deploy/cloud/rgw_encryption.py --context bravo-onprem --inventory  # every object
  uv run --project platform/gate python deploy/cloud/rgw_encryption.py --context bravo-onprem --reencrypt  # plaintext ones

--apply: PutBucketEncryption aws:kms with KMSMasterKeyID rgw-<env>, the transit key that keys.sh rgw-kms-init <env>
  created in the platform OpenBao. RGW needs CephObjectStore security.kms (overlays/rke2/rook/objectstore.yaml) and the
  link to the platform OpenBao (rgw-kms-link.yaml, secrets from onprem-tokens.sh rgw-kms).
--proof: PUT a test object WITHOUT SSE headers (the bucket default applies), print the response headers,
  radosgw-admin object stat (crypt mode, key id, wrapped data key shortened), check that the RADOS head object does not
  contain the plaintext, GET and compare bytes, DELETE.
--outage: PUT object A, scale the link client plat-bao-link to 0, try PUT B and GET A (expected to fail: RGW cannot
  reach OpenBao), scale back, GET A again, DELETE A. The link is down for a few seconds (5 s on 2026-11-03).
--inventory: every object in the bucket: HEAD (SSE headers), radosgw-admin object stat (user.rgw.crypt.mode, key id).
--reencrypt (B13, 2026-11-03): objects stored before the default (no crypt mode) are copied in place with SSE-KMS
  (CopyObject onto the same key, MetadataDirective REPLACE with the object's own content headers and user metadata,
  x-amz-server-side-encryption aws:kms + key rgw-<env>; RGW refuses COPY onto itself). Before and
  after: sha256 of the body over GET, content type, user metadata; after: crypt mode SSE-KMS and a GET that reads.
  The canary _gate/canary is one of them; its body and key stay the same.
Only the encryption configuration, the test objects and (--reencrypt) the storage encryption of existing objects are
touched: never the bucket policy or Block Public Access (the S1 planted regression stays). The bucket owner's keys come from OpenBao secret/envs/<env>/object-store (as rgw_regression.py)
and stay in this process. RGW is reached through a private kubectl port-forward (the RGW certificate names localhost).
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_roles import Bao  # noqa: E402
from rgw_regression import port_forward  # noqa: E402

KST = timezone(timedelta(hours=9))
PROOF_PREFIX = "_obj-enc-proof/"
RADOS = ("--conf=/var/lib/rook/rook-ceph/rook-ceph.config", "--keyring=/var/lib/rook/rook-ceph/client.admin.keyring")
SSE_HEADERS = ("x-amz-server-side-encryption", "x-amz-server-side-encryption-aws-kms-key-id")


def log(msg: str) -> None:
    print(f"rgw_encryption {datetime.now(KST).strftime('%Y-%m-%dT%H:%M:%S%z')}: {msg}", flush=True)


def kubectl(ctx: str, *args: str, check: bool = True) -> str:
    # radosgw-admin prints some xattrs as raw bytes inside JSON strings: decode leniently.
    return subprocess.run(["kubectl", "--context", ctx, *args], capture_output=True, timeout=300,
                          check=check).stdout.decode("utf-8", errors="replace")


def operator_exec(ctx: str, script: str) -> str:
    """radosgw-admin / rados inside the Rook operator pod (admin keyring stays in the cluster)."""
    return kubectl(ctx, "-n", "rook-ceph", "exec", "deploy/rook-ceph-operator", "--", "sh", "-c", script)


def err(exc: ClientError) -> str:
    md = exc.response.get("ResponseMetadata", {})
    return f"{md.get('HTTPStatusCode', '?')} {exc.response.get('Error', {}).get('Code', '')}".strip()


def sse(headers: dict) -> dict:
    return {h: headers.get(h) for h in SSE_HEADERS}


class Bucket:
    def __init__(self, ctx: str, env: str) -> None:
        self.ctx, self.env = ctx, env
        store = Bao().read(f"envs/{env}/object-store")
        missing = [k for k in ("access_key_id", "secret_access_key", "bucket", "ca_pem") if not store.get(k)]
        if missing:
            raise SystemExit(f"secret/envs/{env}/object-store lacks {missing}")
        work = Path(tempfile.mkdtemp(prefix="bravo-rgw-enc-"))
        atexit.register(shutil.rmtree, work, True)
        ca = work / "rgw-ca.pem"  # public CA certificate
        ca.write_text(store["ca_pem"])
        self.name = store["bucket"]
        self.endpoint = f"https://localhost:{port_forward(ctx)}"
        self.s3 = boto3.session.Session(aws_access_key_id=store["access_key_id"],
                                        aws_secret_access_key=store["secret_access_key"],
                                        region_name="us-east-1").client(
            "s3", endpoint_url=self.endpoint, verify=str(ca),
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1, "mode": "standard"},
                          connect_timeout=10, read_timeout=60, request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"))
        store.clear()

    def encryption(self) -> list[dict] | str:
        try:
            return self.s3.get_bucket_encryption(Bucket=self.name)["ServerSideEncryptionConfiguration"]["Rules"]
        except ClientError as exc:
            return err(exc)

    def untouched(self) -> dict:
        """Bucket policy Sids and Block Public Access, read only (another lane owns them; S1 planted regression)."""
        try:
            pol = json.loads(self.s3.get_bucket_policy(Bucket=self.name)["Policy"])
            sids = [s.get("Sid") for s in (pol.get("Statement") or [])]
        except ClientError as exc:
            sids = [err(exc)]
        try:
            pab = self.s3.get_public_access_block(Bucket=self.name)["PublicAccessBlockConfiguration"]
        except ClientError as exc:
            pab = err(exc)
        return {"policy_sids": sids, "public_access_block": pab}

    def put(self, key: str, body: bytes) -> dict:
        r = self.s3.put_object(Bucket=self.name, Key=key, Body=body, ContentType="application/octet-stream")
        return {"status": r["ResponseMetadata"]["HTTPStatusCode"], **sse(r["ResponseMetadata"]["HTTPHeaders"]),
                "etag": r.get("ETag")}

    def get(self, key: str) -> tuple[dict, bytes]:
        r = self.s3.get_object(Bucket=self.name, Key=key)
        return {"status": r["ResponseMetadata"]["HTTPStatusCode"], **sse(r["ResponseMetadata"]["HTTPHeaders"])}, \
            r["Body"].read()

    def delete(self, key: str) -> str:
        try:
            return str(self.s3.delete_object(Bucket=self.name, Key=key)["ResponseMetadata"]["HTTPStatusCode"])
        except ClientError as exc:
            return err(exc)

    def object_stat(self, key: str) -> dict:
        out = operator_exec(self.ctx, f"radosgw-admin {' '.join(RADOS)} --rgw-realm=bravo --rgw-zonegroup=bravo "
                                      f"--rgw-zone=bravo object stat --bucket={self.name} --object='{key}'")
        attrs = json.loads(out).get("attrs", {})
        crypt = {k: v for k, v in attrs.items() if k.startswith("user.rgw.crypt")}
        if "user.rgw.crypt.datakey" in crypt:  # wrapped by OpenBao transit; shortened anyway
            dk = crypt["user.rgw.crypt.datakey"]
            crypt["user.rgw.crypt.datakey"] = f"{dk[:17]}...({len(dk)} chars)"
        if "user.rgw.crypt.keysel" in crypt:  # binary key selector; not printed
            crypt["user.rgw.crypt.keysel"] = "(binary, not shown)"
        return crypt

    def rados_head_has(self, key: str, marker: bytes) -> dict:
        """Read the RADOS object(s) holding `key` in the data pool and look for the plaintext marker in them."""
        b = json.loads(operator_exec(self.ctx, f"radosgw-admin {' '.join(RADOS)} --rgw-realm=bravo "
                                               f"--rgw-zonegroup=bravo --rgw-zone=bravo bucket stats --bucket={self.name}"))
        leaf = key.rsplit("/", 1)[-1]
        # `rados ls` prints "oid<TAB>locator"; a key that starts with "_" is stored as <marker>__<key> with a locator.
        out = operator_exec(self.ctx, f"""p=bravo.rgw.buckets.data; n=0; t="$(printf '\\t')"
            rados {' '.join(RADOS)} -p $p ls | grep -F '{b['marker']}_' | grep -F '{leaf}' >/tmp/obj-enc.ls
            while IFS="$t" read -r o loc; do
              rados {' '.join(RADOS)} -p $p ${{loc:+--object-locator "$loc"}} get "$o" /tmp/obj-enc.bin
              echo "$o $(wc -c </tmp/obj-enc.bin) $(grep -c '{marker.decode()}' /tmp/obj-enc.bin)"; n=1
            done </tmp/obj-enc.ls; rm -f /tmp/obj-enc.bin /tmp/obj-enc.ls; [ $n = 1 ] || echo none""")
        rows = [ln.split() for ln in out.strip().splitlines() if ln.strip() and ln.strip() != "none"]
        return {"rados_objects": [{"oid": r[0], "bytes": int(r[1]), "plaintext_marker_found": int(r[2]) > 0}
                                  for r in rows if len(r) == 3]}


def body_for(tag: str) -> tuple[bytes, bytes]:
    marker = f"BRAVO-OBJ-ENC-PLAINTEXT-{tag}".encode()
    return marker, marker + b"\n" + os.urandom(65536) + b"\n" + marker + b"\n"


def proof(b: Bucket) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{PROOF_PREFIX}{stamp}.bin"
    marker, body = body_for(stamp)
    log(f"PUT {key} ({len(body)} bytes, sha256 {hashlib.sha256(body).hexdigest()[:16]}), no SSE request headers")
    try:
        print("put_response:", json.dumps(b.put(key, body)))
        print("object_stat_crypt_attrs:", json.dumps(b.object_stat(key)))
        print("rados_head:", json.dumps(b.rados_head_has(key, marker)))
        got, data = b.get(key)
        print("get_response:", json.dumps({**got, "bytes": len(data), "sha256_equal": hashlib.sha256(data).digest()
                                           == hashlib.sha256(body).digest()}))
    finally:
        print("delete:", b.delete(key))
    return 0


def object_rows(b: Bucket) -> list[dict]:
    """Every object: key, size, SSE headers from HEAD, crypt attributes from radosgw-admin."""
    rows = []
    for page in b.s3.get_paginator("list_objects_v2").paginate(Bucket=b.name):
        for o in page.get("Contents") or []:
            head = b.s3.head_object(Bucket=b.name, Key=o["Key"])
            hh = head["ResponseMetadata"]["HTTPHeaders"]
            crypt = b.object_stat(o["Key"])
            rows.append({"object": o["Key"], "size": o["Size"], "content_type": head.get("ContentType"),
                         "head": sse(hh), "crypt_mode": crypt.get("user.rgw.crypt.mode"),
                         "crypt_keyid": crypt.get("user.rgw.crypt.keyid")})
    return rows


def inventory(b: Bucket) -> int:
    rows = object_rows(b)
    for r in rows:
        print("object:", json.dumps(r, ensure_ascii=False))
    plain = [r["object"] for r in rows if r["crypt_mode"] != "SSE-KMS"]
    print("summary:", json.dumps({"objects": len(rows), "sse_kms": len(rows) - len(plain), "not_sse_kms": plain}))
    return 0


def body_digest(b: Bucket, key: str) -> tuple[dict, str, int]:
    got, data = b.get(key)
    return got, hashlib.sha256(data).hexdigest(), len(data)


def reencrypt(b: Bucket, env: str) -> int:
    key_id = f"rgw-{env}"
    rows = object_rows(b)
    todo = [r for r in rows if r["crypt_mode"] != "SSE-KMS"]
    log(f"{len(rows)} objects, {len(todo)} without SSE-KMS: {[r['object'] for r in todo]}")
    failed = 0
    for r in todo:
        key = r["object"]
        before_head = b.s3.head_object(Bucket=b.name, Key=key)
        _, before_sha, before_len = body_digest(b, key)
        # RGW refuses a copy onto itself with MetadataDirective COPY even when only the SSE headers change
        # ("InvalidRequest ... without changing the object's metadata ... or encryption attributes", measured
        # 2026-11-03 02:35 KST). REPLACE with the object's own headers and user metadata, read from HEAD.
        keep = {k: before_head[k] for k in ("ContentType", "ContentDisposition", "ContentEncoding", "ContentLanguage",
                                            "CacheControl", "Expires") if before_head.get(k)}
        resp = b.s3.copy_object(Bucket=b.name, Key=key, CopySource={"Bucket": b.name, "Key": key},
                                MetadataDirective="REPLACE", Metadata=before_head.get("Metadata") or {}, **keep,
                                ServerSideEncryption="aws:kms", SSEKMSKeyId=key_id)
        after_head = b.s3.head_object(Bucket=b.name, Key=key)
        got, after_sha, after_len = body_digest(b, key)
        crypt = b.object_stat(key)
        same = (before_sha == after_sha and before_len == after_len
                and before_head.get("ContentType") == after_head.get("ContentType")
                and before_head.get("Metadata") == after_head.get("Metadata"))
        ok = same and crypt.get("user.rgw.crypt.mode") == "SSE-KMS" and got["status"] == 200
        failed += 0 if ok else 1
        print("reencrypt:", json.dumps({
            "object": key, "copy_status": resp["ResponseMetadata"]["HTTPStatusCode"],
            "copy_sse": sse(resp["ResponseMetadata"]["HTTPHeaders"]),
            "before": {"bytes": before_len, "sha256": before_sha, "content_type": before_head.get("ContentType"),
                       "user_metadata_keys": sorted(before_head.get("Metadata") or {})},
            "after": {"bytes": after_len, "sha256": after_sha, "content_type": after_head.get("ContentType"),
                      "user_metadata_keys": sorted(after_head.get("Metadata") or {}), "get": got,
                      "crypt_mode": crypt.get("user.rgw.crypt.mode"), "crypt_keyid": crypt.get("user.rgw.crypt.keyid")},
            "body_and_metadata_same": same, "ok": ok}, ensure_ascii=False))
    return inventory(b) or (1 if failed else 0)


def link_replicas(ctx: str, n: int) -> None:
    kubectl(ctx, "-n", "rook-ceph", "scale", "deploy/plat-bao-link", f"--replicas={n}")
    if n == 0:
        kubectl(ctx, "-n", "rook-ceph", "wait", "--for=delete", "pod", "-l", "app.kubernetes.io/name=plat-bao-link",
                "--timeout=120s", check=False)
    else:
        kubectl(ctx, "-n", "rook-ceph", "rollout", "status", "deploy/plat-bao-link", "--timeout=180s")


def timed(fn):
    t = time.monotonic()
    try:
        r = fn()
        return "ok", r, round(time.monotonic() - t, 1)
    except ClientError as exc:
        return "error", err(exc), round(time.monotonic() - t, 1)
    except Exception as exc:  # noqa: BLE001 - recorded as measured
        return "error", f"{type(exc).__name__}: {exc}"[:200], round(time.monotonic() - t, 1)


def outage(b: Bucket) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key_a, key_b = f"{PROOF_PREFIX}{stamp}-a.bin", f"{PROOF_PREFIX}{stamp}-b.bin"
    _, body_a = body_for(stamp + "-a")
    _, body_b = body_for(stamp + "-b")
    print("put_a_link_up:", json.dumps(b.put(key_a, body_a)))
    log("scale rook-ceph/plat-bao-link to 0 (RGW cannot reach the platform OpenBao)")
    link_replicas(b.ctx, 0)
    try:
        print("put_b_link_down:", json.dumps(timed(lambda: b.put(key_b, body_b))))
        print("get_a_link_down:", json.dumps(timed(lambda: len(b.get(key_a)[1]))))
        print("get_canary_link_down (stored before default encryption):",
              json.dumps(timed(lambda: len(b.s3.get_object(Bucket=b.name, Key="_gate/canary")["Body"].read()))))
    finally:
        log("scale rook-ceph/plat-bao-link back to 2")
        link_replicas(b.ctx, 2)
    print("get_a_link_up:", json.dumps(timed(lambda: hashlib.sha256(b.get(key_a)[1]).digest()
                                             == hashlib.sha256(body_a).digest())))
    print("delete_a:", b.delete(key_a), "delete_b:", b.delete(key_b))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default="target-onprem")
    ap.add_argument("--context", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="PutBucketEncryption aws:kms with key rgw-<env>")
    mode.add_argument("--proof", action="store_true", help="one test object end to end, then delete it")
    mode.add_argument("--outage", action="store_true", help="measure PUT/GET with the OpenBao link scaled to 0")
    mode.add_argument("--inventory", action="store_true", help="crypt mode of every object")
    mode.add_argument("--reencrypt", action="store_true", help="copy objects without SSE-KMS in place with SSE-KMS")
    a = ap.parse_args()
    b = Bucket(a.context, a.env)
    log(f"bucket {b.name} at {b.endpoint} (port-forward to {a.context} rook-ceph-rgw-bravo:443)")
    if a.apply:
        key_id = f"rgw-{a.env}"
        b.s3.put_bucket_encryption(Bucket=b.name, ServerSideEncryptionConfiguration={"Rules": [
            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": key_id}}]})
        log(f"PutBucketEncryption {b.name}: aws:kms, KMSMasterKeyID {key_id} (platform OpenBao transit key)")
    print("get_bucket_encryption:", json.dumps(b.encryption()))
    print("unchanged_by_this_tool:", json.dumps(b.untouched()))
    if a.proof:
        return proof(b)
    if a.outage:
        return outage(b)
    if a.inventory:
        return inventory(b)
    if a.reencrypt:
        return reencrypt(b, a.env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
