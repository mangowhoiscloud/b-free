#!/usr/bin/env bash
# 데모 시작 상태로 되돌립니다(T28, 스펙 §10). 실제 대상만 다룹니다: 출발지 EKS, 온프렘 K3s, GKE, 플랫폼 EKS.
# 노트북 Compose 파일럿은 쓰지 않습니다(deploy/CLOUD-E2E-CONTRACT.md 6.1). 예외는 키 보관소 platform-bao 하나입니다.
# 같은 명령을 여러 번 돌려도 끝 상태가 같습니다(멱등). 대상마다 한 일, 이미 된 일, 못 한 이유를 한 줄씩 냅니다.
# 일어나지 않은 결과는 내지 않습니다. 닿지 않는 대상은 NOT_CONNECTED와 이유입니다.
#
#   deploy/scripts/reset-demo.sh --dry-run             읽기만 함(kubectl get, 공개 HTTP GET). 할 일을 PLAN으로 냄
#   deploy/scripts/reset-demo.sh                       실제로 되돌림
#   deploy/scripts/reset-demo.sh --only front,writes   일부 단계만(rehearsal, approvals, front, data, writes)
#
# 단계(이 순서로 돎)
#   rehearsal  리허설 · 검증 네임스페이스 삭제. 라벨 bravo.dev/trial-id 또는 bravo.io/tier=rehearsal|verify
#   approvals  플랫폼 DB에 열린 승인 · 승인 요청이 0건인지 확인. 승인 기록은 지우지 않음(감사 기록, append-only).
#              열린 것이 있으면 만료까지 기다림(승인은 계획 · revision · 게이트 판정에 묶여 새 계획에 쓰이지 않음)
#   front      고객 앱 대표 이름(b-free-chat)을 출발지로. Executor POST /v1/front/shift {"target_env":"src"}
#   data       PgBouncer 활성 대상을 target-src.ini로(rollout restart, init 컨테이너가 복사),
#              복제를 컷오버 전 상태로(Executor repl_setup rearm: 구독 · 슬롯 정리, 목적지 표 비움, 다시 복사)
#   writes     모든 환경의 쓰기 차단 끔. chat-admin PUT /admin/writes {"blocked": false}
#
# 입력(환경 변수. 비밀 없음)
#   기본값은 deploy/cloud/cloud.env(클라우드 E2E 설정, 비밀 없음)에서 읽고, 아래 RESET_* 가 앞섭니다.
#   RESET_SRC_CONTEXT       출발지 EKS 컨텍스트(기본 cloud.env SRC_CONTEXT, 없으면 bravo-source)
#   RESET_ONPREM_CONTEXT    온프렘 K3s 컨텍스트(기본 cloud.env DST_CONTEXT)
#   RESET_GKE_CONTEXT       GKE 컨텍스트(기본 없음)
#   RESET_PLATFORM_CONTEXT  플랫폼 컨텍스트(기본 bravo-platform = 결정 전 EKS 판). 승인 확인에 씀.
#                           플랫폼은 EC2 1대 + RKE2로 바뀜(ADR 0010). RKE2 컨텍스트가 생기면 이 값으로 줌
#   RESET_PLATFORM_NS       플랫폼 네임스페이스(기본 bravo-control, docs/architecture/10-cluster-overview.md)
#   RESET_PLATFORM_DB       플랫폼 DB CNPG Cluster 이름(기본 platform-db). RESET_PLATFORM_DB_NAME(기본 bravo)
#   RESET_EXECUTOR_URL      Executor 주소(기본 cloud.env LINK_BIND:EXECUTOR_PORT, deploy/cloud/executor.sh)
#   RESET_TOPOLOGY_FILES    Executor 토폴로지 JSON, 목적지마다 하나, 콜론으로 구분(기본 $BRAVO_TOPOLOGY_FILE,
#                           없으면 deploy/cloud/render_topology.sh가 쓴 deploy/pilot/runs/cloud-state/topology.json).
#                           형식은 platform/executor/src/bravo_executor/topology.py. data · writes 단계가 씀
#   RESET_FRONT_HOST        고객 앱 대표 이름(기본: infra/envs/edge 출력 front_hostname)
#   RESET_SRC_ENV_ID        출발지 앱의 GET /api/config env.id(기본 cloud.env SRC_APP_ENV_ID, 없으면 aws)
#   RESET_APPROVAL_WAIT_S   열린 승인의 만료를 기다리는 상한(기본 900 = BRAVO_APPROVAL_TTL_S 기본값)
#
# 비밀: 토큰과 비밀번호는 플랫폼 OpenBao에서 executor AppRole로, 자식 프로세스 안에서만 읽습니다
# (deploy/pilot/scripts/executor-bao.sh with-executor, D-26). 이 스크립트는 비밀 값을 출력하거나 파일에 쓰지 않습니다.
# executor-bao.sh는 BAO_ADDR(기본 https://127.0.0.1:8200)로 닿습니다. 금고가 플랫폼 EKS(ns openbao)에 있으면
# 먼저 port-forward 합니다. 닿지 않으면 data · writes는 NOT_CONNECTED입니다.
#
# 종료 코드: 0 모든 대상이 처음 상태(또는 dry-run 계획만 있음), 1 실패한 대상 있음, 2 사용법,
#            3 실패는 없지만 연결 안 된 대상 있음
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
BAO_SH="$REPO/deploy/pilot/scripts/executor-bao.sh"
EXECUTOR_PROJECT="$REPO/platform/executor"

