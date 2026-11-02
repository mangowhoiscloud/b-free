"""``python -m bravo_audit follow|verify|check-bucket``.

Settings: BRAVO_AUDIT_BUCKET, BRAVO_AUDIT_REGION (default ap-northeast-2), BRAVO_AUDIT_S3_ENDPOINT (RGW; unset
for AWS S3), BRAVO_AUDIT_MODE (COMPLIANCE), BRAVO_AUDIT_RETENTION_DAYS (1), BRAVO_API_BASE, BRAVO_AUDIT_TOKEN_ENV
(name of the env var holding the Access JWT; default BRAVO_AUDIT_TOKEN). S3 credentials: standard AWS chain.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .forwarder import Forwarder
from .sink import ObjectLockSink


def _sink() -> ObjectLockSink:
    bucket = os.environ.get("BRAVO_AUDIT_BUCKET", "").strip()
    if not bucket:
        raise SystemExit("BRAVO_AUDIT_BUCKET is not set")
    return ObjectLockSink(bucket, region=os.environ.get("BRAVO_AUDIT_REGION", "ap-northeast-2"),
                          endpoint_url=os.environ.get("BRAVO_AUDIT_S3_ENDPOINT") or None,
                          mode=os.environ.get("BRAVO_AUDIT_MODE", "COMPLIANCE"),
                          retention_days=int(os.environ.get("BRAVO_AUDIT_RETENTION_DAYS", "1")))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("botocore", "boto3", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # wire logs can carry headers
    p = argparse.ArgumentParser(prog="bravo-audit")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("follow", help="stream plan events into the Object Lock bucket")
    f.add_argument("--plan", action="append", required=True, help="plan id (repeat; one stream each, in turn)")
    f.add_argument("--idle-exit", type=float, default=None, help="return after N seconds without events")
    v = sub.add_parser("verify", help="re-read stored records: sha256, lock mode, retain-until")
    v.add_argument("--plan", required=True)
    sub.add_parser("check-bucket", help="show the bucket's Object Lock configuration")
    a = p.parse_args(argv)
    sink = _sink()
    if a.cmd == "check-bucket":
        print(json.dumps(sink.check_bucket(), indent=2, default=str))
        return 0
    if a.cmd == "verify":
        res = sink.verify(a.plan)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if not res["bad"] else 1
    sink.check_bucket()
    api = os.environ.get("BRAVO_API_BASE", "").strip()
    if not api:
        raise SystemExit("BRAVO_API_BASE is not set")
    fwd = Forwarder(api, sink, token_env=os.environ.get("BRAVO_AUDIT_TOKEN_ENV", "BRAVO_AUDIT_TOKEN"))
    total = 0
    for plan in a.plan:
        total += fwd.follow(plan, idle_exit_s=a.idle_exit)
    print(json.dumps({"stored": total}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
