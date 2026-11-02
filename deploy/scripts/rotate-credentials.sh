#!/usr/bin/env bash
# 자격 증명 회전 연습(B16 · B17). 만료가 가까운 자격을 하나씩 바꾸고, 바꿀 때마다 그 자격을 쓰는 쪽(소비자)을 실제로
# 확인합니다. 회전은 기존 스크립트로만 합니다: keys.sh(rotate-service-token, refresh-service-token, custody-renew,
# env-pipe), deploy/platform/scripts/onprem-tokens.sh, gate-credentials.sh. 값은 파이프로만 움직이고 출력하지 않습니다.
#
#   deploy/scripts/rotate-credentials.sh plan [--before ISO]      읽기만. 모든 자격의 만료와, run이 바꿀 것을 표로 냄
#   deploy/scripts/rotate-credentials.sh run  [--before ISO] [--kinds K,..] [--dry-run]
#        --before 앞에 만료되는 것만 회전(기본 2026-11-09T06:00:00+09:00 = 11/8 데모 다음 날 새벽)
#        --kinds  custody,service-token,k8s(기본 셋 다). rgw-kms는 기본에 없음: RGW가 다시 떠서 첨부가 약 40초
#                 멈추므로 무대 밖에서 --kinds rgw-kms로 따로 돌림
#   deploy/scripts/rotate-credentials.sh one service-token ENV/SECRET [--refresh-only]
#   deploy/scripts/rotate-credentials.sh one k8s onprem|gate      onprem = collector-k8s, k8s-executor, k8s-rehearsal
#   deploy/scripts/rotate-credentials.sh one custody | rgw-kms
#
# 종류마다 하는 일과 확인
#   custody        keys.sh custody-renew(주기 168h 토큰 renew-self) → preflight_checks.py bao로 새 만료 확인
#   service-token  keys.sh rotate-service-token ENV SECRET 15(새 client secret, 옛 것은 15분 유효, 만료 갱신)
#                  → 소비자 클러스터의 Secret을 OpenBao에서 파이프로 다시 만듦 → 링크 Deployment rollout restart
#                  → link_probe(링크 너머 서비스가 실제로 답함). 링크 표: deploy/scripts/demo-lib.sh DEMO_LINKS
#   k8s onprem     onprem-tokens.sh k8s(TokenRequest 168h 재발급, 수집기 · 리허설 kubeconfig Secret)
#                  → Executor 파일 Secret bravo-control/bx-envs-target-onprem-k8s-executor 다시 전달
#                  → 새 토큰으로 SelfSubjectReview(온프렘 API), executor-0 안에서 마운트된 토큰으로 링크 dst-k8s 경유 확인
#   k8s gate       gate-credentials.sh onprem_token → 같은 확인
#   rgw-kms        onprem-tokens.sh rgw-kms(새 RGW 토큰, cf-link-plat-bao 다시 전달, RGW 재시작, 옛 토큰 폐기)
#                  → preflight_checks.py rgw(암호화 객체 실제 읽기)
# 옛 TokenRequest 토큰은 회전 뒤에도 자기 만료까지 유효합니다(쿠버네티스는 발급된 토큰을 따로 폐기하지 않음).
# 기록: deploy/scripts/rotate-credentials.sh run ... --log DIR 이면 DIR/rotate.log와 전후 만료 표를 남김.
set -uo pipefail
umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/scripts/demo-lib.sh
source "$HERE/demo-lib.sh"
ONPREM_TOKENS="$REPO/deploy/platform/scripts/onprem-tokens.sh"
GATE_CREDS="$REPO/deploy/platform/scripts/gate-credentials.sh"
BEFORE="2026-11-09T06:00:00+09:00"
KINDS="custody,service-token,k8s"
DRY=0 LOG_DIR="" REFRESH_ONLY=0
PROPAGATE_S="${ROTATE_PROPAGATE_S:-30}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/rotate.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
declare -i N_OK=0 N_FAIL=0