DRY=0
ONLY=""
usage() { sed -n '2,40p' "${BASH_SOURCE[0]}"; }
while (($#)); do
  case "$1" in
    --dry-run) DRY=1 ;;
    --only) ONLY="${2-}"; shift ;;
    --only=*) ONLY="${1#--only=}" ;;
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

for bin in kubectl curl jq; do
  command -v "$bin" >/dev/null || { echo "reset-demo.sh: $bin 없음" >&2; exit 2; }
done

# 기본값은 클라우드 E2E 설정(deploy/cloud/cloud.env, 비밀 없음, Git 제외)에서 읽음. RESET_* 가 앞섬.
CLOUD_ENV="$REPO/deploy/cloud/cloud.env"
cloud_get() {
  [[ -f $CLOUD_ENV ]] || return 0
  local v
  v="$(sed -n "s/^$1=//p" "$CLOUD_ENV" | tail -n 1)"
  v="${v%$'\r'}"; v="${v#\"}"; v="${v%\"}"
  printf '%s' "$v"
}
CLOUD_EXECUTOR_PORT="$(cloud_get EXECUTOR_PORT)"
CLOUD_BIND="$(cloud_get LINK_BIND)"
CLOUD_STATE_TOPOLOGY="$REPO/deploy/pilot/runs/cloud-state/topology.json" # deploy/cloud/render_topology.sh가 씀

SRC_CTX="${RESET_SRC_CONTEXT-$(cloud_get SRC_CONTEXT)}"
SRC_CTX="${SRC_CTX:-bravo-source}"
ONPREM_CTX="${RESET_ONPREM_CONTEXT-$(cloud_get DST_CONTEXT)}"
GKE_CTX="${RESET_GKE_CONTEXT-}"
PLATFORM_CTX="${RESET_PLATFORM_CONTEXT-bravo-platform}"
PLATFORM_NS="${RESET_PLATFORM_NS:-bravo-control}"
PLATFORM_DB="${RESET_PLATFORM_DB:-platform-db}"
PLATFORM_DB_NAME="${RESET_PLATFORM_DB_NAME:-bravo}"
EXECUTOR_URL="${RESET_EXECUTOR_URL-}"
if [[ -z $EXECUTOR_URL && -n $CLOUD_EXECUTOR_PORT ]]; then
  EXECUTOR_URL="http://${CLOUD_BIND:-127.0.0.1}:$CLOUD_EXECUTOR_PORT" # deploy/cloud/executor.sh의 주소
