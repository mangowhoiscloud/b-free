"""Planted S1 regression on the on-prem RGW attachment bucket (심은 시나리오), opt-in and removable.

Demo S1 (spec 10): the destination attachment store is open to anonymous read, so the gate's OBJ-01 is FAIL on the
destination and the pair is BLOCK. This tool plants exactly that and takes it out again. Off by default: without a
flag it only reports the current state. Same pattern as the DB regression (db_roles.py --plant-regression,
03_dst.sql under BRAVO_PLANT_REGRESSION=1).

  python3 deploy/cloud/rgw_regression.py --context bravo-onprem                       # state only, changes nothing
  python3 deploy/cloud/rgw_regression.py --context bravo-onprem --plant-regression    # 심은 시나리오 적용
  python3 deploy/cloud/rgw_regression.py --context bravo-onprem --remove-regression   # 심은 시나리오 제거
  ... [--gate-report FILE]   then run the gate source-aws <-> --env and write its full report to FILE
  ... [--gate-serve PORT]    then serve the gate API on 127.0.0.1:PORT (same registry) until interrupted

Plant: Block Public Access off (all four flags false; RGW refuses a public policy while BlockPublicPolicy is on)
and one statement added to the bucket policy, Sid ``BravoPlantedRegressionAnonymousRead`` (Principal "*",
s3:GetObject on <bucket>/*). Every other statement stays as it is.
Remove: that one statement is taken out again and Block Public Access goes back on (the install's state,
deploy/landing-zone/scripts/rgw-bucket.sh). Both end with an unsigned GET of the canary (_gate/canary) and print
what RGW answered. Only the bucket policy and Block Public Access are touched. Encryption settings are not.

Keys (D-26): the bucket owner's keys come from the platform OpenBao secret/envs/<env>/object-store (re-piped by the
landing-zone install, step rgw), read with the custody admin token like db_roles.py. They reach the aws CLI and the
gate only through the child's environment, never argv or files. RGW is reached through a private kubectl
port-forward for this process (the RGW certificate names localhost).

--gate-report: the gate CLI (platform/gate, the code of the platform gate image) runs with
deploy/environments/real-targets.yaml plus an object_store section for --env that points at the port-forward
(written to a temporary file, references only). Source credentials are the operator's standard AWS chain.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db_roles import REPO, Bao

PLANTED_SID = "BravoPlantedRegressionAnonymousRead"
CANARY = "_gate/canary"
KST = timezone(timedelta(hours=9))
PAB_ON = "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
PAB_OFF = "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false"


def log(msg: str) -> None:
    print(f"rgw_regression {datetime.now(KST).strftime('%Y-%m-%dT%H:%M:%S%z')}: {msg}", flush=True)


def port_forward(ctx: str) -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(["kubectl", "--context", ctx, "-n", "rook-ceph", "port-forward", "svc/rook-ceph-rgw-bravo",
                             f"{port}:443"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atexit.register(proc.terminate)
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return port
        except OSError:
            if proc.poll() is not None:
                raise SystemExit("port-forward to rook-ceph-rgw-bravo exited") from None
            time.sleep(0.25)
    raise SystemExit("RGW not reachable through port-forward")


class S3:
    """aws CLI as the bucket owner. Keys only in the child's environment."""

    def __init__(self, store: dict, endpoint: str, ca_file: Path) -> None:
        self.endpoint, self.bucket = endpoint, store["bucket"]
        base = {k: v for k, v in os.environ.items()
                if not k.startswith("AWS_") and k not in ("HTTPS_PROXY", "HTTP_PROXY")}
        self.env = {**base, "AWS_ACCESS_KEY_ID": store["access_key_id"], "AWS_SECRET_ACCESS_KEY": store["secret_access_key"],
                    "AWS_REGION": "us-east-1", "AWS_CA_BUNDLE": str(ca_file), "AWS_PAGER": "",
                    "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
                    "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
                    "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required"}

    def api(self, *args: str, ok_codes: tuple[str, ...] = ()) -> dict | None:
        r = subprocess.run(["aws", "--endpoint-url", self.endpoint, "--output", "json", "s3api", *args,
                            "--bucket", self.bucket], env=self.env, capture_output=True, text=True, timeout=60,
                           check=False)
        if r.returncode != 0:
            if any(c in r.stderr for c in ok_codes):
                return None
            raise SystemExit(f"aws s3api {args[0]}: {r.stderr.strip()[-300:]}")
        return json.loads(r.stdout) if r.stdout.strip() else {}

    def policy(self) -> dict | None:
        out = self.api("get-bucket-policy", ok_codes=("NoSuchBucketPolicy",))
        return json.loads(out["Policy"]) if out else None

    def pab(self) -> dict | None:
        out = self.api("get-public-access-block", ok_codes=("NoSuchPublicAccessBlockConfiguration",))
        return (out or {}).get("PublicAccessBlockConfiguration")


