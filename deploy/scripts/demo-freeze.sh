#!/usr/bin/env bash
# 데모 동결(B20, P23): 데모에 쓰는 커밋, 이미지 digest, ConfigMap, Argo CD 동기화 revision을 한 번에 기록하고,
# 나중에 지금 상태와 비교합니다. 클러스터에는 읽기만 합니다. 태그는 로컬에만 만들고 push는 팀장이 합니다.
#
#   deploy/scripts/demo-freeze.sh create [--name demo-freeze-YYYYMMDD-HHMM]
#       전제: 플랫폼 Argo CD 앱이 모두 Healthy이고, git 앱의 동기화 revision이 origin main과 같고,
#       OutOfSync가 없음(Cilium 인증서 Secret과 prune 대상 고아만 허용, 그 사실을 기록).
#       기록: docs/evidence/freeze/<태그>/record.json(+ SUMMARY.md), 같은 내용을 담은 주석 태그(배포 커밋에).
#   deploy/scripts/demo-freeze.sh check [태그] [--tsv]
#       지금 상태를 다시 모아 기록과 비교. 이미지 digest · ConfigMap이 다르면 FAIL. Argo revision이 움직였는데
#       deploy/ 아래 매니페스트 변경이 없으면 WARN(문서만 바뀐 커밋), 있으면 FAIL.
#   deploy/scripts/demo-freeze.sh collect     지금 상태 JSON만 출력
#
# 동결 중 규칙(docs/demo/ONSITE-CHECKLIST.md): main에 deploy/ 변경을 push하지 않음. 꼭 고쳐야 하면 고친 뒤
# create를 다시 돌려 새 태그를 만듦. Argo를 태그에 묶는 방법(apps/platform.yaml targetRevision)은 팀장 결정 사항.
set -euo pipefail
umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/scripts/demo-lib.sh
source "$HERE/demo-lib.sh"
FREEZE_DIR="$REPO/docs/evidence/freeze"

# Workloads whose images and ConfigMaps make up the demo: ALIAS NAMESPACE.
SCOPE=("platform bravo-control" "platform bravo-link" "platform openbao" "platform bravo-observe" "platform cf-edge"
  "source chat" "source cf-edge" "onprem chat" "onprem cf-edge" "onprem rook-ceph" "gke chat" "gke cf-edge")

