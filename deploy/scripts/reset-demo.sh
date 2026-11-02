#!/usr/bin/env bash
# 데모 시작 상태로 되돌립니다(T28, 스펙 §10, 감사 B19). 실제 대상만 다룹니다: 출발지 EKS, 온프렘 RKE2, GKE,
# 플랫폼 RKE2(EC2 1대, 컨텍스트 bravo-platform-rke2). Executor는 플랫폼의 executor-0이고 D-16 링크로 닿습니다.
# 노트북 Executor · 임시 링크(127.0.0.1:8085, 18081/28081)는 쓰지 않습니다.
# 같은 명령을 여러 번 돌려도 끝 상태가 같습니다(멱등). 대상마다 한 일, 이미 된 일, 못 한 이유를 한 줄씩 냅니다.
#
#   deploy/scripts/reset-demo.sh --dry-run [--plan PLAN_ID]   읽기만 함. 실제 실행과 같은 읽기를 하고 바꿀 것만 PLAN
#   deploy/scripts/reset-demo.sh --plan PLAN_ID                실제로 되돌림(그 데모 계획의 리허설 · 검증 NS만 지움)
#   deploy/scripts/reset-demo.sh --plan PLAN_ID --only front,writes
#
# 단계(이 순서로 돎). 시작할 때 executor-0의 repl_setup status를 한 번 읽어 컷오버 뒤인지 봅니다(guard).
# 컷오버 뒤(목적지 writer LOGIN 또는 역방향 구독 켜짐)면 front · data · writes는 FAIL로 멈춥니다. 되돌리기는 콘솔에서
# 사람이 승인하고 시작합니다(D-18). 스크립트는 failback을 시작하지 않습니다.
#   rehearsal  그 계획(--plan)의 리허설 · 검증 네임스페이스만 삭제: 이름 rehearsal-<계획 ID 앞 8자>-* · verify-<같음>-*이고
#              라벨 bravo.dev/trial-id가 있는 것(platform/rehearsal cluster.Names). 다른 계획의 것은 OTHER로 보이기만 함
#   approvals  플랫폼 DB에서 그 계획의 열린 승인 · 승인 요청을 셈(읽기 전용). 기다리지도 지우지도 않음(append-only).
#              승인은 계획 revision에 묶여 새 계획에 쓰이지 않으므로 다음 데모를 막지 않음. 결과는 ALREADY 또는 INFO
#   front      고객 앱 대표 이름을 출발지로. executor-0 GET /v1/front가 src가 아니면 POST /v1/front/shift {"target_env":"src"}
#   data       PgBouncer 활성 대상이 target-src.ini가 아니면 rollout restart(init 컨테이너가 복사).
#              복제가 컷오버 전 기준과 다르면 executor-0에서 repl_setup rearm(구독 · 슬롯 정리, 목적지 표 비움, 다시 복사)
#   writes     모든 환경의 쓰기 차단 끔. executor-0 안에서 chat-admin GET /admin/writes, blocked면 PUT {"blocked": false}
#
# 입력(환경 변수, 비밀 없음)
#   RESET_PLAN_ID            --plan과 같음
#   RESET_PLATFORM_CONTEXT   플랫폼 컨텍스트(기본 bravo-platform-rke2). RESET_PLATFORM_NS(기본 bravo-control)
#   RESET_SRC_CONTEXT · RESET_ONPREM_CONTEXT  기본 bravo-source · bravo-onprem. GKE는 deploy/scripts/demo-lib.sh(kc gke)
#   RESET_FRONT_HOST         고객 앱 대표 이름(기본 b-free-chat.mangowhois.ai). RESET_SRC_ENV_ID(기본 aws)
#
# 비밀: chat-admin 토큰과 DB 비밀번호는 executor-0에 이미 전달된 파일(BRAVO_SECRETS_DIR)에서 그 Pod 안에서만 읽습니다.
# 이 스크립트는 비밀 값을 출력하거나 파일에 쓰지 않습니다.
#
# 종료 코드: 0 모든 대상이 처음 상태(또는 dry-run 계획만 있음), 1 실패한 대상 있음, 2 사용법,
#            3 실패는 없지만 연결 안 된 대상 있음. INFO · OTHER는 종료 코드에 영향 없음
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export BRAVO_PLATFORM_CONTEXT="${RESET_PLATFORM_CONTEXT:-${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}}"
export BRAVO_SOURCE_CONTEXT="${RESET_SRC_CONTEXT:-${BRAVO_SOURCE_CONTEXT:-bravo-source}}"
export BRAVO_ONPREM_CONTEXT="${RESET_ONPREM_CONTEXT:-${BRAVO_ONPREM_CONTEXT:-bravo-onprem}}"
# shellcheck source=deploy/scripts/demo-lib.sh
source "$SCRIPT_DIR/demo-lib.sh"

