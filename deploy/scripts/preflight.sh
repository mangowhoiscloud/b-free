#!/usr/bin/env bash
# 데모 직전 점검(B16-B20, P6, P9). 읽기만 합니다. 아무것도 바꾸지 않고, 비밀 값을 출력하지 않습니다.
# 11/7 · 11/8 T-60분과 T-10분에 돌립니다(docs/demo/ONSITE-CHECKLIST.md). 목표 시간 2분 이내(그룹은 동시에 돎).
#
#   deploy/scripts/preflight.sh                  전체 점검, 표 출력
#   deploy/scripts/preflight.sh --out DIR        표(TABLE.md)와 원자료(results.tsv)를 DIR에 저장
#   deploy/scripts/preflight.sh --only cred,link 일부 그룹만
#
# 그룹: kube argo bao cred link repl rgw s1 edge http console host freeze
# 상태: PASS 그대로 진행 · WARN 진행 가능하나 확인 필요 · FAIL 데모 전에 고칠 것
# 종료 코드: 0 FAIL 없음, 1 FAIL 있음, 2 사용법
#
# 환경 변수(모두 선택, 비밀 아님. 쿠키만 예외로 아래 참고)
#   PREFLIGHT_DEADLINE           자격 증명 만료 기준(기본 2026-11-09T00:00:00+09:00). 이보다 먼저 끝나면 WARN, 2시간 안이면 FAIL
#   PREFLIGHT_EXPECT_ACTIVE      시작 때 LB 가중치 1인 쪽(기본 src). PREFLIGHT_EXPECT_FRONT_ENV 대표 이름 env.id(기본 aws)
#   PREFLIGHT_EXPECT_REGRESSION  S1 심은 퇴행 기대 상태(기본 planted, 다른 값 removed)
#   PREFLIGHT_REPL_MAX_AGE_S     정방향 구독 latest_end_time 경과 PASS 상한(기본 45. 2배까지 WARN, 넘으면 FAIL).
#                                wal_sender_timeout 60초에서 쉬는 링크는 30초마다 keepalive가 오므로 45초를 씀
#   PREFLIGHT_FREEZE_TAG         비교할 동결 태그(기본: 가장 최근 demo-freeze-* 태그)
#   PREFLIGHT_OPERATOR_COOKIE    운영자(naver) 브라우저의 CF_Authorization 쿠키 값. PREFLIGHT_APPROVER_COOKIE 승인자(gmail).
#                                있으면 GET https://b-free.mangowhois.ai/v1/me로 역할을 확인. 값은 curl 설정(stdin)으로만 넘기고
#                                파일 · 인자 · 출력에 남기지 않음. 없으면 플랫폼 DB의 신원 · 역할만 보고 WARN
set -uo pipefail
umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/scripts/demo-lib.sh
source "$HERE/demo-lib.sh"

OUT_DIR="" ONLY=""
while (($#)); do
  case $1 in
    --out) OUT_DIR=${2-}; shift ;;
    --only) ONLY=${2-}; shift ;;
    -h | --help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "preflight.sh: 모르는 인자 $1" >&2; exit 2 ;;
  esac
  shift