collect() {
  local a ns w p items='[]' argo main
  for s in "${SCOPE[@]}"; do
    read -r a ns <<<"$s"
    w="$(kc "$a" -n "$ns" get deploy,statefulset,daemonset -o json 2>/dev/null || echo '{"items":[]}')"
    p="$(kc "$a" -n "$ns" get pods -o json 2>/dev/null || echo '{"items":[]}')"
    items="$(jq -c --arg a "$a" --arg ns "$ns" --argjson p "$p" '. as $w | $w.items | map(
      . as $o | ($o.spec.selector.matchLabels // {}) as $sel
      | {key: "\($a)/\($ns)/\($o.kind | ascii_downcase)/\($o.metadata.name)",
         value: {images: [$o.spec.template.spec.containers[].image] | sort,
                 configmaps: ([$o.spec.template.spec.volumes[]?.configMap.name // empty]
                   + [$o.spec.template.spec.volumes[]?.projected.sources[]?.configMap.name // empty]) | unique,
                 running: [$p.items[] | select(.status.phase == "Running")
                   | select(.metadata.labels as $l | $sel | to_entries | all($l[.key] == .value))
                   | .status.containerStatuses[] | (.imageID | sub("^docker-pullable://"; ""))] | unique}})
      | from_entries' <<<"$w" | jq -c --argjson acc "$items" '$acc + [.]')"
  done
  argo="$(kc platform -n argocd get applications.argoproj.io -o json | jq -c '[.items[] | {key: .metadata.name, value: {
    sync: .status.sync.status, health: .status.health.status, revision: (.status.sync.revision // null),
    target: (.spec.source.targetRevision // null),
    out_of_sync: [.status.resources[]? | select(.status == "OutOfSync") | "\(.kind)/\(.name)" + (if .requiresPruning then " (prune)" else "" end)]}}] | from_entries')"
  main="$(cd "$REPO" && timeout 15 git ls-remote origin refs/heads/main | cut -f1)"
  jq -n --argjson items "$items" --argjson argo "$argo" --arg main "$main" --arg at "$(kst_now)" \
    --arg pins "$(awk '/newName:/ {n = $2} /digest:/ {print n "@" $2}' "$REPO/deploy/platform/control/kustomization.yaml" | tr '\n' ' ')" \
    '{collected_at: $at, origin_main: $main, argo: $argo,
      platform_pins: ($pins | split(" ") | map(select(length > 0))),
      workloads: ($items | add // {})}'
}

# Problems that block a freeze: unhealthy apps, git apps not at main, real OutOfSync.
blockers() { # JSON from collect on stdin
  jq -r '.origin_main as $m | .argo | to_entries[] | .key as $n | .value
    | (if .health != "Healthy" then "\($n): health \(.health)" else empty end),
      (if (.revision // "" | test("^[0-9a-f]{40}$")) and .revision != $m then "\($n): revision \(.revision[0:8]) != main \($m[0:8])" else empty end),
      ([.out_of_sync[] | select((endswith(" (prune)") | not)
         and (($n == "cilium") and test("^Secret/(cilium-ca|hubble-relay-client-certs|hubble-server-certs)$") | not))] as $real
       | if ($real | length) > 0 then "\($n): OutOfSync \($real | join(", "))" else empty end)'
}

cmd_create() {
  local name="" now rec commit b dir
  [[ ${1-} == --name ]] && name=${2-}
  now="$(TZ=Asia/Seoul date +%Y%m%d-%H%M)"
  name="${name:-demo-freeze-$now}"
  [[ $name =~ ^demo-freeze-[0-9A-Za-z-]+$ ]] || { echo "demo-freeze: 태그 이름은 demo-freeze-로 시작" >&2; exit 2; }
  rec="$(collect)"
  commit="$(jq -r .origin_main <<<"$rec")"
  [[ $commit =~ ^[0-9a-f]{40}$ ]] || { echo "demo-freeze: origin main을 읽지 못함" >&2; exit 1; }
  b="$(blockers <<<"$rec")"
  if [[ -n $b ]]; then
    printf 'demo-freeze: 동결하지 않음. 먼저 고칠 것:\n%s\n' "$b" >&2
    exit 1
  fi
  (cd "$REPO" && git fetch -q origin main && git cat-file -e "$commit^{commit}") \
    || { echo "demo-freeze: 커밋 $commit이 로컬에 없음(git fetch origin main)" >&2; exit 1; }
  dir="$FREEZE_DIR/$name"; mkdir -p "$dir"
  jq --arg tag "$name" '{tag: $tag} + .' <<<"$rec" >"$dir/record.json"
  {
    printf '# %s\n\n기록 %s. 배포 커밋 `%s`(origin main = Argo CD 동기화 revision). 명령 `deploy/scripts/demo-freeze.sh create`.\n\n' \
      "$name" "$(jq -r .collected_at <<<"$rec")" "$commit"
    printf '## Argo CD\n\n| 앱 | 동기화 | 건강 | revision | 남은 OutOfSync |\n|---|---|---|---|---|\n'
    jq -r '.argo | to_entries[] | "| \(.key) | \(.value.sync) | \(.value.health) | \(.value.revision // "-" | .[0:8]) | \(.value.out_of_sync | join(", ")) |"' <<<"$rec"
    printf '\n## 이미지(spec, 실행 중 digest)\n\n| 워크로드 | spec 이미지 | 실행 중 imageID |\n|---|---|---|\n'
    jq -r '.workloads | to_entries[] | "| \(.key) | \(.value.images | map(split("/") | last) | join("<br>")) | \(.value.running | map(split("@") | last | .[0:19]) | join("<br>")) |"' <<<"$rec"
    printf '\n## ConfigMap(해시 이름)\n\n'
    jq -r '.workloads | to_entries[] | select(.value.configmaps | length > 0) | "- \(.key): \(.value.configmaps | join(", "))"' <<<"$rec"
  } >"$dir/SUMMARY.md"
  (cd "$REPO" && jq -c '{tag, collected_at, origin_main, argo: (.argo | map_values({sync, health, revision}))}' "$dir/record.json" \
    | git tag -a "$name" -F - "$commit")
  echo "demo-freeze: 태그 $name → ${commit:0:12} (로컬). 기록 docs/evidence/freeze/$name/. push: git push origin $name"
}

cmd_check() {
  local tag="" tsv=0 rec now st
  while (($#)); do case $1 in --tsv) tsv=1 ;; *) tag=$1 ;; esac; shift; done
  tag="${tag:-$(cd "$REPO" && git tag -l 'demo-freeze-*' --sort=-creatordate | head -n 1)}"
  [[ -n $tag && -f $FREEZE_DIR/$tag/record.json ]] || { echo "demo-freeze: 기록 없음 ${tag:-(태그 없음)}" >&2; exit 2; }
  rec="$FREEZE_DIR/$tag/record.json"
  now="$(collect)"
  out() { if ((tsv)); then printf '%s\tfreeze\t%s\t%s\n' "$1" "$2" "$3"; else printf '%-4s  %s: %s\n' "$1" "$2" "$3"; fi; }
  local commit live changed
  commit="$(jq -r .origin_main "$rec")"
  live="$(jq -r '[.argo[] | .revision // empty | select(test("^[0-9a-f]{40}$"))] | unique | join(",")' <<<"$now")"
  if [[ $live == "$commit" ]]; then out PASS "Argo revision" "$tag 커밋 ${commit:0:8} 그대로"
  else
    changed="$(cd "$REPO" && git fetch -q origin main 2>/dev/null; git diff --name-only "$commit" "${live%%,*}" -- deploy/ 2>/dev/null | head -n 5 | tr '\n' ' ')"
    if [[ -z $changed && $live != *,* ]]; then out WARN "Argo revision" "${commit:0:8} → ${live:0:8}, deploy/ 변경 없음(문서만)"
    else out FAIL "Argo revision" "${commit:0:8} → ${live//,/ }, deploy/ 변경: ${changed:-확인 못 함}"; fi
  fi
  while IFS=$'\t' read -r st key detail; do out "$st" "$key" "$detail"; done < <(jq -r --slurpfile r "$rec" '
    ($r[0].workloads) as $old | .workloads as $new
    | ($old | keys) + ($new | keys) | unique[] as $k
    | if ($old[$k] == null) then ["WARN", $k, "동결 뒤 새로 생김"]
      elif ($new[$k] == null) then ["FAIL", $k, "동결 때 있었는데 지금 없음"]
      elif ($old[$k].images != $new[$k].images) then ["FAIL", $k, "spec 이미지 다름: \($new[$k].images | map(split("/") | last) | join(" "))"]
      elif ($old[$k].configmaps != $new[$k].configmaps) then ["FAIL", $k, "ConfigMap 다름: \($new[$k].configmaps | join(" "))"]
      elif (($new[$k].running - $old[$k].running) | length) > 0 then ["FAIL", $k, "실행 중 digest가 기록에 없음: \(($new[$k].running - $old[$k].running) | map(split("@") | last | .[0:19]) | join(" "))"]
      else empty end | @tsv' <<<"$now")
  out PASS "워크로드 비교" "$(jq '.workloads | length' "$rec")개를 기록과 비교(위에 없는 것은 같음)"
}

case ${1-} in
  create) shift; cmd_create "$@" ;;
  check) shift; cmd_check "$@" ;;
  collect) collect ;;
  *) sed -n '2,17p' "$0"; exit 2 ;;
esac