DRY=0
ONLY=""
PLAN="${RESET_PLAN_ID-}"
usage() { sed -n '2,36p' "${BASH_SOURCE[0]}"; }
while (($#)); do
  case "$1" in
    --dry-run) DRY=1 ;;
    --only) ONLY="${2-}"; shift ;;
    --only=*) ONLY="${1#--only=}" ;;
    --plan) PLAN="${2-}"; shift ;;
    --plan=*) PLAN="${1#--plan=}" ;;
    -h | --help) usage; exit 0 ;;
    *) echo "reset-demo.sh: 모르는 인자 $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
for s in ${ONLY//,/ }; do
  case "$s" in rehearsal | approvals | front | data | writes) ;;
    *) echo "reset-demo.sh: 모르는 단계 $s" >&2; exit 2 ;;
  esac
done
if [[ -n $PLAN && ! $PLAN =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  echo "reset-demo.sh: --plan은 계획 UUID(콘솔 #/plan 또는 GET /v1/plans)" >&2; exit 2
fi
want() { [[ -z $ONLY || ",$ONLY," == *",$1,"* ]]; }
if ((!DRY)) && [[ -z $PLAN ]] && { want rehearsal || want approvals; }; then
  echo "reset-demo.sh: 실제 실행에는 --plan PLAN_ID가 필요합니다(지울 네임스페이스와 볼 승인을 그 데모 계획으로 한정, B19)." >&2
  echo "              계획 없이 front · data · writes만 하려면 --only front,data,writes" >&2
  exit 2
fi
for bin in kubectl curl jq; do
  command -v "$bin" >/dev/null || { echo "reset-demo.sh: $bin 없음" >&2; exit 2; }
done

PLATFORM_NS="${RESET_PLATFORM_NS:-bravo-control}"
FRONT="${RESET_FRONT_HOST:-b-free-chat.mangowhois.ai}"
SRC_ENV_ID="${RESET_SRC_ENV_ID:-aws}"
SHORT="${PLAN//-/}"; SHORT="${SHORT:0:8}"
RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/reset-demo.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
T0=$SECONDS

declare -i N_DONE=0 N_ALREADY=0 N_PLAN=0 N_NC=0 N_FAIL=0 N_INFO=0
report() {
  local step=$1 target=$2 st=$3
  shift 3
  printf '[reset-demo] %-9s %-26s %-13s %s\n' "$step" "$target" "$st" "$*"
  case "$st" in
    DONE) N_DONE+=1 ;; ALREADY) N_ALREADY+=1 ;; PLAN) N_PLAN+=1 ;;
    NOT_CONNECTED) N_NC+=1 ;; FAIL) N_FAIL+=1 ;; INFO | OTHER) N_INFO+=1 ;;
  esac
}

# Executor-0 on the platform: commands run inside the pod (in-cluster topology and secret files, D-16 links).
# No --request-timeout here: rearm and a front shift run for minutes inside one exec stream.
ex() { kubectl --context "$BRAVO_PLATFORM_CONTEXT" -n "$PLATFORM_NS" exec executor-0 -c executor -- "$@" </dev/null; }
exi() { kubectl --context "$BRAVO_PLATFORM_CONTEXT" -n "$PLATFORM_NS" exec -i executor-0 -c executor -- "$@"; }
ex_ok() { kc platform -n "$PLATFORM_NS" exec executor-0 -c executor -- true </dev/null >/dev/null 2>&1; }

# ---- guard: 복제 상태(읽기). 컷오버 뒤면 front · data · writes를 하지 않음 ------------------------------------------
REPL_ST=""      # last status JSON line from repl_setup
POST_CUTOVER=0
repl_status() { ex python -m bravo_executor.repl_setup status 2>"$TMP/repl.err" | grep '^{' | jq -c 'select(.event == "status")' | tail -n 1; }
# Pre-cutover baseline (same as repl_setup.rearm): forward subscription flowing into the destination, reverse
# subscription ready but disabled on the source, only the source writer can log in, attachment stores not frozen.
PRE_CUTOVER_JQ='(.dst.subscriptions.bravo_fwd_sub.enabled == true and .dst.subscriptions.bravo_fwd_sub.worker_pid != null)
  and (.src.subscriptions.bravo_rev_sub.exists == true and .src.subscriptions.bravo_rev_sub.enabled == false)
  and .src.writer.login == true and .dst.writer.login == false
  and .src.attachment_store.frozen == false and .dst.attachment_store.frozen == false'