def anonymous_get(endpoint: str, bucket: str, ctx: ssl.SSLContext) -> str:
    req = urllib.request.Request(f"{endpoint}/{bucket}/{CANARY}", method="GET")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:  # noqa: S310 - fixed https endpoint
            return f"{r.status} OK"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        code = body.split("<Code>", 1)[1].split("</Code>", 1)[0] if "<Code>" in body else ""
        return f"{exc.code} {code}".strip()


def statements(policy: dict | None) -> list[dict]:
    st = (policy or {}).get("Statement") or []
    return st if isinstance(st, list) else [st]


def planted_statement(bucket: str) -> dict:
    return {"Sid": PLANTED_SID, "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{bucket}/*"}


def with_planted(policy: dict | None, bucket: str) -> dict:
    kept = [s for s in statements(policy) if s.get("Sid") != PLANTED_SID]
    return {"Version": (policy or {}).get("Version", "2012-10-17"), "Statement": [*kept, planted_statement(bucket)]}


def without_planted(policy: dict | None) -> dict | None:
    kept = [s for s in statements(policy) if s.get("Sid") != PLANTED_SID]
    return {"Version": (policy or {}).get("Version", "2012-10-17"), "Statement": kept} if kept else None


def state(s3: S3, endpoint: str, ctx: ssl.SSLContext) -> dict:
    pol = s3.policy()
    return {"bucket": s3.bucket, "planted": any(x.get("Sid") == PLANTED_SID for x in statements(pol)),
            "policy_sids": [x.get("Sid") for x in statements(pol)], "public_access_block": s3.pab(),
            "anonymous_get_canary": anonymous_get(endpoint, s3.bucket, ctx)}


def gate_env(env: str, endpoint: str, ca_file: Path, store: dict) -> tuple[list[str], dict[str, str], Path]:
    """Command prefix, environment and cwd for the gate CLI (platform/gate) with this env's object_store at the
    port-forward. The registry and the CA bundle are temporary files with references and public certificates only."""
    gate_dir = REPO / "platform/gate"
    work = Path(tempfile.mkdtemp(prefix="bravo-gate-"))
    atexit.register(shutil.rmtree, work, True)
    uv = ["uv", "run", "--quiet", "--frozen", "--project", str(gate_dir), "python"]
    certifi = subprocess.run([*uv, "-c", "import certifi; print(certifi.where())"], capture_output=True, text=True,
                             check=True).stdout.strip()
    bundle = work / "ca-bundle.pem"  # public CA certificates only: certifi (AWS side) + the RGW CA
    bundle.write_text(Path(certifi).read_text() + "\n" + ca_file.read_text())
    registry = work / "environments.json"
    section = {"bucket": store["bucket"], "endpoint_url": endpoint, "region": "us-east-1", "addressing_style": "path",
               "canary_key": CANARY, "write_probes": False,
               "app_credential": {"access_key_env": "BRAVO_GATE_DST_S3_AK", "secret_key_env": "BRAVO_GATE_DST_S3_SK"}}
    merge = ("import json,sys,yaml; d=yaml.safe_load(open(sys.argv[1])); e=d['environments'][sys.argv[2]];"
             "e['object_store']=json.loads(sys.argv[3]); json.dump(d,open(sys.argv[4],'w'),indent=1)")
    subprocess.run([*uv, "-c", merge, str(REPO / "deploy/environments/real-targets.yaml"), env, json.dumps(section),
                    str(registry)], check=True)
    run_env = {**os.environ, "BRAVO_ENVIRONMENTS_FILE": str(registry), "AWS_CA_BUNDLE": str(bundle),
               "BRAVO_GATE_DST_S3_AK": store["access_key_id"], "BRAVO_GATE_DST_S3_SK": store["secret_access_key"]}
    return uv, run_env, gate_dir