ARGS=()
say() {
  local line; line="$(printf '[rotate] %s %s' "$(TZ=Asia/Seoul date +%H:%M:%S)" "$*")"
  printf '%s\n' "$line"; [[ -z $LOG_DIR ]] || printf '%s\n' "$line" >>"$LOG_DIR/rotate.log"
}
# Output of a rotation command, indented, also into the log.
sub_out() { local l; while IFS= read -r l; do say "  $l"; done; }
ok() { N_OK+=1; say "OK   $*"; }
bad() { N_FAIL+=1; say "FAIL $*"; }

# Expiry table: KIND KEY EXPIRES_AT(UTC ISO or -) from the same code preflight uses.
expiries() {
  local f="$TMP/exp.tsv"; : >"$f"
  PREFLIGHT_EXPIRY_FILE="$f" python3 -I "$HERE/preflight_checks.py" bao >/dev/null 2>&1
  PREFLIGHT_EXPIRY_FILE="$f" "$KEYS" with-cloudflare -- python3 -I "$HERE/preflight_checks.py" cf >/dev/null 2>&1
  sort -u "$f"
}
# due KIND KEY EXP -> 0 when EXP is before BEFORE
due() { python3 -I -c 'import sys,datetime as d
e,b=sys.argv[1],d.datetime.fromisoformat(sys.argv[2])
sys.exit(0 if e!="-" and d.datetime.fromisoformat(e)<b else 1)' "$3" "$BEFORE"; }
kst_of() { [[ $1 == - ]] && { echo "만료 없음"; return; }; python3 -I -c 'import sys,datetime as d
print(d.datetime.fromisoformat(sys.argv[1]).astimezone(d.timezone(d.timedelta(hours=9))).strftime("%m-%d %H:%M KST"))' "$1"; }

print_table() { # file
  printf '%-14s %-38s %-18s %s\n' 종류 대상 만료 "run($BEFORE 전이면 회전)"
  while IFS=$'\t' read -r kind key exp; do
    local act="그대로"
    if due "$kind" "$key" "$exp"; then
      case $kind in
        cf-api-token) act="수동: Cloudflare 대시보드에서 새 API 토큰 → keys.sh import-cloudflare" ;;
        rgw-kms) act="회전 대상(--kinds rgw-kms, 무대 밖)" ;;
        *) act="회전 대상" ;;
      esac
    fi
    printf '%-14s %-38s %-18s %s\n' "$kind" "$key" "$(kst_of "$exp")" "$act"
  done <"$1"
}