guard() {
  if ! ex_ok; then GUARD="NOT_CONNECTED executor-0에 exec 못 함($BRAVO_PLATFORM_CONTEXT $PLATFORM_NS)"; return; fi
  REPL_ST="$(repl_status || true)"
  if [[ -z $REPL_ST ]]; then GUARD="FAIL repl_setup status를 읽지 못함: $(tail -n 1 "$TMP/repl.err" | head -c 200)"; return; fi
  if jq -e '.dst.writer.login == true or .src.subscriptions.bravo_rev_sub.enabled == true' >/dev/null <<<"$REPL_ST"; then
    POST_CUTOVER=1
    GUARD="FAIL 컷오버 뒤 상태(목적지 writer LOGIN 또는 역방향 구독 켜짐). 되돌리기는 콘솔에서 사람이 승인 · 시작(D-18)"
    return
  fi
  GUARD="OK"
}

# ---- rehearsal: 그 계획의 리허설 · 검증 네임스페이스 --------------------------------------------------------------
step_rehearsal() {
  local a names mine others ns err
  for a in source onprem gke; do
    if ! names="$(kc "$a" get ns -l bravo.dev/trial-id -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>"$TMP/err")"; then
      report rehearsal "$a" NOT_CONNECTED "네임스페이스 목록 실패: $(head -c 160 "$TMP/err")"; continue
    fi
    mine=""; others=""
    while read -r ns; do
      [[ -n $ns ]] || continue
      if [[ -n $SHORT && ( $ns == rehearsal-$SHORT-* || $ns == verify-$SHORT-* ) ]]; then mine+="$ns "; else others+="$ns "; fi
    done <<<"$names"
    [[ -n $others ]] && report rehearsal "$a" OTHER "다른 계획 · 시험의 Trial 네임스페이스(지우지 않음): ${others% }"
    if [[ -z $PLAN ]]; then report rehearsal "$a" INFO "--plan 없음: 지울 대상을 정하지 않음"; continue; fi
    if [[ -z $mine ]]; then report rehearsal "$a" ALREADY "계획 ${SHORT}의 리허설 · 검증 네임스페이스 0개"; continue; fi
    if ((DRY)); then report rehearsal "$a" PLAN "삭제할 것: ${mine% }"; continue; fi
    # shellcheck disable=SC2086
    if kc "$a" delete ns ${mine% } --wait=true --timeout=180s >/dev/null 2>"$TMP/err"; then
      report rehearsal "$a" DONE "삭제함: ${mine% }"
    else
      report rehearsal "$a" FAIL "삭제 실패: $(head -c 200 "$TMP/err")"
    fi
  done
}

# ---- approvals: 그 계획의 열린 승인 · 요청(읽기만, 기다리지 않음) --------------------------------------------------
step_approvals() {
  local pod out n first last where
  pod="$(kc platform -n "$PLATFORM_NS" get pod -l "cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary" -o name 2>/dev/null | head -n 1)"
  if [[ -z $pod ]]; then report approvals platform NOT_CONNECTED "플랫폼 DB 주 Pod 없음($BRAVO_PLATFORM_CONTEXT $PLATFORM_NS)"; return; fi
  where="${PLAN:+AND plan_id = '$PLAN'}"
  if ! out="$(kc platform -n "$PLATFORM_NS" exec "${pod#pod/}" -c postgres -- psql -X -q -At -F ' ' -d bravo \
    -c 'SET default_transaction_read_only = on' -c "WITH o AS (
      SELECT plan_id, expires_at AS t FROM approvals WHERE consumed_at IS NULL AND expires_at > now() $where
      UNION ALL
      SELECT p.plan_id, p.approval_request_expires_at FROM plans p
       WHERE p.approval_request_id IS NOT NULL AND p.approval_request_expires_at > now() ${PLAN:+AND p.plan_id = '$PLAN'}
         AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.approval_request_id = p.approval_request_id))
      SELECT count(*), count(DISTINCT plan_id), coalesce(to_char(max(t) AT TIME ZONE 'Asia/Seoul', 'MM-DD HH24:MI:SS'), '-') FROM o" 2>"$TMP/err")"; then
    report approvals platform FAIL "조회 실패: $(head -c 200 "$TMP/err")"; return
  fi
  read -r n first last <<<"$(tail -n 1 <<<"$out")"
  if [[ -z $PLAN ]]; then
    report approvals platform INFO "--plan 없음. 모든 계획의 열린 승인 · 요청 ${n}건(계획 ${first}개, 마지막 만료 $last KST). 기다리지 않음"
  elif [[ $n == 0 ]]; then
    report approvals "plan:$SHORT" ALREADY "열린 승인 · 승인 요청 0건"
  else
    report approvals "plan:$SHORT" INFO "열린 승인 · 요청 ${n}건(만료 $last KST). 계획 revision에 묶여 새 계획에 쓰이지 않음. 기다리지 · 지우지 않음"
  fi
}

