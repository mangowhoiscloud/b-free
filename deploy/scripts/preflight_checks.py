"""Read-only checks for deploy/scripts/preflight.sh (demo readiness, B16-B18, P6, P9). Never prints a secret value.

  python3 -I deploy/scripts/preflight_checks.py bao                       # platform OpenBao: seal, custody tokens,
                                                                          # RGW token, TokenRequest tokens, LLM
  deploy/pilot/scripts/keys.sh with-cloudflare -- python3 -I deploy/scripts/preflight_checks.py cf
                                                                          # Access service tokens, tunnels, LB, API token
  uv run --quiet --project platform/gate python deploy/scripts/preflight_checks.py rgw
                                                                          # RGW SSE-KMS read, plaintext count, S1 regression

Output: one line per check, tab-separated: STATUS GROUP CHECK DETAIL (STATUS is PASS, WARN or FAIL).
Expiry rule (PREFLIGHT_DEADLINE, default 2026-11-09T00:00:00+09:00, the night after the 11/8 demo):
  FAIL = expired or less than 2 h left, WARN = expires before the deadline, PASS = after it.
Values (tokens, client secrets, keys) stay in this process: only expiry times, IDs, names and hashes are printed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "deploy/cloud"))

KST = timezone(timedelta(hours=9))
NOW = datetime.now(timezone.utc)
DEADLINE = datetime.fromisoformat(os.environ.get("PREFLIGHT_DEADLINE", "2026-11-09T00:00:00+09:00"))
HARD_MARGIN = timedelta(hours=2)
PLATFORM_CTX = os.environ.get("BRAVO_PLATFORM_CONTEXT", "bravo-platform-rke2")
ONPREM_CTX = os.environ.get("BRAVO_ONPREM_CONTEXT", "bravo-onprem")
ENVS = ("platform", "source-aws", "target-onprem", "target-gcp")


def out(status: str, group: str, check: str, detail: str) -> None:
    print(f"{status}\t{group}\t{check}\t{detail}".replace("\n", " "), flush=True)


def record(kind: str, key: str, exp: datetime | None) -> None:
    """Machine-readable expiry for rotate-credentials.sh: KIND KEY EXPIRES_AT(ISO UTC or -) in PREFLIGHT_EXPIRY_FILE."""
    path = os.environ.get("PREFLIGHT_EXPIRY_FILE")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{kind}\t{key}\t{'-' if exp is None else exp.astimezone(timezone.utc).isoformat()}\n")


def parse_ts(v: object) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    s = v.strip().replace("Z", "+00:00")
    if "." in s:  # OpenBao returns nanoseconds; fromisoformat takes at most microseconds
        head, rest = s.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        s = f"{head}.{frac[:6]}{tz}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def kst(dt: datetime | None) -> str:
    return "-" if dt is None else dt.astimezone(KST).strftime("%m-%d %H:%M KST")


def left(dt: datetime) -> str:
    h = (dt - NOW).total_seconds() / 3600
    return f"{h:.1f}h 남음" if h >= 0 else f"{-h:.1f}h 지남"


def expiry(exp: datetime | None) -> str:
    """PASS/WARN/FAIL for a credential that stops working at exp (None = no expiry)."""
    if exp is None:
        return "PASS"
    if exp - NOW < HARD_MARGIN:
        return "FAIL"
    return "WARN" if exp < DEADLINE else "PASS"


def jwt_claims(token: str) -> dict:
    parts = token.strip().split(".")
    if len(parts) != 3:
        return {}
    try:
        return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return {}


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()[:8]


# ---- OpenBao -------------------------------------------------------------------------------------------------------
def bao():
    from db_roles import Bao  # admin token from custody, private port-forward to openbao-0 (as rgw_regression.py)

    return Bao()


def bao_call(b, method: str, path: str, body: dict | None = None) -> dict | None:
    try:
        return b._call(method, path, body)  # noqa: SLF001 - same helper db_roles.py uses
    except SystemExit as exc:  # Bao raises SystemExit("OpenBao GET x: HTTP n") on errors other than 404
        raise RuntimeError(str(exc)) from None


def bao_list(b, path: str) -> list[str]:
    r = bao_call(b, "GET", f"secret/metadata/{path}?list=true") or {}
    return [k for k in (r.get("data") or {}).get("keys", []) if not k.endswith("/")]


def lookup_self(b, token: str) -> tuple[dict | None, str]:
    """auth/token/lookup-self WITH that token (read only, does not renew). platform-admin has no lookup-accessor."""
    req = urllib.request.Request(f"{b._addr}/v1/auth/token/lookup-self", headers={"X-Vault-Token": token})  # noqa: SLF001
    try:
        with urllib.request.urlopen(req, context=b._ctx, timeout=15) as r:  # noqa: S310,SLF001 - port-forward
            return json.loads(r.read()).get("data"), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"  # 403 = expired, revoked or unknown token


def kubectl_json(ctx: str, *args: str) -> dict | None:
    r = subprocess.run(["kubectl", "--context", ctx, "--request-timeout=20s", *args, "-o", "json"],
                       capture_output=True, timeout=40, check=False)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout else None


def delivered_token(ctx: str, ns: str, name: str) -> str | None:
    """The token inside a delivered Secret (kubeconfig JSON or plain `token` key). Stays in this process."""
    s = kubectl_json(ctx, "-n", ns, "get", "secret", name)
    if not s:
        return None
    data = s.get("data") or {}
    if "kubeconfig" in data:
        cfg = json.loads(base64.b64decode(data["kubeconfig"]))
        users = cfg.get("users") or []
        return (users[0].get("user") or {}).get("token") if users else None
    if "token" in data:
        return base64.b64decode(data["token"]).decode()
    return None


# TokenRequest tokens (168h) in OpenBao and the copies their consumers actually use.
K8S_TOKENS = (
    ("collector-k8s", "수집기(온프렘 API 읽기)", PLATFORM_CTX, "bravo-control", "onprem-kubeconfig"),
    ("k8s-executor", "Executor(온프렘 exec)", PLATFORM_CTX, "bravo-control", "bx-envs-target-onprem-k8s-executor"),
    ("k8s-rehearsal", "리허설 서비스(온프렘)", PLATFORM_CTX, "bravo-control", "rehearsal-kubeconfig"),
    ("k8s-gate", "게이트(온프렘 읽기)", PLATFORM_CTX, "bravo-control", "gate-onprem-kubeconfig"),
)


def cmd_bao() -> int:
    try:
        b = bao()
    except SystemExit as exc:
        out("FAIL", "bao", "플랫폼 OpenBao 연결", f"닿지 않음: {exc}")
        return 0
    seal = bao_call(b, "GET", "sys/seal-status") or {}
    sealed = seal.get("sealed")
    out("PASS" if sealed is False else "FAIL", "bao", "봉인 상태",
        f"sealed={sealed} version={seal.get('version')} storage={seal.get('storage_type')}")

    # Custody tokens: periodic tokens (168h period) die if nobody renews them for a week. Each is looked up with
    # itself (lookup-self: read only, no renewal). rotate-credentials.sh custody renews them.
    custody_path = Path(os.environ.get("BRAVO_KEY_CUSTODY_DIR", str(Path.home() / ".bravo/custody"))) / "openbao-init.json"
    custody = json.loads(custody_path.read_text())
    toks = {k: v for k, v in custody.items() if k.endswith("_token") and k != "root_token" and v}
    for env, rec in (custody.get("rgw_kms_issuer") or {}).items():
        if rec.get("token"):
            toks[f"rgw_kms_issuer[{env}]"] = rec["token"]
    custody.clear()
    for name, tok in sorted(toks.items()):
        d, why = lookup_self(b, tok)
        if d is None:
            out("FAIL", "cred", f"custody {name}", f"조회 실패 {why}(만료 또는 폐기). keys.sh 해당 init으로 다시 발급")
            continue
        exp = parse_ts(d.get("expire_time"))
        record("custody", name, exp)
        kind = f"주기 {int(d.get('period') or 0) // 3600}h, renew-self로 연장" if d.get("period") else "주기 없음"
        out(expiry(exp), "cred", f"custody {name}", f"만료 {kst(exp)}{'' if exp is None else ' (' + left(exp) + ')'}, {kind}")
    toks.clear()

    # RGW SSE-KMS token (B16/B17): 1h TTL, renewed every 15 min by CronJob rook-ceph/rgw-kms-renew, hard limit 168h.
    # Its policy has no lookup-self (token_no_default_policy), so the TTL comes from the renewer's last success + lease.
    rgw = (bao_call(b, "GET", "secret/data/envs/target-onprem/rgw-kms") or {}).get("data", {}).get("data") or {}
    hard = parse_ts(rgw.get("renewable_until"))
    record("rgw-kms", "target-onprem", hard)
    out(expiry(hard) if rgw.get("token") else "FAIL", "cred", "RGW 토큰 최대 수명(168h)",
        f"재발급 필요 시각 {kst(hard)} ({left(hard) if hard else '-'}), 발급 {kst(parse_ts(rgw.get('issued_at')))}"
        if rgw.get("token") else "secret/envs/target-onprem/rgw-kms에 token 없음")
    if rgw.get("previous_accessor"):
        out("WARN", "cred", "RGW 이전 토큰", "previous_accessor 남음: 회전 뒤 keys.sh rgw-kms-retire target-onprem")
    rgw.clear()
    cj = kubectl_json(ONPREM_CTX, "-n", "rook-ceph", "get", "cronjob", "rgw-kms-renew") or {}
    last = parse_ts((cj.get("status") or {}).get("lastSuccessfulTime"))
    sched = parse_ts((cj.get("status") or {}).get("lastScheduleTime"))
    lease = "?"
    jobs = (kubectl_json(ONPREM_CTX, "-n", "rook-ceph", "get", "jobs") or {}).get("items") or []
    mine = sorted((j for j in jobs if (j["metadata"].get("ownerReferences") or [{}])[0].get("name") == "rgw-kms-renew"),
                  key=lambda j: j["metadata"]["creationTimestamp"])
    if mine:
        r = subprocess.run(["kubectl", "--context", ONPREM_CTX, "-n", "rook-ceph", "logs", f"job/{mine[-1]['metadata']['name']}",
                            "--tail=5"], capture_output=True, text=True, timeout=30, check=False)
        for ln in r.stdout.splitlines():
            if "lease" in ln:
                lease = ln.rsplit("lease", 1)[-1].strip().rstrip("s")
    if last is None:
        out("FAIL", "cred", "RGW 토큰 갱신(15분 주기)", "rook-ceph/rgw-kms-renew 성공 기록 없음")
    else:
        age = (NOW - last).total_seconds() / 60
        ttl_end = last + timedelta(seconds=int(lease)) if lease.isdigit() else None
        st = "PASS" if age <= 20 else ("WARN" if age <= 40 else "FAIL")
        if lease.isdigit() and int(lease) < 3600:
            st = "WARN" if st == "PASS" else st
        out(st, "cred", "RGW 토큰 갱신(15분 주기)", f"마지막 성공 {kst(last)} ({age:.0f}분 전, 마지막 예약 {kst(sched)}), "
            f"lease {lease}s, TTL 끝 {kst(ttl_end)}. 갱신이 1시간 멈추면 첨부 PUT/GET 500")

    # Kubernetes TokenRequest tokens: OpenBao copy and the copy the consumer mounts.
    for name, who, ctx, ns, secret in K8S_TOKENS:
        tok = ((bao_call(b, "GET", f"secret/data/envs/target-onprem/{name}") or {}).get("data") or {}).get("data", {}).get("token")
        if not tok:
            out("FAIL", "cred", f"온프렘 토큰 {name}", "OpenBao에 없음")
            continue
        claims = jwt_claims(tok)
        exp = datetime.fromtimestamp(claims["exp"], timezone.utc) if "exp" in claims else None
        live = delivered_token(ctx, ns, secret)
        if live is None:
            same = f"전달본 {ns}/{secret} 읽지 못함"
            st = "WARN"
        elif fingerprint(live) == fingerprint(tok):
            same, st = f"전달본 {ns}/{secret} 같음", expiry(exp)
        else:
            lexp = jwt_claims(live).get("exp")
            lexp_dt = datetime.fromtimestamp(lexp, timezone.utc) if lexp else None
            same, st = f"전달본 {ns}/{secret}이 다름(전달본 만료 {kst(lexp_dt)})", "FAIL" if expiry(lexp_dt) == "FAIL" else "WARN"
        live_exp = jwt_claims(live or "").get("exp")
        live_dt = datetime.fromtimestamp(live_exp, timezone.utc) if live_exp else None
        record("k8s", name, min(x for x in (exp, live_dt) if x) if (exp or live_dt) else None)
        out(st, "cred", f"온프렘 토큰 {name}", f"{who}: 만료 {kst(exp)} ({left(exp) if exp else '-'}), {same}")
        tok = live = ""

    # Model credentials (agent worker): stored or not, and what is known without using them.
    for p in ("claude", "openai"):
        md = (bao_call(b, "GET", f"secret/metadata/envs/platform/llm-{p}") or {}).get("data")
        if not md:
            out("FAIL" if p == "claude" else "WARN", "cred", f"llm-{p}", "저장 안 됨")
            continue
        note = ""
        if p == "openai":
            data = ((bao_call(b, "GET", "secret/data/envs/platform/llm-openai") or {}).get("data") or {}).get("data") or {}
            if "expires_at" in data:
                note = (f", 접근 토큰 만료 {kst(parse_ts(data.get('expires_at')))}(워커가 refresh_token으로 갱신),"
                        f" 마지막 refresh {kst(parse_ts(data.get('last_refresh')))}")
            data.clear()
        else:
            note = ", 만료는 값을 써야 알 수 있음(claude setup-token 발급분). 사용량 한도 상태는 알 수 없음"
        out("PASS", "cred", f"llm-{p}", f"저장됨 v{md.get('current_version')}, 갱신 {kst(parse_ts(md.get('updated_time')))}{note}")
    return 0


# ---- Cloudflare (run under keys.sh with-cloudflare) ----------------------------------------------------------------
CF_API = "https://api.cloudflare.com/client/v4"


def cf(path: str) -> dict:
    req = urllib.request.Request(f"{CF_API}{path}", headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed https host
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read())
        except ValueError:
            return {"success": False, "errors": [{"code": exc.code, "message": "HTTP error"}]}


def cf_all(path: str) -> list[dict]:
    items, page = [], 1
    sep = "&" if "?" in path else "?"
    while True:
        r = cf(f"{path}{sep}per_page=100&page={page}")
        if not r.get("success"):
            raise RuntimeError(json.dumps(r.get("errors"))[:200])
        items += r.get("result") or []
        info = r.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1):
            return items
        page += 1


def cmd_cf() -> int:
    acct, zone = os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""), os.environ.get("CLOUDFLARE_ZONE_ID", "")
    front = os.environ.get("PREFLIGHT_FRONT", "b-free-chat.mangowhois.ai")
    expect_active = os.environ.get("PREFLIGHT_EXPECT_ACTIVE", "src")

    v = cf("/user/tokens/verify")
    if not v.get("success"):
        v = cf(f"/accounts/{acct}/tokens/verify")
    r = v.get("result") or {}
    exp = parse_ts(r.get("expires_on"))
    record("cf-api-token", "platform/cloudflare", exp)
    out(("PASS" if exp is None else expiry(exp)) if r.get("status") == "active" else "FAIL", "cred", "Cloudflare API 토큰",
        f"status={r.get('status')} 만료 {kst(exp) if exp else '없음'}")

    # Access service tokens: Cloudflare is the authority for expiry; OpenBao holds the secret and a copy of expires_at.
    try:
        tokens = cf_all(f"/accounts/{acct}/access/service_tokens")
    except RuntimeError as exc:
        out("FAIL", "cred", "Access 서비스 토큰 목록", f"Cloudflare API 실패: {exc}")
        tokens = []
    by_id = {t["id"]: t for t in tokens}
    used: set[str] = set()
    try:
        b = bao()
        for env in ENVS:
            for name in bao_list(b, f"envs/{env}"):
                if not (name.startswith("cf-link-") or name == "cf-access-service-token"):
                    continue
                d = ((bao_call(b, "GET", f"secret/data/envs/{env}/{name}") or {}).get("data") or {}).get("data") or {}
                tid, bao_exp = d.get("token_id"), parse_ts(d.get("expires_at"))
                d.clear()
                t = by_id.get(tid or "")
                where = f"secret/envs/{env}/{name}"
                if not t:
                    out("FAIL", "cred", f"서비스 토큰 {env}/{name}", f"Cloudflare에 토큰 {tid} 없음({where})")
                    continue
                used.add(tid)
                exp = parse_ts(t.get("expires_at"))
                drift = "" if bao_exp == exp else f", OpenBao 기록 만료 {kst(bao_exp)}와 다름"
                unused = " (어느 정책에도 없는 시험 토큰)" if name == "cf-access-service-token" else ""
                if not unused:
                    record("service-token", f"{env}/{name}", exp)
                seen = parse_ts(t.get("last_seen_at"))
                out(expiry(exp) if not unused else "PASS", "cred", f"서비스 토큰 {t.get('name')}",
                    f"만료 {kst(exp)} ({left(exp) if exp else '-'}), 마지막 사용 {kst(seen)}, {where}{drift}{unused}")
    except (RuntimeError, SystemExit) as exc:
        out("FAIL", "cred", "서비스 토큰 ↔ OpenBao", f"OpenBao 읽기 실패: {exc}")
    for t in tokens:
        if t["id"] not in used:
            exp = parse_ts(t.get("expires_at"))
            out("WARN" if exp and exp > NOW else "PASS", "cred", f"서비스 토큰 {t.get('name')}",
                f"OpenBao에 비밀 없음(고아), 만료 {kst(exp)}. 쓰지 않으면 폐기")

    # Tunnels: user entry, management (customer clusters) and the platform tunnel.
    try:
        for tn in cf_all(f"/accounts/{acct}/cfd_tunnel?is_deleted=false"):
            if not str(tn.get("name", "")).startswith(("bravo-", "t1-")):
                continue  # other projects' tunnels in the same account
            st = tn.get("status")
            conns = len(tn.get("connections") or [])
            out({"healthy": "PASS", "degraded": "WARN"}.get(st, "FAIL"), "edge", f"터널 {tn.get('name')}",
                f"status={st}, 연결 {conns}")
    except RuntimeError as exc:
        out("FAIL", "edge", "터널 목록", f"Cloudflare API 실패: {exc}")

    # Load balancer of the customer front: pools healthy and weights where the demo starts (src 1, dst 0).
    try:
        lbs = cf_all(f"/zones/{zone}/load_balancers")
        pools = {p["id"]: p for p in cf_all(f"/accounts/{acct}/load_balancers/pools")}
        lb = next((x for x in lbs if x.get("name") == front), None)
        if lb is None:
            out("FAIL", "edge", f"LB {front}", "없음")
            return 0
        weights = (lb.get("random_steering") or {}).get("pool_weights") or {}
        fb = pools.get(lb.get("fallback_pool"), {}).get("name", lb.get("fallback_pool"))
        out("PASS" if lb.get("enabled") else "FAIL", "edge", f"LB {front}",
            f"enabled={lb.get('enabled')} steering={lb.get('steering_policy')} fallback={fb}")
        for pid in lb.get("default_pools") or []:
            p = pools.get(pid, {})
            pname = p.get("name", pid)
            h = cf(f"/accounts/{acct}/load_balancers/pools/{pid}/health").get("result") or {}
            pops = h.get("pop_health") or {}
            healthy = [bool(x.get("healthy")) for x in pops.values()]
            if not healthy:
                hs, hdesc = "WARN", "건강 정보 없음"
            else:
                ratio = sum(healthy) / len(healthy)
                hs = "PASS" if ratio >= 0.95 else ("WARN" if ratio > 0 else "FAIL")
                hdesc = f"정상 POP {sum(healthy)}/{len(healthy)}"
            w = weights.get(pid)
            want = 1 if pname.endswith(f"-{expect_active}") else 0
            ws = "PASS" if w is not None and float(w) == want else "WARN"
            out(hs if ws == "PASS" else ("FAIL" if hs == "FAIL" else "WARN"), "edge", f"LB pool {pname}",
                f"{hdesc}, enabled={p.get('enabled')}, 가중치 {w} (시작 기대값 {want}, PREFLIGHT_EXPECT_ACTIVE={expect_active})")
    except RuntimeError as exc:
        out("FAIL", "edge", "LB", f"Cloudflare API 실패: {exc}")
    return 0


# ---- RGW (run with uv --project platform/gate: boto3) --------------------------------------------------------------
def cmd_rgw() -> int:
    env = os.environ.get("PREFLIGHT_RGW_ENV", "target-onprem")
    try:
        from rgw_encryption import Bucket
        from rgw_regression import PLANTED_SID

        b = Bucket(ONPREM_CTX, env)
    except SystemExit as exc:
        out("FAIL", "rgw", "RGW 연결", f"{exc}")
        return 0
    enc = b.encryption()
    rule = (enc[0].get("ApplyServerSideEncryptionByDefault") or {}) if isinstance(enc, list) and enc else {}
    ok = rule.get("SSEAlgorithm") == "aws:kms" and rule.get("KMSMasterKeyID") == f"rgw-{env}"
    out("PASS" if ok else "FAIL", "rgw", "버킷 기본 암호화", f"{rule or enc}")

    # Encrypted-object read: RGW asks the platform OpenBao (transit decrypt) through the D-16 link t1-plat-bao.
    listed = b.s3.list_objects_v2(Bucket=b.name, MaxKeys=100).get("Contents") or []
    kms, plain = [], []
    for o in listed:
        h = b.s3.head_object(Bucket=b.name, Key=o["Key"])
        (kms if h.get("ServerSideEncryption") == "aws:kms" else plain).append((o["Size"], o["Key"]))
    if kms:
        size, key = min(kms)
        t = time.monotonic()
        try:
            got, data = b.get(key)
            ms = (time.monotonic() - t) * 1000
            st = "PASS" if got["status"] == 200 and len(data) == size else "FAIL"
            out(st, "rgw", "암호화 객체 읽기", f"GET {key} HTTP {got['status']}, {len(data)}/{size} bytes, "
                f"{got.get('x-amz-server-side-encryption')}, {ms:.0f} ms (링크 t1-plat-bao → OpenBao transit)")
        except Exception as exc:  # noqa: BLE001 - recorded as the measured failure
            out("FAIL", "rgw", "암호화 객체 읽기", f"GET {key} 실패: {type(exc).__name__} {str(exc)[:160]}")
    else:
        out("WARN", "rgw", "암호화 객체 읽기", f"SSE-KMS 객체 없음(앞 {len(listed)}개 확인). "
            "rgw_encryption.py --proof로 쓰기·읽기를 따로 확인")
    out("PASS" if not plain else "WARN", "rgw", "평문 객체", f"{len(plain)}개 / 확인한 {len(listed)}개"
        + (f" (B13 재암호화 대상: {', '.join(k for _, k in plain[:5])})" if plain else ""))

    # S1 planted regression (OBJ-01 BLOCK on the destination): planted = Sid present and Block Public Access off.
    u = b.untouched()
    planted = PLANTED_SID in (u.get("policy_sids") or [])
    pab = u.get("public_access_block")
    pab_off = isinstance(pab, dict) and not any(pab.values())
    code = "-"
    try:
        verify = b.s3._endpoint.http_session._verify  # noqa: SLF001 - the CA file the Bucket client already uses
        import ssl

        ctx = ssl.create_default_context(cafile=verify) if isinstance(verify, str) else ssl.create_default_context()
        req = urllib.request.Request(f"{b.endpoint}/{b.name}/_gate/canary")
        try:
            with urllib.request.urlopen(req, timeout=10, context=ctx) as r:  # noqa: S310 - port-forward
                code = str(r.status)
        except urllib.error.HTTPError as exc:
            code = str(exc.code)
    except Exception as exc:  # noqa: BLE001
        code = f"확인 못 함({type(exc).__name__})"
    want = os.environ.get("PREFLIGHT_EXPECT_REGRESSION", "planted")
    state = "planted" if planted and pab_off else ("removed" if not planted else "partial")
    out("PASS" if state == want else "WARN", "s1", "심은 퇴행(열린 버킷, OBJ-01)",
        f"상태 {state}(기대 {want}): Sid {'있음' if planted else '없음'}, Block Public Access "
        f"{'꺼짐' if pab_off else '켜짐'}, 서명 없는 GET _gate/canary HTTP {code}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    fn = {"bao": cmd_bao, "cf": cmd_cf, "rgw": cmd_rgw}.get(cmd)
    if fn is None:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - one FAIL line instead of a traceback (which could carry context)
        out("FAIL", cmd, f"{cmd} 점검", f"{type(exc).__name__}: {str(exc)[:200]}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