fi
EXECUTOR_URL="${EXECUTOR_URL%/}"
# 경로에 공백이 있을 수 있어 콜론(:)으로 나눔
TOPOLOGY_FILES=()
IFS=':' read -r -a TOPOLOGY_FILES <<<"${RESET_TOPOLOGY_FILES:-${BRAVO_TOPOLOGY_FILE-}}"
if ((${#TOPOLOGY_FILES[@]} == 0)) && [[ -r $CLOUD_STATE_TOPOLOGY ]]; then TOPOLOGY_FILES=("$CLOUD_STATE_TOPOLOGY"); fi
SRC_ENV_ID="${RESET_SRC_ENV_ID:-$(cloud_get SRC_APP_ENV_ID)}"
SRC_ENV_ID="${SRC_ENV_ID:-aws}"
APPROVAL_WAIT_S="${RESET_APPROVAL_WAIT_S:-900}"
RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/reset-demo.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

declare -i N_DONE=0 N_ALREADY=0 N_PLAN=0 N_NC=0 N_FAIL=0
report() {
  local step=$1 target=$2 st=$3
  shift 3
  printf '[reset-demo] %-9s %-24s %-13s %s\n' "$step" "$target" "$st" "$*"
  case "$st" in
    DONE) N_DONE+=1 ;; ALREADY) N_ALREADY+=1 ;; PLAN) N_PLAN+=1 ;;
    NOT_CONNECTED) N_NC+=1 ;; FAIL) N_FAIL+=1 ;;
  esac
}
want() { [[ -z $ONLY || ",$ONLY," == *",$1,"* ]]; }

k() { local ctx=$1; shift; kubectl --context "$ctx" --request-timeout=15s "$@"; }

# 컨텍스트가 있고 API가 답하는지. 아니면 REASON에 이유를 둠.
REASON=""
ctx_ok() {
  local ctx=$1
  if [[ -z $ctx ]]; then REASON="컨텍스트 미설정"; return 1; fi
  if ! kubectl config get-contexts -o name 2>/dev/null | grep -qxF "$ctx"; then
    REASON="kubeconfig에 컨텍스트 $ctx 없음"; return 1
  fi
  if ! k "$ctx" get --raw /readyz >/dev/null 2>&1; then REASON="API 응답 없음($ctx)"; return 1; fi
  return 0
}

env_ctx() {
  case "$1" in
    source-aws) printf '%s' "$SRC_CTX" ;;
    target-onprem) printf '%s' "$ONPREM_CTX" ;;
    target-gcp) printf '%s' "$GKE_CTX" ;;
  esac
}
ENVS=(source-aws target-onprem target-gcp)

# ---- rehearsal: 리허설 · 검증 네임스페이스 ----------------------------------------------------------
step_rehearsal() {
  local env ctx names a b
  for env in "${ENVS[@]}"; do
    ctx="$(env_ctx "$env")"
    if ! ctx_ok "$ctx"; then report rehearsal "$env" NOT_CONNECTED "$REASON"; continue; fi
    if ! a="$(k "$ctx" get ns -l bravo.dev/trial-id -o name 2>"$TMP/err")" \
      || ! b="$(k "$ctx" get ns -l 'bravo.io/tier in (rehearsal,verify)' -o name 2>>"$TMP/err")"; then
      report rehearsal "$env" FAIL "네임스페이스 목록 실패: $(head -c 200 "$TMP/err")"; continue
    fi
    names="$(printf '%s\n%s\n' "$a" "$b" | sed '/^$/d' | sort -u | tr '\n' ' ')"
    names="${names% }"
    if [[ -z $names ]]; then report rehearsal "$env" ALREADY "리허설 · 검증 네임스페이스 0개"; continue; fi
    if ((DRY)); then report rehearsal "$env" PLAN "삭제할 것: $names"; continue; fi
    # shellcheck disable=SC2086
    if k "$ctx" delete $names --wait=true --timeout=180s >/dev/null 2>"$TMP/err"; then
      report rehearsal "$env" DONE "삭제함: $names"
    else
      report rehearsal "$env" FAIL "삭제 실패: $(head -c 200 "$TMP/err")"
    fi
  done
}

# ---- approvals: 플랫폼 DB의 열린 승인(읽기만) -----------------------------------------------------------
APPROVAL_SQL="WITH o AS (
  SELECT expires_at AS t FROM approvals WHERE consumed_at IS NULL AND expires_at > now()
  UNION ALL
  SELECT p.approval_request_expires_at FROM plans p
   WHERE p.approval_request_id IS NOT NULL AND p.approval_request_expires_at > now()
     AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.approval_request_id = p.approval_request_id))
SELECT count(*), coalesce(ceil(extract(epoch FROM max(t) - now()))::int, 0) FROM o"