done
want() { [[ -z $ONLY || ",$ONLY," == *",$1,"* ]]; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/preflight.XXXXXX")"
trap 'rm -rf "$TMP"; jobs -p | xargs kill 2>/dev/null' EXIT
START_S=$SECONDS
STARTED="$(kst_now)"
# res STATUS GROUP CHECK DETAIL  -> one TSV line in this group's file ($RES)
res() { printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "${4//$'\n'/ }" >>"$RES"; }

# ---- kube: 컨텍스트, 노드, 핵심 네임스페이스 Pod --------------------------------------------------------------
g_kube() {
  local a nodes ready total bad
  for a in platform source onprem gke; do
    if ! nodes="$(kc "$a" get nodes -o json 2>"$TMP/kube-$a.err")"; then
      res FAIL kube "$a API" "응답 없음: $(head -c 160 "$TMP/kube-$a.err")"; continue
    fi
    ready="$(jq '[.items[] | select(any(.status.conditions[]; .type == "Ready" and .status == "True"))] | length' <<<"$nodes")"
    total="$(jq '.items | length' <<<"$nodes")"
    res "$([[ $ready == "$total" && $total -gt 0 ]] && echo PASS || echo FAIL)" kube "$a API" \
      "노드 Ready $ready/$total, $(jq -r '.items[0].status.nodeInfo.kubeletVersion' <<<"$nodes")"
    # Pods that are not Ready in the namespaces the demo touches (Completed Jobs are fine).
    bad="$(kc "$a" get pods -A -o json 2>/dev/null | jq -r '.items[]
      | select(.metadata.namespace | test("^(bravo-control|bravo-link|bravo-edge|openbao|argocd|chat|cf-edge|rook-ceph|external-secrets|bravo-observability|bravo-observe)$"))
      | select(.status.phase != "Succeeded")
      | select(any(.status.conditions[]?; .type == "Ready" and .status == "True") | not)
      | "\(.metadata.namespace)/\(.metadata.name)(\(.status.phase))"' | head -n 8 | tr '\n' ' ')"
    res "$([[ -z $bad ]] && echo PASS || echo WARN)" kube "$a Pod" "${bad:-데모 네임스페이스의 Pod 모두 Ready}"
  done
}

# ---- argo: 플랫폼 Argo CD 앱 동기화 · 건강, 배포 revision과 main ------------------------------------------------
g_argo() { "$HERE/argo-state.sh" --tsv >>"$RES" 2>/dev/null || res FAIL argo "Argo CD" "argo-state.sh 실패"; }

# ---- bao + cred: 금고 봉인, custody · RGW · TokenRequest · LLM 자격 ------------------------------------------------
g_bao() { python3 -I "$HERE/preflight_checks.py" bao >>"$RES" 2>"$TMP/bao.err" || res FAIL bao "OpenBao 점검" "$(tail -c 200 "$TMP/bao.err")"; }
g_cf() {
  "$KEYS" with-cloudflare -- python3 -I "$HERE/preflight_checks.py" cf >>"$RES" 2>"$TMP/cf.err" \
    || res FAIL edge "Cloudflare 점검" "$(tail -c 200 "$TMP/cf.err")"
}

# ---- link: D-16 링크 20개 실제 왕복 + 소비자 확인 ---------------------------------------------------------------
g_link() {
  local l a ns dep port env name i=0 st rd
  for l in "${DEMO_LINKS[@]}"; do
    read -r a ns dep port env name <<<"$l"
    (
      rd="$(kc "$a" -n "$ns" get deploy "$dep" -o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null)"
      read -r st detail <<<"$(link_probe "$a" "$ns" "$dep" "$port")"
      res "$([[ $st == OK ]] && echo PASS || echo FAIL)" link "$a $ns/$dep" "Ready ${rd:-?}, :$port $detail (토큰 secret/envs/$env/$name)"
    ) &
    i=$((i + 1))
  done
  # Consumers: the collector reads three cluster APIs over src-k8s / dst-k8s / gke-k8s with its own credentials.
  (
    local lp pf body
    lp="$(free_port)"; kc platform -n bravo-control port-forward svc/collector "$lp:8083" >/dev/null 2>&1 & pf=$!
    for _ in $(seq 1 40); do nc -z 127.0.0.1 "$lp" 2>/dev/null && break; sleep 0.25; done
    body="$(curl -s --max-time 40 "http://127.0.0.1:$lp/v1/clusters")"; kill $pf 2>/dev/null; wait $pf 2>/dev/null
    if [[ -z $body ]] || ! jq -e '.clusters' >/dev/null 2>&1 <<<"$body"; then
      res FAIL link "소비자 수집기 /v1/clusters" "응답 없음"
    else
      while IFS=$'\t' read -r id st ver err; do
        res "$([[ $st == ok ]] && echo PASS || echo FAIL)" link "소비자 수집기 → $id" "status=$st $ver $err"
      done < <(jq -r '.clusters[] | [.cluster_id, .status, (.server_version // "-"), ((.error // "") | tostring | .[0:120])] | @tsv' <<<"$body")
    fi
  ) &
  # The Executor reads both chat DBs (src-db, dst-db links) for readiness.
  (
    local lp pf body
    lp="$(free_port)"; kc platform -n bravo-control port-forward svc/executor "$lp:8081" >/dev/null 2>&1 & pf=$!
    for _ in $(seq 1 40); do nc -z 127.0.0.1 "$lp" 2>/dev/null && break; sleep 0.25; done
    body="$(curl -s --max-time 40 "http://127.0.0.1:$lp/v1/readiness")"; kill $pf 2>/dev/null; wait $pf 2>/dev/null
    if jq -e '.schema_hash.src and (.schema_hash.src == .schema_hash.dst)' >/dev/null 2>&1 <<<"$body"; then
      res PASS link "소비자 Executor /v1/readiness" "$(jq -r '"sub_state=\(.sub_state) direction=\(.direction) lag_bytes=\(.lag_bytes) rows src \(.rows.src) dst \(.rows.dst), 스키마 해시 같음"' <<<"$body")"
    else
      res FAIL link "소비자 Executor /v1/readiness" "$(head -c 200 <<<"${body:-응답 없음}")"
    fi
  ) &
  # GKE External Secrets reads the platform OpenBao through t1-plat-bao.
  (
    local s
    s="$(kc gke get clustersecretstore bravo-platform-openbao -o json 2>/dev/null \
      | jq -r '[.status.conditions[]? | select(.type == "Ready")][0] | "\(.status) \(.reason // "") \(.message // "" | .[0:100])"')"
    res "$([[ $s == True* ]] && echo PASS || echo FAIL)" link "소비자 GKE ClusterSecretStore" "Ready ${s:-없음}"
  ) &
  wait
}

# ---- repl: 논리 복제(정방향 흐름, 역방향 준비), latest_end_time 경과, 슬롯 WAL 보존 --------------------------------
psql_ro() { # ALIAS SQL...
  local a=$1 pod; shift
  pod="$(kc "$a" -n chat get pod -l cnpg.io/cluster=chat-db,cnpg.io/instanceRole=primary -o name 2>/dev/null | head -n 1)"
  [[ -n $pod ]] || return 1
  local args=(-X -q -At -F $'\t' -d chat -c 'SET default_transaction_read_only = on')
  for q in "$@"; do args+=(-c "$q"); done
  kc "$a" -n chat exec "${pod#pod/}" -c postgres -- psql "${args[@]}"
}
g_repl() {
  local max="${PREFLIGHT_REPL_MAX_AGE_S:-45}" row name pid age rage en slot active ret status safe keep
  if ! row="$(psql_ro onprem "SELECT subname, coalesce(pid::text,''), coalesce(extract(epoch FROM now()-latest_end_time)::int::text,''),
      coalesce(extract(epoch FROM now()-last_msg_receipt_time)::int::text,'') FROM pg_stat_subscription WHERE subname='bravo_fwd_sub'" 2>"$TMP/repl.err")"; then
    res FAIL repl "정방향 구독(온프렘)" "조회 실패: $(head -c 160 "$TMP/repl.err")"
  else
    IFS=$'\t' read -r name pid age rage <<<"$(tail -n 1 <<<"$row")"
    if [[ -z $name || -z $pid || -z $age ]]; then
      res FAIL repl "정방향 구독 bravo_fwd_sub" "작업자 없음(구독이 꺼졌거나 링크가 끊김)"
    else
      res "$( ((age <= max)) && echo PASS || { ((age <= 2 * max)) && echo WARN || echo FAIL; })" repl "정방향 구독 bravo_fwd_sub" \
        "streaming pid $pid, latest_end_time ${age}s 전(PASS 상한 ${max}s), 마지막 메시지 ${rage}s 전"
    fi
  fi
  if row="$(psql_ro source "SELECT subname, subenabled FROM pg_subscription WHERE subname='bravo_rev_sub'" 2>/dev/null)"; then
    IFS=$'\t' read -r name en <<<"$(tail -n 1 <<<"$row")"
    if [[ -z $name ]]; then res FAIL repl "역방향 구독 bravo_rev_sub(출발지)" "없음: 되돌리기(S4) 준비 안 됨"
    elif [[ $en == f ]]; then res PASS repl "역방향 구독 bravo_rev_sub(출발지)" "있음, 꺼짐(컷오버 전 상태)"
    else res WARN repl "역방향 구독 bravo_rev_sub(출발지)" "켜짐: 컷오버 뒤 상태. reset-demo.sh 전에 콘솔에서 되돌리기"; fi
  else
    res FAIL repl "역방향 구독 bravo_rev_sub(출발지)" "조회 실패"
  fi
  # Inactive slots keep WAL. Past max_slot_wal_keep_size the slot is lost and S4 failback cannot replicate back.
  local a
  for a in onprem source; do
    keep="$(psql_ro "$a" "SHOW max_slot_wal_keep_size" 2>/dev/null | tail -n 1)"
    while IFS=$'\t' read -r slot active ret status safe; do
      [[ -n $slot ]] || continue
      local st=PASS
      [[ $status == lost || $status == unreserved ]] && st=FAIL
      [[ $st == PASS && -n $safe && $safe -lt 536870912 ]] && st=WARN
      res "$st" repl "슬롯 $slot($a)" "active=$active, 보존 WAL $ret, wal_status=$status, 남은 여유 $((${safe:-0} / 1048576))MB(max_slot_wal_keep_size $keep)"
    done < <(psql_ro "$a" "SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)), wal_status, coalesce(safe_wal_size::text,'') FROM pg_replication_slots" 2>/dev/null)
  done
}

# ---- rgw + s1: 암호화 객체 실제 읽기, 평문 객체, 심은 퇴행 -----------------------------------------------------------
g_rgw() {
  (cd "$REPO" && uv run --quiet --project platform/gate python "$HERE/preflight_checks.py" rgw) >>"$RES" 2>"$TMP/rgw.err" \
    || res FAIL rgw "RGW 점검" "$(tail -c 200 "$TMP/rgw.err")"
}

# ---- http: 플랫폼 · 고객 앱 공개 이름 -------------------------------------------------------------------------------
g_http() {
  local code loc h want id t
  read -r code loc <<<"$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 15 https://b-free.mangowhois.ai/)"
  res "$([[ $code == 302 && $loc == *cloudflareaccess.com* ]] && echo PASS || echo FAIL)" http "b-free.mangowhois.ai" \
    "HTTP $code → ${loc%%\?*}(로그인 없이는 Access 로그인으로 가야 함)"
  for h in "b-free-chat:${PREFLIGHT_EXPECT_FRONT_ENV:-aws}" b-free-chat-src:aws b-free-chat-dst:onprem b-free-chat-gke:gcp; do
    want=${h#*:}; h=${h%%:*}
    read -r code t <<<"$(curl -s -o "$TMP/$h.json" -w '%{http_code} %{time_total}' --max-time 15 "https://$h.mangowhois.ai/api/config")"
    id="$(jq -r '.env.id // "-"' "$TMP/$h.json" 2>/dev/null || echo -)"
    res "$([[ $code == 200 && $id == "$want" ]] && echo PASS || echo FAIL)" http "$h.mangowhois.ai" \
      "GET /api/config HTTP $code $(printf '%.2f' "${t:-0}")s env.id=$id(기대 $want)"
  done
}

# ---- console: 두 신원(P9). 플랫폼 DB의 신원 · 역할, 쿠키가 있으면 /v1/me ---------------------------------------------
g_console() {
  local rows dom roles v who code body
  rows="$(kc platform -n bravo-control exec platform-db-1 -c postgres -- psql -X -q -At -F $'\t' -d bravo \
    -c 'SET default_transaction_read_only = on' -c "SELECT split_part(p.email,'@',2), coalesce(string_agg(m.role, ',' ORDER BY m.role),''),
      to_char(p.created_at AT TIME ZONE 'Asia/Seoul','MM-DD HH24:MI') FROM principals p LEFT JOIN memberships m ON m.principal_id = p.id
      AND m.tenant_id = 'b-free' GROUP BY p.id, 1, 3 ORDER BY 3" 2>"$TMP/console.err")" \
    || { res FAIL console "플랫폼 DB 신원" "조회 실패: $(head -c 160 "$TMP/console.err")"; rows=""; }
  for who in "operator:naver.com" "approver:gmail.com"; do
    v=${who%%:*}; dom=${who#*:}
    roles="$(awk -F'\t' -v d="$dom" '$1 == d {print $2 " (첫 로그인 " $3 ")"}' <<<"$rows" | head -n 1)"
    if [[ $roles == *"$v"* ]]; then res PASS console "$v 신원(@$dom)" "플랫폼 DB 역할 $roles"
    else res FAIL console "$v 신원(@$dom)" "플랫폼 DB에 ${roles:+역할 $roles만 있음}${roles:-신원 없음}. 그 계정으로 https://b-free.mangowhois.ai 첫 로그인(One-time PIN)하면 members.json 역할이 붙음"; fi
  done
  local var
  for who in "operator:PREFLIGHT_OPERATOR_COOKIE" "approver:PREFLIGHT_APPROVER_COOKIE"; do
    v=${who%%:*}; var=${who#*:}
    if [[ -z ${!var-} ]]; then
      res WARN console "$v 세션 /v1/me" "$var 없음: 그 브라우저에서 로그인 상태와 역할을 직접 확인(세션 24시간, T-60분에 새로 로그인)"
      continue
    fi
    body="$(printf 'header = "Cookie: CF_Authorization=%s"\n' "${!var}" | curl -s -K - -w '\n%{http_code}' --max-time 15 https://b-free.mangowhois.ai/v1/me)"
    code="$(tail -n 1 <<<"$body")"; body="$(sed '$d' <<<"$body")"
    if [[ $code == 200 ]] && jq -e --arg r "$v" '.roles | index($r)' >/dev/null 2>&1 <<<"$body"; then
      res PASS console "$v 세션 /v1/me" "HTTP 200, roles $(jq -c .roles <<<"$body"), principal $(jq -r '.principal.id[0:8]' <<<"$body")"
    else
      res FAIL console "$v 세션 /v1/me" "HTTP $code $(jq -c '{roles}' 2>/dev/null <<<"$body" || echo '(로그인 페이지로 감: 세션 만료)')"
    fi
  done
}

# ---- host: Lima VM, 절전, 전원, 디스크 · 메모리 ------------------------------------------------------------------
g_host() {
  local vms n st sleepv batt free pct vm line
  if vms="$(limactl list --json 2>/dev/null)"; then
    for n in bravo-onprem-server bravo-onprem-agent; do
      st="$(jq -r --arg n "$n" 'select(.name == $n) | .status' <<<"$vms")"
      if [[ $st != Running ]]; then res FAIL host "Lima $n" "상태 ${st:-없음}: limactl start $n"; continue; fi
      line="$(limactl shell "$n" -- sh -c 'df -P / | tail -n 1; free -m | sed -n 2p' 2>/dev/null | tr '\n' ' ')"
      read -r _ _ _ _ pct _ _ _ _ _ _ _ free <<<"$line"
      res "$([[ ${pct%\%} -lt 85 && ${free:-0} -gt 1024 ]] && echo PASS || echo WARN)" host "Lima $n" \
        "Running, 디스크 / 사용 ${pct:-?}, 메모리 available ${free:-?}MB"
    done
  else
    res FAIL host "Lima" "limactl 없음 또는 실패"
  fi
  sleepv="$(pmset -g | awk '$1 == "sleep" {print $2}')"
  if [[ $sleepv == 0 ]]; then res PASS host "Mac 절전" "pmset sleep 0"
  elif pgrep -fl caffeinate 2>/dev/null | grep -Eq 'caffeinate .*-[a-z]*[is]'; then
    res PASS host "Mac 절전" "pmset sleep ${sleepv}분이지만 caffeinate(-i/-s) 실행 중"
  else res WARN host "Mac 절전" "pmset sleep ${sleepv}분, caffeinate 없음: caffeinate -dimsu & 또는 sudo pmset -a sleep 0"; fi
  batt="$(pmset -g batt | head -n 1)"
  res "$([[ $batt == *"AC Power"* ]] && echo PASS || echo WARN)" host "Mac 전원" "${batt#Now drawing from }"
  pct="$(df -P / | awk 'NR == 2 {print $5}')"
  free="$(memory_pressure 2>/dev/null | awk -F': ' '/free percentage/ {print $2}')"
  res "$([[ ${pct%\%} -lt 85 && ${free%\%} -ge 15 ]] && echo PASS || echo WARN)" host "Mac 디스크 · 메모리" "디스크 / 사용 $pct, 메모리 여유 ${free:-?}"
  # Ephemeral ports: on 2026-11-03 02:44 KST a laptop loadgen left ~9,400 TIME_WAIT sockets and kubectl / curl failed
  # with "connect: can't assign requested address" (EKS API and b-free-chat-src both looked down from this Mac).
  local tw est range used st
  read -r tw est <<<"$(netstat -an -p tcp 2>/dev/null | awk '$6 == "TIME_WAIT" {t++} $6 == "ESTABLISHED" {e++} END {print t + 0, e + 0}')"
  range=$(($(sysctl -n net.inet.ip.portrange.last) - $(sysctl -n net.inet.ip.portrange.first) + 1))
  used=$((tw + est)); st=PASS
  ((used > range / 2)) && st=WARN
  ((used > range * 8 / 10)) && st=FAIL
  res "$st" host "Mac TCP 포트" "TIME_WAIT $tw, ESTABLISHED $est / 임시 포트 $range개. 가득 차면 kubectl · 브라우저 연결이 실패(노트북 loadgen을 무대 Mac에서 돌리지 말 것)"
}

# ---- freeze: 동결 태그와 지금 배포 비교(B20, P23) ---------------------------------------------------------------
g_freeze() {
  local tag="${PREFLIGHT_FREEZE_TAG:-$(cd "$REPO" && git tag -l 'demo-freeze-*' --sort=-creatordate | head -n 1)}"
  if [[ -z $tag ]]; then res WARN freeze "동결 태그" "demo-freeze-* 태그 없음: deploy/scripts/demo-freeze.sh create(데모 전날)"; return; fi
  "$HERE/demo-freeze.sh" check "$tag" --tsv >>"$RES" 2>"$TMP/freeze.err" || true
  [[ -s $TMP/freeze.err ]] && res WARN freeze "동결 비교" "$(tail -c 200 "$TMP/freeze.err")"
}

# ---- 실행 --------------------------------------------------------------------------------------------------------
run() { local g=$1; RES="$TMP/$g.tsv"; : >"$RES"; local t=$SECONDS; "g_$g"; printf '%s\t%s\n' "$g" "$((SECONDS - t))" >"$TMP/$g.time"; }
for g in kube argo bao link repl rgw edge http console host freeze; do
  case $g in
    edge) want edge || want cred || continue; run cf & ;;
    bao) want bao || want cred || continue; run bao & ;;
    rgw) want rgw || want s1 || continue; run rgw & ;;
    *) want "$g" || continue; run "$g" & ;;
  esac
done
wait

ORDER=(kube argo bao cred link repl rgw s1 edge http console host freeze)
cat "$TMP"/*.tsv 2>/dev/null >"$TMP/all"
: >"$TMP/sorted"
for g in "${ORDER[@]}"; do awk -F'\t' -v g="$g" '$2 == g' "$TMP/all" >>"$TMP/sorted"; done
awk -F'\t' 'BEGIN{split("kube argo bao cred link repl rgw s1 edge http console host freeze",o," "); for(i in o) k[o[i]]=1} !($2 in k)' "$TMP/all" >>"$TMP/sorted"
if [[ -n $ONLY ]]; then awk -F'\t' -v o=",$ONLY," 'index(o, "," $2 ",")' "$TMP/sorted" >"$TMP/f" && mv "$TMP/f" "$TMP/sorted"; fi

ELAPSED=$((SECONDS - START_S))
NP="$(awk -F'\t' '$1 == "PASS"' "$TMP/sorted" | wc -l | tr -d ' ')"
NW="$(awk -F'\t' '$1 == "WARN"' "$TMP/sorted" | wc -l | tr -d ' ')"
NF="$(awk -F'\t' '$1 == "FAIL"' "$TMP/sorted" | wc -l | tr -d ' ')"
{
  printf 'preflight %s, %ss, PASS %s · WARN %s · FAIL %s\n\n' "$STARTED" "$ELAPSED" "$NP" "$NW" "$NF"
  awk -F'\t' '{ printf "%-4s  %-7s  %s: %s\n", $1, $2, $3, $4 }' "$TMP/sorted"
  printf '\n그룹별 시간(초): %s\n' "$(cat "$TMP"/*.time 2>/dev/null | tr '\t\n' '= ' )"
} | tee "$TMP/table.txt"

if [[ -n $OUT_DIR ]]; then
  mkdir -p "$OUT_DIR"
  cp "$TMP/sorted" "$OUT_DIR/results.tsv"
  {
    printf '# preflight %s\n\n%s초, PASS %s · WARN %s · FAIL %s. 명령 `deploy/scripts/preflight.sh`(읽기만, 비밀 값 없음)\n\n' \
      "$STARTED" "$ELAPSED" "$NP" "$NW" "$NF"
    printf '| 상태 | 그룹 | 항목 | 내용 |\n|---|---|---|---|\n'
    awk -F'\t' '{ gsub(/\|/, "\\|"); printf "| %s | %s | %s | %s |\n", $1, $2, $3, $4 }' "$TMP/sorted"
  } >"$OUT_DIR/TABLE.md"
  echo "저장: $OUT_DIR/TABLE.md"
fi
((NF == 0))