# ---- front: 대표 이름을 출발지로(executor-0) ---------------------------------------------------------------------
FRONT_PY='import json, sys, time, urllib.request
base = "http://127.0.0.1:8081"
def call(path, body=None, key=None):
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **({"Idempotency-Key": key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
mode = sys.argv[1]
code, st = call("/v1/front")
if mode == "get" or code != 200:
    print(json.dumps({"code": code, "state": st})); sys.exit(0)
code, acc = call("/v1/front/shift", {"target_env": "src"}, sys.argv[2])
if code != 202:
    print(json.dumps({"code": code, "shift": acc})); sys.exit(0)
run = acc["run_id"]; deadline = time.time() + 240; status = "RUNNING"
while status == "RUNNING" and time.time() < deadline:
    time.sleep(2)
    c, r = call("/v1/runs/" + run); status = r.get("status", "RUNNING")
c, after = call("/v1/front")
print(json.dumps({"code": 202, "run_id": run, "status": status, "state": after}))'

probe_front() { # "<HTTP 코드> <env.id 또는 ->"
  local code id="-"
  code="$(curl -sS -o "$TMP/front.json" -w '%{http_code}' --max-time 10 "https://$FRONT/api/config" 2>/dev/null)" || code="${code:-000}"
  [[ $code == 200 ]] && id="$(jq -r '.env.id // "-"' "$TMP/front.json" 2>/dev/null || echo -)"
  printf '%s %s' "${code:-000}" "$id"
}

step_front() {
  local out active mode pid pcode code id
  if [[ $GUARD != OK ]]; then report front "$FRONT" "${GUARD%% *}" "${GUARD#* }"; return; fi
  out="$(exi python - get <<<"$FRONT_PY" 2>"$TMP/err")" || { report front "$FRONT" FAIL "executor-0 GET /v1/front 실패: $(head -c 200 "$TMP/err")"; return; }
  if [[ $(jq -r .code <<<"$out") != 200 ]]; then report front "$FRONT" FAIL "GET /v1/front: $(head -c 200 <<<"$out")"; return; fi
  # Judge by the Executor's own probe from the platform (GET /v1/front), not by the operator laptop's network.
  active="$(jq -r '.state.active_env // "-"' <<<"$out")"; mode="$(jq -r '.state.mode // "-"' <<<"$out")"
  pcode="$(jq -r '.state.probe.status // "-"' <<<"$out")"; pid="$(jq -r '.state.probe.env_id // "-"' <<<"$out")"
  if [[ $active == src && $pid == "$SRC_ENV_ID" ]]; then
    report front "$FRONT" ALREADY "mode=$mode active_env=src, Executor 탐침 HTTP $pcode env.id=$pid"; return
  fi
  if [[ $active == src ]]; then  # already pointed at the source: a shift cannot fix a failing source app
    report front "$FRONT" FAIL "이미 active_env=src인데 Executor 탐침 HTTP $pcode env.id=$pid: 출발지 앱 문제(전환하지 않음)"; return
  fi
  if ((DRY)); then
    report front "$FRONT" PLAN "지금 mode=$mode active_env=$active, Executor 탐침 HTTP $pcode env.id=$pid. executor-0 POST /v1/front/shift {\"target_env\":\"src\"}"
    return
  fi
  out="$(exi python - shift "reset-demo-front-$RUN_TAG" <<<"$FRONT_PY" 2>"$TMP/err")" || { report front "$FRONT" FAIL "전환 요청 실패: $(head -c 200 "$TMP/err")"; return; }
  pid="$(jq -r '.state.probe.env_id // "-"' <<<"$out")"
  read -r code id <<<"$(probe_front)"
  if [[ $(jq -r '.state.active_env // ""' <<<"$out") == src && $pid == "$SRC_ENV_ID" ]]; then
    report front "$FRONT" DONE "run $(jq -r .run_id <<<"$out") status=$(jq -r .status <<<"$out"). Executor 탐침 env.id=$pid, 노트북 HTTP $code env.id=$id"
  else
    report front "$FRONT" FAIL "$(jq -c '{code, run_id, status, shift}' <<<"$out" | head -c 200). Executor 탐침 env.id=$pid, 노트북 HTTP $code env.id=$id"
  fi
}

# ---- data: PgBouncer 대상과 복제 ---------------------------------------------------------------------------------
pool_reset() { # name alias namespace selector
  local name=$1 a=$2 ns=$3 sel=$4 pod dep x y
  pod="$(kc "$a" -n "$ns" get pod -l "$sel" --field-selector=status.phase=Running -o name 2>/dev/null | head -n 1)"
  if [[ -z $pod ]]; then report data "pool:$name" NOT_CONNECTED "$a $ns에 $sel Pod 없음"; return; fi
  x="$(kc "$a" -n "$ns" exec "${pod#pod/}" -c pgbouncer -- cat /run/pgbouncer/active.ini 2>/dev/null)" || x="(읽기 실패)"
  y="$(kc "$a" -n "$ns" exec "${pod#pod/}" -c pgbouncer -- cat /etc/pgbouncer/targets/target-src.ini 2>/dev/null)" || y="(읽기 실패)"
  if [[ $y != "(읽기 실패)" && $x == "$y" ]]; then report data "pool:$name" ALREADY "활성 대상 = target-src.ini"; return; fi
  dep="$(kc "$a" -n "$ns" get deploy -l "$sel" -o name 2>/dev/null | head -n 1)"; dep="${dep:-deployment/chat-pgbouncer}"
  if ((DRY)); then report data "pool:$name" PLAN "활성 대상이 target-src.ini와 다름. $dep rollout restart"; return; fi
  if ! kc "$a" -n "$ns" rollout restart "$dep" >/dev/null 2>"$TMP/err" \
    || ! kc "$a" -n "$ns" rollout status "$dep" --timeout=180s >/dev/null 2>>"$TMP/err"; then
    report data "pool:$name" FAIL "rollout 실패: $(head -c 200 "$TMP/err")"; return
  fi
  pod="$(kc "$a" -n "$ns" get pod -l "$sel" --field-selector=status.phase=Running -o name 2>/dev/null | head -n 1)"
  x="$(kc "$a" -n "$ns" exec "${pod#pod/}" -c pgbouncer -- cat /run/pgbouncer/active.ini 2>/dev/null)" || x="(읽기 실패)"
  if [[ $x == "$y" ]]; then report data "pool:$name" DONE "재시작 뒤 활성 대상 = target-src.ini"
  else report data "pool:$name" FAIL "재시작 뒤에도 활성 대상이 target-src.ini와 다름"; fi
}

step_data() {
  local topo name ctx ns wl a st
  if [[ $GUARD != OK ]]; then report data repl "${GUARD%% *}" "${GUARD#* }"; return; fi
  topo="$(ex cat /etc/bravo/topology/topology.json 2>/dev/null)" || { report data topology NOT_CONNECTED "executor-0의 토폴로지를 읽지 못함"; return; }
  while IFS=$'\t' read -r name ctx ns wl; do
    case $ctx in "$BRAVO_SOURCE_CONTEXT") a=source ;; "$BRAVO_ONPREM_CONTEXT") a=onprem ;; *) report data "pool:$name" NOT_CONNECTED "모르는 컨텍스트 $ctx"; continue ;; esac
    pool_reset "$name" "$a" "$ns" "${wl#selector:}"
  done < <(jq -r '.pools[]? | [.name, .switch.context, .switch.namespace, .switch.workload] | @tsv' <<<"$topo")
  if jq -e "$PRE_CUTOVER_JQ" >/dev/null <<<"$REPL_ST"; then
    report data repl ALREADY "정방향 흐름, 역방향 준비(꺼짐), 쓰기는 출발지만, 첨부 저장소 얼지 않음"; return
  fi
  if ((DRY)); then report data repl PLAN "컷오버 전 기준과 다름. executor-0 repl_setup rearm(구독 · 슬롯 정리, 목적지 표 비움, 다시 복사)"; return; fi
  ex python -m bravo_executor.repl_setup rearm >"$TMP/rearm.out" 2>"$TMP/err" || true
  st="$(repl_status || true)"
  if [[ -n $st ]] && jq -e "$PRE_CUTOVER_JQ" >/dev/null <<<"$st"; then
    report data repl DONE "rearm 뒤 다시 읽은 상태가 컷오버 전 기준과 같음"
  else
    report data repl FAIL "rearm 뒤에도 기준과 다름: $(grep '^{' "$TMP/rearm.out" | jq -r 'select(.event == "failed") | "\(.error): \(.detail)"' 2>/dev/null | tail -n 1 | head -c 200)"
  fi
}

# ---- writes: 쓰기 차단 끄기(executor-0 안에서, 토큰은 그 Pod의 전달된 파일) --------------------------------------------
WRITES_PY='import json, os, sys, urllib.request
mode, topo = sys.argv[1], json.load(open(os.environ["BRAVO_TOPOLOGY_FILE"]))
root = os.environ["BRAVO_SECRETS_DIR"]
def call(url, tok, method="GET", body=None):
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")
for e in topo.get("entries", []):
    out = {"name": e["name"], "url": e["url"]}
    try:
        tok = open(os.path.join(root, e["secret"].replace("/", "__"), "token")).read().strip()
        before = call(e["url"] + "/admin/writes", tok)
        out["before"] = before
        if before.get("blocked") is True and mode == "apply":
            call(e["url"] + "/admin/writes", tok, "PUT", {"blocked": False})
            out["after"] = call(e["url"] + "/admin/writes", tok)
        tok = ""
    except Exception as exc:
        out["error"] = type(exc).__name__ + ": " + str(exc)[:160]
    print(json.dumps(out))'

step_writes() {
  local out line name
  if [[ $GUARD != OK ]]; then report writes all "${GUARD%% *}" "${GUARD#* }"; return; fi
  out="$(exi python - "$( ((DRY)) && echo read || echo apply)" <<<"$WRITES_PY" 2>"$TMP/err")" \
    || { report writes all FAIL "executor-0에서 실행 실패: $(head -c 200 "$TMP/err")"; return; }
  while read -r line; do
    [[ -n $line ]] || continue
    name="entry:$(jq -r .name <<<"$line")"
    if jq -e .error >/dev/null <<<"$line"; then report writes "$name" FAIL "$(jq -r '"\(.url): \(.error)"' <<<"$line")"
    elif jq -e '.before.blocked == false' >/dev/null <<<"$line"; then
      report writes "$name" ALREADY "blocked=false since=$(jq -r '.before.since // "-"' <<<"$line")"
    elif ((DRY)); then report writes "$name" PLAN "blocked=true. PUT {\"blocked\": false} 뒤 다시 읽음"
    elif jq -e '.after.blocked == false' >/dev/null <<<"$line"; then report writes "$name" DONE "blocked true에서 false. 다시 읽음 false"
    else report writes "$name" FAIL "PUT 뒤 다시 읽어도 blocked"; fi
  done <<<"$out"
}

# ---- 실행 --------------------------------------------------------------------------------------------------------
printf '[reset-demo] 시작 %s, 모드 %s, 계획 %s, 단계 %s, 플랫폼 %s\n' "$(kst_now)" "$( ((DRY)) && echo dry-run || echo 실행)" \
  "${PLAN:-없음}" "${ONLY:-전체}" "$BRAVO_PLATFORM_CONTEXT"
GUARD="OK"
if want front || want data || want writes; then guard; fi
# `want && step` 꼴: errexit가 함수 안에 걸리지 않아 한 대상이 실패해도 다음으로 넘어감. 실패는 함수 안에서 FAIL로 셈.
want rehearsal && step_rehearsal
want approvals && step_approvals
want front && step_front
want data && step_data
want writes && step_writes

printf '[reset-demo] 요약: DONE %d, ALREADY %d, PLAN %d, NOT_CONNECTED %d, FAIL %d, INFO/OTHER %d. 걸린 시간 %ds\n' \
  "$N_DONE" "$N_ALREADY" "$N_PLAN" "$N_NC" "$N_FAIL" "$N_INFO" "$((SECONDS - T0))"
if ((N_FAIL)); then exit 1; fi
if ((N_NC)); then exit 3; fi
exit 0