open_approvals() { # 출력: "<열린 건수> <가장 늦은 만료까지 초>"
  k "$PLATFORM_CTX" -n "$PLATFORM_NS" exec "$1" -c postgres -- \
    psql -X -q -At -F ' ' -d "$PLATFORM_DB_NAME" \
    -c 'SET default_transaction_read_only = on' -c "$APPROVAL_SQL"
}

step_approvals() {
  local pod out n wait_s
  if ! ctx_ok "$PLATFORM_CTX"; then report approvals platform-eks NOT_CONNECTED "$REASON(플랫폼 EKS)"; return; fi
  pod="$(k "$PLATFORM_CTX" -n "$PLATFORM_NS" get pod \
    -l "cnpg.io/cluster=$PLATFORM_DB,cnpg.io/instanceRole=primary" -o name 2>/dev/null | head -n 1)"
  if [[ -z $pod ]]; then
    report approvals platform-eks NOT_CONNECTED "플랫폼 DB 주 Pod 없음($PLATFORM_NS/$PLATFORM_DB)"; return
  fi
  if ((DRY)); then
    report approvals platform-eks PLAN "열린 승인 · 요청을 읽기 전용 SELECT로 셈. 남으면 만료까지 기다림(최대 ${APPROVAL_WAIT_S}s). 지우지 않음"
    return
  fi
  if ! out="$(open_approvals "$pod" 2>"$TMP/err")"; then
    report approvals platform-eks FAIL "조회 실패: $(head -c 200 "$TMP/err")"; return
  fi
  read -r n wait_s <<<"$(tail -n 1 <<<"$out")"
  if [[ $n == 0 ]]; then report approvals platform-eks ALREADY "열린 승인 · 승인 요청 0건"; return; fi
  if ((wait_s > APPROVAL_WAIT_S)); then
    report approvals platform-eks FAIL "열린 승인 · 요청 ${n}건, 만료까지 ${wait_s}s(상한 ${APPROVAL_WAIT_S}s). 지우지 않음"
    return
  fi
  printf '[reset-demo] approvals 열린 승인 · 요청 %s건의 만료(%ss 뒤)를 기다립니다. 기록은 지우지 않습니다\n' "$n" "$wait_s"
  sleep $((wait_s + 2)) # 만료를 기다린 뒤 DB를 다시 읽어 판정함
  out="$(open_approvals "$pod" 2>"$TMP/err")" || { report approvals platform-eks FAIL "재조회 실패"; return; }
  read -r n wait_s <<<"$(tail -n 1 <<<"$out")"
  if [[ $n == 0 ]]; then
    report approvals platform-eks DONE "만료로 닫힘. 다시 읽은 열린 건수 0"
  else
    report approvals platform-eks FAIL "다시 읽어도 열린 건수 ${n}"
  fi
}

# ---- front: 고객 앱 대표 이름을 출발지로 ------------------------------------------------------------------
FRONT="${RESET_FRONT_HOST-}"
probe_front() { # 출력: "<HTTP 코드> <env.id 또는 ->"
  local code id="-"
  code="$(curl -sS -o "$TMP/front.json" -w '%{http_code}' --max-time 10 "https://$FRONT/api/config" 2>/dev/null)" || code="${code:-000}"
  if [[ $code == 200 ]]; then id="$(jq -r '.env.id // "-"' "$TMP/front.json" 2>/dev/null || echo -)"; fi
  printf '%s %s' "${code:-000}" "$id"
}

