#!/usr/bin/env bash
# 증거를 남기는 실행마다 그때의 플랫폼 배포 상태를 함께 적습니다(B20: 게이트 증거가 OutOfSync 때 찍힘).
# 읽기만 합니다. Argo CD 앱마다 동기화 · 건강 · revision, origin main, bravo-control 이미지 digest.
#
#   deploy/scripts/argo-state.sh            사람이 읽는 몇 줄(증거 폴더의 argo-state.txt로 저장해 씀)
#   deploy/scripts/argo-state.sh --tsv      STATUS argo CHECK DETAIL 줄(deploy/scripts/preflight.sh가 씀)
#
# 판정: 건강하지 않으면 FAIL. git 앱의 revision이 origin main과 다르면 FAIL. OutOfSync가 prune 대상 고아나
# Cilium Helm 인증서 Secret뿐이면 WARN(배포 차이 아님), 그 밖의 OutOfSync는 FAIL.
# 증거 스크립트에서:  "$REPO/deploy/scripts/argo-state.sh" >"$OUT/argo-state.txt" || true
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/scripts/demo-lib.sh
source "$HERE/demo-lib.sh"
TSV=0; [[ ${1-} == --tsv ]] && TSV=1

apps="$(kc platform -n argocd get applications.argoproj.io -o json 2>/dev/null)" || {
  if ((TSV)); then printf 'FAIL\targo\tArgo CD\t앱 목록 실패(%s)\n' "$PLATFORM_CTX"; else echo "argo-state: 앱 목록 실패($PLATFORM_CTX)"; fi
  exit 0
}
main="$(cd "$REPO" && timeout 15 git ls-remote origin refs/heads/main 2>/dev/null | cut -f1)"
head="$(cd "$REPO" && git rev-parse HEAD 2>/dev/null)"
revs="$(jq -r '[.items[].status.sync.revision // empty | select(test("^[0-9a-f]{40}$"))] | unique | join(",")' <<<"$apps")"
# Behind main only by commits that do not touch what Argo deploys (deploy/platform/): Argo polls every ~3 min, and a
# docs-only push does not change the deployment. Unknown (commit not in this clone) counts as a change.
plat_moved=yes
if [[ -n $main && $revs != *,* && -n $revs && $revs != "$main" ]] \
  && (cd "$REPO" && git cat-file -e "$revs^{commit}" 2>/dev/null && git cat-file -e "$main^{commit}" 2>/dev/null) \
  && [[ -z $(cd "$REPO" && git diff --name-only "$revs" "$main" -- deploy/platform 2>/dev/null) ]]; then plat_moved=no; fi
lines="$(jq -r --arg main "$main" --arg moved "$plat_moved" '
  .items[] | . as $a
  | ([.status.resources[]? | select(.status == "OutOfSync")]) as $oos
  | ([$oos[] | select(.requiresPruning != true)]) as $real
  | ([$real[] | select(($a.metadata.name == "cilium") and .kind == "Secret"
       and (.name | test("^(cilium-ca|hubble-relay-client-certs|hubble-server-certs)$")))]) as $benign
  | (.status.sync.revision // "-") as $rev
  | (($rev | test("^[0-9a-f]{40}$")) and $main != "" and $rev != $main) as $behind
  | (if (.status.health.status // "") != "Healthy" or ($behind and $moved == "yes") then "FAIL"
     elif $behind then "WARN"
     elif .status.sync.status == "Synced" then "PASS"
     elif ($real | length) == ($benign | length) then "WARN"
     else "FAIL" end) as $st
  | [$st, "앱 \(.metadata.name)",
     "\(.status.sync.status)/\(.status.health.status) rev \($rev[0:8])"
     + (if $behind then " (main \($main[0:8])와 다름" + (if $moved == "no" then ", deploy/platform 변경 없음)" else ")" end) else "" end)
     + (if ($oos | length) > 0 then ", OutOfSync \($oos | length)개: "
          + ([$oos[] | "\(.kind)/\(.name)" + (if .requiresPruning then "(prune 대상)" else "" end)] | .[0:4] | join(" "))
          + (if ($benign | length) > 0 and ($real | length) == ($benign | length) then " — Cilium Helm이 비교 때마다 새로 만드는 인증서 Secret" else "" end)
          + (if ($real | length) == 0 then " — 고아 리소스만 남음: argocd app sync \(.metadata.name) --prune" else "" end)
        else "" end)
     + ", 마지막 동기화 \(.status.operationState.finishedAt // "-")"] | @tsv' <<<"$apps")"
if [[ -z $main ]]; then rv="WARN"; rd="origin main을 읽지 못함(git ls-remote). 배포 ${revs:0:8}"
elif [[ $revs == "$main" ]]; then rv="PASS"; rd="origin main ${main:0:8} = 배포 revision"
elif [[ $plat_moved == no ]]; then rv="WARN"; rd="origin main ${main:0:8}, 배포 ${revs:0:8}: 사이에 deploy/platform 변경 없음(Argo가 곧 따라옴)"
else rv="FAIL"; rd="origin main ${main:0:8}, 배포 ${revs//,/ }: deploy/platform이 바뀐 커밋을 아직 배포하지 않음(Argo 동기화 대기 또는 실패)"; fi
rd+=", 로컬 HEAD ${head:0:8}$([[ -n $main && $head != "$main" ]] && echo ' (origin main과 다름)')"

if ((TSV)); then
  while IFS=$'\t' read -r st name detail; do printf '%s\targo\t%s\t%s\n' "$st" "$name" "$detail"; done <<<"$lines"
  printf '%s\targo\tmain revision\t%s\n' "$rv" "$rd"
  exit 0
fi
printf 'argo-state %s, 플랫폼 %s\n' "$(kst_now)" "$PLATFORM_CTX"
printf '%-4s  main revision: %s\n' "$rv" "$rd"
while IFS=$'\t' read -r st name detail; do printf '%-4s  %s: %s\n' "$st" "$name" "$detail"; done <<<"$lines"
printf '판정: %s\n' "$(awk -F'\t' '$1 == "FAIL" {f = 1} $1 == "WARN" {w = 1} END {print f ? "FAIL(동기화 · 건강 · revision 문제가 있는 상태에서 잰 증거)" : (w ? "WARN(배포 차이 없는 OutOfSync만)" : "PASS(모두 Synced · Healthy, main과 같음)")}' <<<"$lines"$'\n'"$rv"$'\t'"main")"
printf 'bravo-control 이미지:\n'
kc platform -n bravo-control get deploy,statefulset -o json 2>/dev/null \
  | jq -r '.items[] | "  \(.kind | ascii_downcase)/\(.metadata.name): \([.spec.template.spec.containers[].image | split("/") | last] | join(" "))"'