def gate_report(env: str, endpoint: str, ca_file: Path, store: dict, out: Path) -> dict:
    """The gate CLI source-aws <-> env, full report to ``out``."""
    uv, run_env, gate_dir = gate_env(env, endpoint, ca_file, store)
    r = subprocess.run([*uv, "-m", "bravo_gate", "run", "--source", "source-aws", "--target", env, "--report"],
                       cwd=gate_dir, env=run_env, capture_output=True, text=True, timeout=600, check=False)
    if r.returncode != 0:
        raise SystemExit(f"gate run failed: {r.stderr.strip()[-500:]}")
    report = json.loads(r.stdout)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def gate_serve(env: str, endpoint: str, ca_file: Path, store: dict, port: int) -> int:
    """The gate HTTP API on 127.0.0.1:<port> with the same registry, until interrupted (e.g. BRAVO_GATE_URL of a
    CLI rehearsal: its controls_describe then reads this gate)."""
    uv, run_env, gate_dir = gate_env(env, endpoint, ca_file, store)
    run_env.update({"BRAVO_GATE_HOST": "127.0.0.1", "BRAVO_GATE_PORT": str(port)})
    log(f"gate serve http://127.0.0.1:{port} (source-aws, {env} object_store at {endpoint}); Ctrl-C to stop")
    try:
        return subprocess.run([*uv, "-m", "bravo_gate", "serve"], cwd=gate_dir, env=run_env, check=False).returncode
    except KeyboardInterrupt:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default="target-onprem")
    ap.add_argument("--context", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plant-regression", action="store_true",
                      help="심은 시나리오: open the attachment bucket to anonymous read (S1 gate BLOCK)")
    mode.add_argument("--remove-regression", action="store_true",
                      help="take the planted statement out and turn Block Public Access back on")
    ap.add_argument("--gate-report", type=Path, help="run the gate source-aws <-> --env and write the report here")
    ap.add_argument("--gate-serve", type=int, metavar="PORT",
                    help="then serve the gate API on 127.0.0.1:PORT with the same registry until interrupted")
    a = ap.parse_args()
    store = Bao().read(f"envs/{a.env}/object-store")
    missing = [k for k in ("access_key_id", "secret_access_key", "bucket", "ca_pem") if not store.get(k)]
    if missing:
        raise SystemExit(f"secret/envs/{a.env}/object-store lacks {missing}")
    work = Path(tempfile.mkdtemp(prefix="bravo-rgw-"))
    atexit.register(shutil.rmtree, work, True)
    ca_file = work / "rgw-ca.pem"  # public CA certificate
    ca_file.write_text(store["ca_pem"])
    ctx = ssl.create_default_context(cafile=str(ca_file))
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)  # the install's CA has no Authority Key Identifier
    endpoint = f"https://localhost:{port_forward(a.context)}"
    s3 = S3(store, endpoint, ca_file)
    if a.plant_regression:
        s3.api("put-public-access-block", "--public-access-block-configuration", PAB_OFF)
        s3.api("put-bucket-policy", "--policy", json.dumps(with_planted(s3.policy(), s3.bucket)))
        log(f"심은 시나리오 적용: 첨부 버킷 {s3.bucket} 익명 읽기 허용 (Sid {PLANTED_SID}, 공개 차단 해제, "
            f"--plant-regression). 제거: --remove-regression")
    elif a.remove_regression:
        kept = without_planted(s3.policy())
        if kept is None:
            s3.api("delete-bucket-policy")
        else:
            s3.api("put-bucket-policy", "--policy", json.dumps(kept))
        s3.api("put-public-access-block", "--public-access-block-configuration", PAB_ON)
        log(f"심은 시나리오 제거: {s3.bucket}에서 Sid {PLANTED_SID}를 빼고 공개 차단을 켬 (--remove-regression)")
    now = state(s3, endpoint, ctx)
    log(f"상태: {'심은 시나리오 적용됨' if now['planted'] else '심은 시나리오 없음'}")
    print(json.dumps({"env": a.env, "context": a.context, "at": datetime.now(KST).isoformat(), **now},
                     ensure_ascii=False))
    if a.gate_report:
        rep = gate_report(a.env, endpoint, ca_file, store, a.gate_report)
        for q in rep["questions"]:
            if q["control_id"].startswith("OBJ-"):
                ev = q["dst_evidence"].get("evidence") or {}
                print(f"gate {q['control_id']}: src={q['src']} dst={q['dst']} -> {q['verdict']} "
                      f"({q['reason'] or '-'}); dst {q['dst_evidence'].get('reason')} {ev.get('response', '')}")
        iam = next(c for c in rep["controls"] if c["control_id"] == "IAM-01")
        print(f"gate IAM-01: src={iam['src']} dst={iam['dst']} -> {iam['verdict']} ({iam['reason']})")
        print(f"gate verdict={rep['gate_verdict']} blocked={','.join(rep['blocked_controls'])} "
              f"input_digest={rep['input_digest']} report={a.gate_report}")
    if a.gate_serve:
        return gate_serve(a.env, endpoint, ca_file, store, a.gate_serve)
    return 0


if __name__ == "__main__":
    sys.exit(main())