step_front() {
  local code id route body run_id status deadline
  if [[ -z $FRONT ]]; then
    FRONT="$(terraform -chdir="$REPO/infra/envs/edge" output -raw front_hostname 2>/dev/null || true)"
  fi
  if [[ -z $FRONT ]]; then report front front NOT_CONNECTED "대표 이름을 모름(RESET_FRONT_HOST, edge 출력 없음)"; return; fi
  read -r code id <<<"$(probe_front)"
  if [[ $code == 200 && $id == "$SRC_ENV_ID" ]]; then
    report front "$FRONT" ALREADY "GET /api/config env.id=$id(출발지)"; return
  fi
  if [[ -z $EXECUTOR_URL ]]; then
    report front "$FRONT" NOT_CONNECTED "지금 HTTP $code env.id=$id. Executor 주소 없음(RESET_EXECUTOR_URL)"; return
  fi
  # 길 확인: POST만 있는 경로에 OPTIONS를 보내면 405(경로 있음), 없으면 404. 아무것도 바꾸지 않음
  route="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X OPTIONS "$EXECUTOR_URL/v1/front/shift")" || true
  route="${route:-000}"
  if [[ $route == 000 ]]; then
    report front "$FRONT" NOT_CONNECTED "지금 HTTP $code env.id=$id. Executor $EXECUTOR_URL 응답 없음(deploy/cloud/executor.sh start)"; return
  fi
  if [[ $route == 404 ]]; then
    report front "$FRONT" NOT_CONNECTED "지금 HTTP $code env.id=$id. Executor에 /v1/front/shift 없음(HTTP 404)"; return
  fi
  if ((DRY)); then
    report front "$FRONT" PLAN "지금 HTTP $code env.id=$id. Executor POST /v1/front/shift {\"target_env\":\"src\"} 뒤 env.id=$SRC_ENV_ID 확인"
    return
  fi
  if ! body="$(curl -sS --max-time 20 -X POST "$EXECUTOR_URL/v1/front/shift" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: reset-demo-front-$RUN_TAG" \
    -d '{"target_env":"src"}' 2>"$TMP/err")"; then
    report front "$FRONT" FAIL "전환 요청 실패: $(head -c 200 "$TMP/err")"; return
  fi
  run_id="$(jq -r '.run_id // empty' <<<"$body" 2>/dev/null || true)"
  if [[ -z $run_id ]]; then report front "$FRONT" FAIL "전환 요청 거절: $(head -c 200 <<<"$body")"; return; fi
  deadline=$((SECONDS + 240))
  status=RUNNING
  while [[ $status == RUNNING ]] && ((SECONDS < deadline)); do
    sleep 2 # 실행 기록을 다시 읽는 간격. 결과는 아래에서 앞단을 다시 재서 판정함
    status="$(curl -sS --max-time 10 "$EXECUTOR_URL/v1/runs/$run_id" | jq -r '.status // "RUNNING"' 2>/dev/null || echo RUNNING)"
  done
  read -r code id <<<"$(probe_front)"
  if [[ $code == 200 && $id == "$SRC_ENV_ID" ]]; then
    report front "$FRONT" DONE "run $run_id status=$status. 다시 잰 env.id=$id"
  else
    report front "$FRONT" FAIL "run $run_id status=$status. 다시 잰 HTTP $code env.id=$id"
  fi
}