# ---- service tokens ---------------------------------------------------------------------------------------------
# deliver_link_secret ALIAS NS SECRET OPENBAO_ENV OPENBAO_NAME: client_id + client_secret, pipes only.
deliver_link_secret() {
  local a=$1 ns=$2 name=$3 env=$4 bname=$5 acc f
  acc="$(jq -nc --arg n "$name" --arg ns "$ns" '{apiVersion: "v1", kind: "Secret", type: "Opaque",
    metadata: {name: $n, namespace: $ns, labels: {"app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "openbao"}}, data: {}}')"
  for f in client_id client_secret; do
    acc="$(printf '%s\n%s\n' "$acc" "$("$KEYS" env-pipe "$env" "$bname" "$f" -- jq -Rs '{v: (. | @base64)}')" \
      | jq -sc --arg k "$f" '.[0] as $s | .[1].v as $v | $s | .data[$k] = $v')"
  done
  printf '%s\n' "$acc" | kc "$a" apply --server-side --force-conflicts --field-manager=bravo-secrets -f - >/dev/null
  local rc=$?; acc=""; return $rc
}

rotate_service_token() { # ENV/SECRET
  local env=${1%%/*} bname=${1#*/} l a ns dep port e n found=0 r WAITED=0
  if ((DRY)); then say "PLAN service-token $env/$bname: rotate-service-token(유예 15분) → 소비자 Secret → restart → 링크 확인"; return; fi
  if ((REFRESH_ONLY)); then
    "$KEYS" refresh-service-token "$env" "$bname" 2>&1 | sub_out || { bad "service-token $env/$bname refresh 실패"; return; }
  else
    "$KEYS" rotate-service-token "$env" "$bname" 15 2>&1 | sub_out
    [[ ${PIPESTATUS[0]} == 0 ]] || { bad "service-token $env/$bname rotate 실패(옛 비밀은 그대로이거나 15분 유효)"; return; }
  fi
  for l in "${DEMO_LINKS[@]}"; do
    read -r a ns dep port e n <<<"$l"
    [[ $e == "$env" && $n == "$bname" ]] || continue
    found=1
    if ((!REFRESH_ONLY)); then
      deliver_link_secret "$a" "$ns" "$n" "$e" "$n" || { bad "$a $ns/$n Secret 전달 실패"; continue; }
      # Measured 2026-11-03 02:54 KST: Cloudflare answered "websocket: bad handshake" to the NEW secret for about 15 s
      # after rotate. The old secret stays valid (grace), so restart the client only after the new one has spread.
      if ((!WAITED)); then say "  새 비밀이 Cloudflare에 퍼지도록 ${PROPAGATE_S}초 기다린 뒤 재시작(옛 비밀은 유예 중)"; sleep "$PROPAGATE_S"; WAITED=1; fi
      kc "$a" -n "$ns" rollout restart "deploy/$dep" >/dev/null && kc "$a" -n "$ns" rollout status "deploy/$dep" --timeout=180s >/dev/null \
        || { bad "$a $ns/$dep 재시작 실패"; continue; }
    fi
    local t0=$SECONDS tries=0
    while :; do
      r="$(link_probe "$a" "$ns" "$dep" "$port")"; tries=$((tries + 1))
      [[ $r == OK* ]] || ((SECONDS - t0 >= 60)) && break
      sleep 5
    done
    if [[ $r == OK* ]]; then ok "service-token $env/$bname → $a $ns/$dep 링크 왕복 ${r#OK }(재시작 뒤 ${tries}번째 시도, $((SECONDS - t0))초)"
    else bad "service-token $env/$bname → $a $ns/$dep 링크 확인 실패 60초: $r(옛 비밀 유예 15분 안에 고칠 것)"; fi
  done
  ((found)) || say "INFO service-token $env/$bname: DEMO_LINKS에 소비자 없음(Secret 전달 안 함)"
}

# ---- Kubernetes TokenRequest tokens ------------------------------------------------------------------------------
onprem_api() { kubectl config view --raw -o json | jq -r --arg c "$ONPREM_CTX" '(.contexts[] | select(.name == $c) | .context.cluster) as $cl | .clusters[] | select(.name == $cl) | .cluster.server'; }
onprem_ca() { kubectl config view --raw -o json | jq -r --arg c "$ONPREM_CTX" '(.contexts[] | select(.name == $c) | .context.cluster) as $cl | .clusters[] | select(.name == $cl) | .cluster["certificate-authority-data"]' | base64 -d >"$TMP/onprem-ca.crt"; }
# SelfSubjectReview with the token in OpenBao (header on a pipe): prints the username the API server sees.
whoami_token() { # bao-name
  onprem_ca
  "$KEYS" env-pipe target-onprem "$1" token -- sh -c 'printf "Authorization: Bearer %s\n" "$(cat)" | curl -s --cacert "$0" -H @- \
    -H "Content-Type: application/json" -X POST -d "{\"apiVersion\":\"authentication.k8s.io/v1\",\"kind\":\"SelfSubjectReview\"}" \
    "$1/apis/authentication.k8s.io/v1/selfsubjectreviews"' "$TMP/onprem-ca.crt" "$(onprem_api)" | jq -r '.status.userInfo.username // "거부"'
}
# The executor's mounted token through its own path (link dst-k8s), inside executor-0. Waits for the kubelet to refresh.
executor_onprem_whoami() {
  kubectl --context "$PLATFORM_CTX" -n bravo-control exec -i executor-0 -c executor -- python - <<'PY' 2>/dev/null
import hashlib, http.client, json, socket, ssl
tok = open("/etc/bravo/secrets/envs__target-onprem__k8s-executor/token").read().strip()
fp = hashlib.sha256(tok.encode()).hexdigest()[:8]  # 8 hex of a hash: tells which token, reveals nothing
ctx = ssl.create_default_context(cafile="/etc/bravo/kube/onprem-ca.crt")
ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
class Link(http.client.HTTPSConnection):  # dial the link Service, verify the API server as kubernetes.default.svc
    def connect(self):
        self.sock = self._context.wrap_socket(socket.create_connection((self.host, self.port), self.timeout),
                                              server_hostname="kubernetes.default.svc")
c = Link("dst-k8s.bravo-link.svc.cluster.local", 443, context=ctx, timeout=20)
c.request("POST", "/apis/authentication.k8s.io/v1/selfsubjectreviews",
          body=b'{"apiVersion":"authentication.k8s.io/v1","kind":"SelfSubjectReview"}',
          headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
tok = ""
print(json.loads(c.getresponse().read())["status"]["userInfo"]["username"], fp)
PY
}
same_as_delivered() { # bao-name -> the preflight line for it (delivered copy equal or not)
  python3 -I "$HERE/preflight_checks.py" bao 2>/dev/null | awk -F'\t' -v n="온프렘 토큰 $1" '$3 == n {print $1 " " $4}'
}

rotate_k8s() { # onprem | gate
  local which=$1 n u line i
  if ((DRY)); then say "PLAN k8s $which: $([[ $which == gate ]] && echo 'gate-credentials.sh onprem_token' || echo 'onprem-tokens.sh k8s + Executor 파일 Secret') → SelfSubjectReview · 전달본 비교"; return; fi
  if [[ $which == gate ]]; then
    BRAVO_PLATFORM_CONTEXT="$PLATFORM_CTX" "$GATE_CREDS" onprem_token 2>&1 | sub_out
    [[ ${PIPESTATUS[0]} == 0 ]] || { bad "k8s gate: gate-credentials.sh onprem_token 실패"; return; }
    set -- k8s-gate
  else
    BRAVO_PLATFORM_CONTEXT="$PLATFORM_CTX" "$ONPREM_TOKENS" k8s 2>&1 | sub_out
    [[ ${PIPESTATUS[0]} == 0 ]] || { bad "k8s onprem: onprem-tokens.sh k8s 실패"; return; }
    # The Executor reads its token file (kubeconfig tokenFile) from this Secret; deliver-secrets.sh makes it the same way.
    local acc
    acc="$(jq -nc '{apiVersion: "v1", kind: "Secret", type: "Opaque", metadata: {name: "bx-envs-target-onprem-k8s-executor",
      namespace: "bravo-control", labels: {"app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "openbao"}}, data: {}}')"
    acc="$(printf '%s\n%s\n' "$acc" "$("$KEYS" env-pipe target-onprem k8s-executor token -- jq -Rs '{v: (. | @base64)}')" \
      | jq -sc '.[0] as $s | .[1].v as $v | $s | .data.token = $v')"
    printf '%s\n' "$acc" | kc platform apply --server-side --force-conflicts --field-manager=bravo-secrets -f - >/dev/null \
      || bad "Secret bravo-control/bx-envs-target-onprem-k8s-executor 전달 실패"
    acc=""
    set -- collector-k8s k8s-executor k8s-rehearsal
  fi
  for n in "$@"; do
    u="$(whoami_token "$n")"
    line="$(same_as_delivered "$n")"
    if [[ $u == system:serviceaccount:* && $line == PASS* ]]; then ok "k8s $n: 새 토큰 = $u, ${line#PASS }"
    else bad "k8s $n: SelfSubjectReview=$u, ${line:-preflight 줄 없음}"; fi
  done
  if [[ $which == onprem ]]; then
    local want got fp
    want="$("$KEYS" env-pipe target-onprem k8s-executor token -- python3 -I -c 'import sys,hashlib;print(hashlib.sha256(sys.stdin.read().strip().encode()).hexdigest()[:8])')"
    for i in $(seq 1 18); do  # kubelet refreshes Secret volumes within about a minute
      read -r got fp <<<"$(executor_onprem_whoami)"; [[ $fp == "$want" && $got == system:serviceaccount:chat:bravo-executor ]] && break; sleep 10
    done
    if [[ $fp == "$want" && $got == system:serviceaccount:chat:bravo-executor ]]; then
      ok "executor-0: 마운트된 토큰이 새 토큰(지문 $fp)이고 링크 dst-k8s 경유 SelfSubjectReview = $got"
    else bad "executor-0: 마운트 토큰 지문 ${fp:-?}(기대 $want), SelfSubjectReview ${got:-응답 없음}"; fi
  fi
}

rotate_custody() {
  if ((DRY)); then say "PLAN custody: keys.sh custody-renew(renew-self)"; return; fi
  local out; out="$("$KEYS" custody-renew 2>&1)"
  sub_out <<<"$out"
  if grep -q FAILED <<<"$out"; then bad "custody: renew 실패 있음"; else ok "custody: 주기 토큰 모두 renew-self"; fi
}

rotate_rgw() {
  if ((DRY)); then say "PLAN rgw-kms: onprem-tokens.sh rgw-kms(RGW 재시작 약 40초) → 암호화 객체 읽기"; return; fi
  BRAVO_PLATFORM_CONTEXT="$PLATFORM_CTX" "$ONPREM_TOKENS" rgw-kms 2>&1 | sub_out
  [[ ${PIPESTATUS[0]} == 0 ]] || { bad "rgw-kms: onprem-tokens.sh rgw-kms 실패"; return; }
  local line
  line="$(cd "$REPO" && uv run --quiet --project platform/gate python "$HERE/preflight_checks.py" rgw | awk -F'\t' '$3 == "암호화 객체 읽기"')"
  [[ $line == PASS* ]] && ok "rgw-kms: ${line##*$'\t'}" || bad "rgw-kms: 암호화 객체 읽기 ${line:-결과 없음}"
}

cmd_run() {
  local f="$TMP/before.tsv" kind key exp
  expiries >"$f"
  say "회전 전 만료(기준 $BEFORE)"; print_table "$f" | sub_out
  [[ -n $LOG_DIR ]] && print_table "$f" >"$LOG_DIR/before.txt"
  local did_onprem=0
  while IFS=$'\t' read -r kind key exp; do
    [[ ,$KINDS, == *,$kind,* ]] || continue
    due "$kind" "$key" "$exp" || continue
    case $kind in
      custody) rotate_custody; KINDS="${KINDS//custody/}" ;;   # one renew covers every custody token
      service-token) rotate_service_token "$key" ;;
      k8s) if [[ $key == k8s-gate ]]; then rotate_k8s gate; elif ((!did_onprem)); then rotate_k8s onprem; did_onprem=1; fi ;;
      rgw-kms) rotate_rgw ;;
    esac
  done <"$f"
  ((DRY)) && return
  expiries >"$TMP/after.tsv"
  say "회전 뒤 만료"; print_table "$TMP/after.tsv" | sub_out
  [[ -n $LOG_DIR ]] && print_table "$TMP/after.tsv" >"$LOG_DIR/after.txt"
  say "요약: OK $N_OK, FAIL $N_FAIL"
}

sub=${1-}; shift || true
while (($#)); do
  case $1 in
    --before) BEFORE=${2-}; shift ;;
    --kinds) KINDS=${2-}; shift ;;
    --dry-run) DRY=1 ;;
    --refresh-only) REFRESH_ONLY=1 ;;
    --log) LOG_DIR=${2-}; mkdir -p "$LOG_DIR"; shift ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
case $sub in
  plan) expiries >"$TMP/p.tsv"; print_table "$TMP/p.tsv" ;;
  run) cmd_run ;;
  one)
    case ${ARGS[0]-} in
      service-token) rotate_service_token "${ARGS[1]:?ENV/SECRET}" ;;
      k8s) rotate_k8s "${ARGS[1]:?onprem|gate}" ;;
      custody) rotate_custody ;;
      rgw-kms) rotate_rgw ;;
      *) echo "rotate-credentials.sh one service-token ENV/SECRET | k8s onprem|gate | custody | rgw-kms" >&2; exit 2 ;;
    esac
    say "요약: OK $N_OK, FAIL $N_FAIL" ;;
  *) sed -n '2,30p' "$0"; exit 2 ;;
esac
((N_FAIL == 0))