# ---- data: PgBouncer 대상과 복제 방향 -----------------------------------------------------------------
# 토폴로지가 없을 때도 클러스터마다 무엇이 빠졌는지 읽어서 알려 줌(계약 3 · 9절의 이름).
cluster_gaps() {
  local ctx=$1 gaps=() r
  k "$ctx" get ns chat -o name >/dev/null 2>&1 || { printf 'ns chat 없음'; return; }
  for r in clusters.postgresql.cnpg.io/chat-db deploy/chat-pgbouncer svc/chat-admin; do
    k "$ctx" -n chat get "$r" -o name >/dev/null 2>&1 || gaps+=("$r")
  done
  k "$ctx" -n cf-edge get deploy/cloudflared -o name >/dev/null 2>&1 || gaps+=("cf-edge/cloudflared")
  if ((${#gaps[@]})); then printf '없음: %s' "${gaps[*]}"; fi
}

POOLS_MISSING=0
pool_reset() {
  local name=$1 ctx=$2 ns=$3 wl=$4 a b
  if ! ctx_ok "$ctx"; then report data "pool:$name" NOT_CONNECTED "$REASON"; POOLS_MISSING=1; return; fi
  if ! k "$ctx" -n "$ns" get "$wl" -o name >/dev/null 2>&1; then
    report data "pool:$name" NOT_CONNECTED "$ctx $ns/$wl 없음"; POOLS_MISSING=1; return
  fi
  if ((DRY)); then
    report data "pool:$name" PLAN "active.ini를 target-src.ini와 비교. 다르면 $wl rollout restart(init 컨테이너가 target-src.ini 복사)"
    return
  fi
  a="$(k "$ctx" -n "$ns" exec "$wl" -- cat /run/pgbouncer/active.ini 2>/dev/null)" || a="(읽기 실패)"
  b="$(k "$ctx" -n "$ns" exec "$wl" -- cat /etc/pgbouncer/targets/target-src.ini 2>/dev/null)" || b="(읽기 실패)"
  if [[ $b != "(읽기 실패)" && $a == "$b" ]]; then report data "pool:$name" ALREADY "활성 대상 = target-src.ini"; return; fi
  if ! k "$ctx" -n "$ns" rollout restart "$wl" >/dev/null 2>"$TMP/err" \
    || ! k "$ctx" -n "$ns" rollout status "$wl" --timeout=180s >/dev/null 2>>"$TMP/err"; then
    report data "pool:$name" FAIL "rollout 실패: $(head -c 200 "$TMP/err")"; return
  fi
  a="$(k "$ctx" -n "$ns" exec "$wl" -- cat /run/pgbouncer/active.ini 2>/dev/null)" || a="(읽기 실패)"
  if [[ $a == "$b" ]]; then report data "pool:$name" DONE "재시작 뒤 활성 대상 = target-src.ini"
  else report data "pool:$name" FAIL "재시작 뒤에도 활성 대상이 target-src.ini와 다름"; fi
}

# 금고에 닿는지 실행 모드에서 한 번만 확인(값은 읽지 않음). executor-bao.sh는 BAO_ADDR(기본 https://127.0.0.1:8200)로 닿음.
# 금고가 플랫폼 EKS(ns openbao)에 있으면 먼저 port-forward 합니다.
BAO_STATE=""
bao_ok() {
  if [[ -z $BAO_STATE ]]; then
    if [[ -x $BAO_SH ]] && "$BAO_SH" status >/dev/null 2>&1; then BAO_STATE=ok; else BAO_STATE=down; fi
  fi
  [[ $BAO_STATE == ok ]]
}

repl() { # repl_setup 명령 하나. 비밀은 executor AppRole로 자식 안에서만 읽음
  BRAVO_TOPOLOGY_FILE="$1" "$BAO_SH" with-executor -- \
    uv run --quiet --project "$EXECUTOR_PROJECT" python -m bravo_executor.repl_setup "$2"
}

# 컷오버 전 상태: 목적지 정방향 구독이 켜져 흐르고, 출발지 역방향 구독은 꺼진 채 준비,
# 쓰기 역할은 출발지만 LOGIN, 첨부 저장소는 각자 자기 것이고 얼어 있지 않음(repl_setup.rearm과 같은 기준).
PRE_CUTOVER_JQ='select(.event == "status")
  | (.dst.subscriptions.bravo_fwd_sub.enabled == true and .dst.subscriptions.bravo_fwd_sub.worker_pid != null)
    and (.src.subscriptions.bravo_rev_sub.exists == true and .src.subscriptions.bravo_rev_sub.enabled == false)
    and .src.writer.login == true and .dst.writer.login == false
    and .src.attachment_store.active_store == $s and .src.attachment_store.frozen == false
    and .dst.attachment_store.active_store == $d and .dst.attachment_store.frozen == false'

status_line() { grep '^{' | jq -c 'select(.event == "status")' 2>/dev/null | tail -n 1; }
failed_line() { grep '^{' | jq -r 'select(.event == "failed") | "\(.error): \(.detail)"' 2>/dev/null | tail -n 1; }

repl_reset() {
  local f=$1 tag s d out st
  tag="repl:$(basename "$f" .json)"
  s="$(jq -r '.store_ids.src // "src"' "$f")"
  d="$(jq -r '.store_ids.dst // "dst"' "$f")"
  if ((POOLS_MISSING)); then
    report data "$tag" NOT_CONNECTED "PgBouncer가 없는 클러스터가 있어 복제를 다루지 않음(위 pool 줄)"; return
  fi
  if ((DRY)); then
    report data "$tag" PLAN "repl_setup status를 읽고 컷오버 전 상태가 아니면 rearm(구독 · 슬롯 정리, 목적지 표 비움, 다시 복사). 링크: $(jq -r '.label // "-"' "$f")"
    return
  fi
  bao_ok || { report data "$tag" NOT_CONNECTED "플랫폼 금고에 닿지 못함($BAO_SH status 실패)"; return; }
  out="$(repl "$f" status 2>"$TMP/err")" || true
  st="$(status_line <<<"$out")"
  if [[ -z $st ]]; then
    # 상태를 읽지 못하면 rearm(목적지 표를 비움)을 하지 않음. 불명확하면 멈춤(NFR-3과 같은 쪽)
    report data "$tag" FAIL "status를 읽지 못함: $(failed_line <<<"$out" | head -c 200)$(tail -n 1 "$TMP/err" | head -c 200)"
    return
  fi
  if jq -e --arg s "$s" --arg d "$d" "$PRE_CUTOVER_JQ" <<<"$st" >/dev/null; then
    report data "$tag" ALREADY "정방향 흐름, 역방향 준비(꺼짐), 쓰기는 출발지만"; return
  fi
  out="$(repl "$f" rearm 2>"$TMP/err")" || true
  st="$(status_line <<<"$out")"
  if [[ -z $st ]]; then
    # rearm은 구독이 켜져 있으면(컷오버 뒤, 되돌리기 전) 거부함. 되돌리기는 콘솔에서 사람이 시작함(D-18)
    report data "$tag" FAIL "rearm 거부 · 실패: $(failed_line <<<"$out" | head -c 240)"; return
  fi
  if jq -e --arg s "$s" --arg d "$d" "$PRE_CUTOVER_JQ" <<<"$st" >/dev/null; then
    report data "$tag" DONE "rearm 뒤 다시 읽은 상태가 컷오버 전 기준과 같음"
  else
    report data "$tag" FAIL "rearm은 끝났지만 다시 읽은 상태가 기준과 다름"
  fi
}

step_data() {
  local env ctx gaps f name pctx ns wl
  if ((${#TOPOLOGY_FILES[@]} == 0)); then
    for env in "${ENVS[@]}"; do
      ctx="$(env_ctx "$env")"
      if ! ctx_ok "$ctx"; then report data "$env" NOT_CONNECTED "$REASON"; continue; fi
      gaps="$(cluster_gaps "$ctx")"
      report data "$env" NOT_CONNECTED "토폴로지 파일 없음(RESET_TOPOLOGY_FILES, 계약 9절)${gaps:+. $gaps}"
    done
    return
  fi
  for f in "${TOPOLOGY_FILES[@]}"; do
    if [[ ! -r $f ]]; then report data "$(basename "$f")" NOT_CONNECTED "파일 없음: $f"; continue; fi
    POOLS_MISSING=0
    while IFS=$'\t' read -r name pctx ns wl; do
      pool_reset "$name" "$pctx" "$ns" "$wl"
    done < <(jq -r '.pools[]? | [.name, .switch.context, .switch.namespace, .switch.workload] | @tsv' "$f")
    repl_reset "$f"
  done
}

# ---- writes: 쓰기 차단 끄기 ---------------------------------------------------------------------------
# 토큰은 OpenBao secret/<entries[].secret>의 token. 자식 Python 안에서만 씀. 출력은 상태(blocked, since)뿐.
cat >"$TMP/write_block.py" <<'PY'
import json, os, ssl, sys, urllib.request

secret_path, base = sys.argv[1], sys.argv[2].rstrip("/")
addr = os.environ["BRAVO_BAO_ADDR"].rstrip("/")
ctx = ssl.create_default_context(cafile=os.environ.get("BRAVO_BAO_CACERT") or None)


def call(url, method="GET", body=None, headers=None, tls=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=15, context=tls) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


login = call(f"{addr}/v1/auth/approle/login", "POST",
             {"role_id": os.environ["BRAVO_BAO_ROLE_ID"], "secret_id": os.environ["BRAVO_BAO_SECRET_ID"]}, tls=ctx)
vtok = login["auth"]["client_token"]
try:
    token = call(f"{addr}/v1/secret/data/{secret_path}", headers={"X-Vault-Token": vtok}, tls=ctx)["data"]["data"]["token"]
    auth = {"Authorization": f"Bearer {token}"}
    before = call(f"{base}/admin/writes", headers=auth)
    if before.get("blocked") is True:
        after = call(f"{base}/admin/writes", "PUT", {"blocked": False}, headers=auth)
        again = call(f"{base}/admin/writes", headers=auth)
        print(json.dumps({"action": "done", "before": before, "after": after, "read_back": again}))
    else:
        print(json.dumps({"action": "already", "before": before}))
finally:
    try:
        call(f"{addr}/v1/auth/token/revoke-self", "POST", {}, headers={"X-Vault-Token": vtok}, tls=ctx)
    except Exception:  # noqa: BLE001 - token TTL ends it anyway
        pass
PY

step_writes() {
  local env ctx f name side url secret code out
  if ((${#TOPOLOGY_FILES[@]} == 0)); then
    for env in "${ENVS[@]}"; do
      ctx="$(env_ctx "$env")"
      if ! ctx_ok "$ctx"; then report writes "$env" NOT_CONNECTED "$REASON"; continue; fi
      if k "$ctx" -n chat get svc/chat-admin -o name >/dev/null 2>&1; then
        report writes "$env" NOT_CONNECTED "svc/chat-admin 있음. 토폴로지 entries(관리 주소 · 비밀 경로)가 없음"
      else
        report writes "$env" NOT_CONNECTED "chat/svc chat-admin 없음(앱 배포 전)"
      fi
    done
    return
  fi
  for f in "${TOPOLOGY_FILES[@]}"; do
    if [[ ! -r $f ]]; then report writes "$(basename "$f")" NOT_CONNECTED "파일 없음: $f"; continue; fi
    while IFS=$'\t' read -r name side url secret; do
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url/admin/writes" || true)"
      if [[ -z $code || $code == 000 ]]; then
        report writes "entry:$name" NOT_CONNECTED "$url 닿지 않음(임시 링크 port-forward가 없음)"; continue
      fi
      if ((DRY)); then
        report writes "entry:$name" PLAN "GET $url/admin/writes(토큰 OpenBao $secret), blocked면 PUT {\"blocked\": false} 뒤 다시 읽음"
        continue
      fi
      if ! bao_ok; then report writes "entry:$name" NOT_CONNECTED "플랫폼 금고에 닿지 못함($BAO_SH status 실패)"; continue; fi
      if out="$("$BAO_SH" with-executor -- python3 -I "$TMP/write_block.py" "$secret" "$url" 2>"$TMP/err")"; then
        case "$(jq -r '.action' <<<"$out")" in
          already) report writes "entry:$name" ALREADY "blocked=false since=$(jq -r '.before.since // "-"' <<<"$out")" ;;
          done)
            if [[ $(jq -r '.read_back.blocked' <<<"$out") == false ]]; then
              report writes "entry:$name" DONE "blocked true에서 false. 다시 읽음 false"
            else
              report writes "entry:$name" FAIL "PUT 뒤 다시 읽어도 blocked"
            fi ;;
          *) report writes "entry:$name" FAIL "알 수 없는 응답" ;;
        esac
      else
        report writes "entry:$name" FAIL "관리 API 또는 금고 실패: $(tail -n 1 "$TMP/err" | head -c 200)"
      fi
    done < <(jq -r '.entries[]? | [.name, .side, .url, .secret] | @tsv' "$f")
  done
}

# ---- 실행 --------------------------------------------------------------------------------------------
printf '[reset-demo] 시작 %s, 모드 %s, 단계 %s\n' "$RUN_TAG" "$( ((DRY)) && echo dry-run || echo 실행)" "${ONLY:-전체}"
# 단계 함수는 `want && step` 꼴로 부름. 이 꼴에서는 errexit가 함수 안에 걸리지 않아, 한 대상이 실패해도
# 다음 대상과 단계로 넘어감. 실패는 함수 안에서 명시적으로 FAIL로 셈.
want rehearsal && step_rehearsal
want approvals && step_approvals
want front && step_front
want data && step_data
want writes && step_writes

printf '[reset-demo] 요약: DONE %d, ALREADY %d, PLAN %d, NOT_CONNECTED %d, FAIL %d\n' \
  "$N_DONE" "$N_ALREADY" "$N_PLAN" "$N_NC" "$N_FAIL"
if ((N_FAIL)); then exit 1; fi
if ((N_NC)); then exit 3; fi
exit 0
