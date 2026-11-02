#!/usr/bin/env bash
# DR-01 restore check (gate DR-RESTORE, DR-ARCHIVE) on one environment where CNPG runs chat/chat-db.
#   dr-restore.sh <onprem|source|gke> [step ...]      steps: backup mark restore verify (default: all four)
# backup   Backup object chat-db-<UTC stamp> (method barmanObjectStore); waits for phase completed.
# mark     one transaction on the chat-db primary: LOCK every user table IN SHARE MODE (writers wait a few ms),
#          count rows, pg_create_restore_point('dr-<stamp>'), pg_current_wal_lsn(); COMMIT; pg_switch_wal(); then
#          waits until that WAL segment is archived. The counts are exactly the data at the restore point.
# restore  replaces Cluster chat-db-restore (base/chat-db-restore.yaml) with bootstrap.recovery from that Backup,
#          recoveryTarget.targetName = the restore point; waits for "Cluster in healthy state". The source counts
#          go on the Cluster as annotation bravo.dev/source-counts (not secret), so verify can run later.
# verify   counts the same tables on chat-db-restore and prints source vs restore; exit 1 on any difference.
# A later run of restore/verify alone takes BACKUP_NAME and RESTORE_POINT from the environment.
# Output is plain text with KST times (evidence: docs/evidence/gate/dr-restore-<env>-*/). No secret is read:
# psql runs as postgres over the pod's local socket (kubectl exec), like the schema steps of the installers.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lz="$(cd "$here/.." && pwd)"
repo="$(cd "$lz/../.." && pwd)"
env=${1:?usage: dr-restore.sh <onprem|source|gke> [backup mark restore verify]}
shift
case $env in
  onprem) kc=(--context "${BRAVO_ONPREM_CONTEXT:-bravo-onprem}"); restore_size=1Gi ;;
  source) kc=(--context "${BRAVO_SOURCE_CONTEXT:-bravo-source}"); restore_size=1Gi ;;
  gke)
    # gke-gcloud-auth-plugin lives in the gcloud SDK bin directory (infra/envs/target-gcp/scripts/lib.sh).
    gcloud_bin="$(dirname "$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$(command -v gcloud)")")"
    export PATH="$gcloud_bin:$PATH" USE_GKE_GCLOUD_AUTH_PLUGIN=True
    kc=(--kubeconfig "${BRAVO_GKE_KUBECONFIG:-$repo/infra/envs/target-gcp/.terraform/bravo/kubeconfig}"
        --context "${BRAVO_GKE_CONTEXT:-bravo-target-gcp}")
    restore_size=4Gi ;;   # Hyperdisk Balanced minimum
  *) echo "dr-restore.sh: env must be onprem, source or gke" >&2; exit 2 ;;
esac
k() { kubectl "${kc[@]}" -n chat "$@"; }
kst() { TZ=Asia/Seoul date '+%F %T KST'; }
log() { printf '\n== %s dr-restore[%s] %s\n' "$(kst)" "$env" "$*"; }
die() { printf 'dr-restore.sh: %s\n' "$*" >&2; exit 1; }
primary() { k get cluster "$1" -o jsonpath='{.status.currentPrimary}'; }
psql_on() { local pod=$1; shift; k exec -i "$pod" -c postgres -- psql -X -v ON_ERROR_STOP=1 -d chat "$@"; }

stamp="$(date -u +%Y%m%dt%H%M%Sz)"
BACKUP_NAME=${BACKUP_NAME:-}
RESTORE_POINT=${RESTORE_POINT:-}
SOURCE_COUNTS=${SOURCE_COUNTS:-}

step_backup() {
  BACKUP_NAME="chat-db-$stamp"
  log "Backup $BACKUP_NAME (cluster chat-db, method barmanObjectStore)"
  k get cluster chat-db -o jsonpath='{.spec.backup.barmanObjectStore.destinationPath}{"\n"}' | sed 's/^/destinationPath: /'
  printf '%s\n' "apiVersion: postgresql.cnpg.io/v1" "kind: Backup" \
    "metadata: {name: $BACKUP_NAME, namespace: chat, labels: {bravo.dev/dr-check: \"true\"}}" \
    "spec: {cluster: {name: chat-db}, method: barmanObjectStore}" | k apply -f -
  local phase=""
  for _ in $(seq 1 180); do
    phase="$(k get backup "$BACKUP_NAME" -o jsonpath='{.status.phase}')"
    [[ $phase == completed || $phase == failed ]] && break
    sleep 5
  done
  k get backup "$BACKUP_NAME" -o json | jq '{name: .metadata.name, phase: .status.phase, method: .status.method,
    backupId: .status.backupId, destinationPath: .status.destinationPath, serverName: .status.serverName,
    beginWal: .status.beginWal, endWal: .status.endWal, beginLSN: .status.beginLSN, endLSN: .status.endLSN,
    startedAt: .status.startedAt, stoppedAt: .status.stoppedAt, error: .status.error}'
  [[ $phase == completed ]] || die "backup $BACKUP_NAME: phase $phase"
  local stopped epoch
  stopped="$(k get backup "$BACKUP_NAME" -o jsonpath='{.status.stoppedAt}')"
  epoch="$(date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$stopped" +%s 2>/dev/null || date -u -d "$stopped" +%s)"
  echo "completed: $stopped = $(TZ=Asia/Seoul date -r "$epoch" '+%F %T KST' 2>/dev/null || TZ=Asia/Seoul date -d "@$epoch" '+%F %T KST')"
}

step_mark() {
  RESTORE_POINT="dr-$stamp"
  local pod out lsn seg
  pod="$(primary chat-db)"
  log "restore point $RESTORE_POINT on chat-db primary $pod (tables locked IN SHARE MODE while counted)"
  out="$(psql_on "$pod" -At -F '|' <<SQL
SET lock_timeout = '10s';
BEGIN;
SELECT format('LOCK TABLE %s IN SHARE MODE', string_agg(format('%I.%I', schemaname, relname), ', ' ORDER BY relname))
  FROM pg_stat_user_tables \gexec
SELECT format('SELECT %L AS t, count(*) FROM %I.%I', schemaname || '.' || relname, schemaname, relname)
  FROM pg_stat_user_tables ORDER BY schemaname, relname \gexec
SELECT 'restore_point', pg_create_restore_point('$RESTORE_POINT'), pg_current_wal_insert_lsn(), now();
COMMIT;
SELECT 'switch_wal', pg_switch_wal();
SQL
)"
  echo "table|rows   (restore_point|<restore point lsn>|<insert lsn>|<time>)"
  printf '%s\n' "$out" | grep -Ev '^(LOCK TABLE|SET|BEGIN|COMMIT)$'
  lsn="$(awk -F'|' '$1=="restore_point"{print $2}' <<<"$out")"
  SOURCE_COUNTS="$(awk -F'|' '$1!="restore_point" && $1!="switch_wal" && NF==2 {print $1"|"$2}' <<<"$out" \
    | jq -Rn '[inputs | split("|") | {(.[0]): (.[1] | tonumber)}] | add')"
  seg="$(psql_on "$pod" -At -c "select pg_walfile_name('$lsn')")"
  echo "restore point lsn $lsn, WAL segment $seg; waiting until it is archived"
  for _ in $(seq 1 120); do
    [[ "$(psql_on "$pod" -At -c "select coalesce(last_archived_wal, '') >= '$seg' from pg_stat_archiver")" == t ]] && break
    sleep 5
  done
  psql_on "$pod" -c "select archived_count, last_archived_wal, last_archived_time, failed_count, last_failed_wal, last_failed_time from pg_stat_archiver"
  [[ "$(psql_on "$pod" -At -c "select coalesce(last_archived_wal, '') >= '$seg' from pg_stat_archiver")" == t ]] \
    || die "WAL $seg not archived"
}

step_restore() {
  [[ -n $BACKUP_NAME && -n $RESTORE_POINT && -n $SOURCE_COUNTS ]] \
    || die "restore needs BACKUP_NAME, RESTORE_POINT and SOURCE_COUNTS (run backup and mark first)"
  log "Cluster chat-db-restore <- Backup $BACKUP_NAME, recoveryTarget.targetName $RESTORE_POINT"
  if k get cluster chat-db-restore >/dev/null 2>&1; then
    echo "replacing the previous chat-db-restore ($(k get cluster chat-db-restore -o jsonpath='{.metadata.annotations.bravo\.dev/source-backup}'))"
    k delete cluster chat-db-restore --wait=true --timeout=5m
    for _ in $(seq 1 60); do [[ -z $(k get pvc -l cnpg.io/cluster=chat-db-restore -o name) ]] && break; sleep 5; done
  fi
  local live doc backup_id
  live="$(k get cluster chat-db -o json)"
  backup_id="$(k get backup "$BACKUP_NAME" -o jsonpath='{.status.backupId}')"
  [[ -n $backup_id ]] || die "Backup $BACKUP_NAME has no status.backupId"
  doc="$(PG_IMAGE="$(jq -r .spec.imageName <<<"$live")" BACKUP_NAME="$BACKUP_NAME" RESTORE_POINT="$RESTORE_POINT" \
    BACKUP_ID="$backup_id" RESTORE_STORAGE_SIZE="$restore_size" \
    envsubst '${PG_IMAGE} ${BACKUP_NAME} ${RESTORE_POINT} ${BACKUP_ID} ${RESTORE_STORAGE_SIZE}' <"$lz/base/chat-db-restore.yaml" \
    | kubectl "${kc[@]}" create --dry-run=client -o json -f -)"
  doc="$(jq --argjson live "$live" --arg counts "$(jq -c . <<<"$SOURCE_COUNTS")" '
    .spec.postgresql.parameters = (($live.spec.postgresql.parameters // {}) + .spec.postgresql.parameters)
    | .metadata.annotations["bravo.dev/source-counts"] = $counts' <<<"$doc")"
  jq '{metadata: {name: .metadata.name, annotations: .metadata.annotations}, spec: {imageName: .spec.imageName,
    bootstrap: .spec.bootstrap, storage: .spec.storage, parameters: .spec.postgresql.parameters}}' <<<"$doc"
  k apply -f - <<<"$doc"
  local s=""
  for _ in $(seq 1 240); do
    s="$(k get cluster chat-db-restore -o jsonpath='{.status.phase}|{.status.readyInstances}')"
    [[ $s == "Cluster in healthy state|1" ]] && break
    sleep 5
  done
  k get cluster chat-db-restore -o json | jq '{name: .metadata.name, phase: .status.phase, readyInstances: .status.readyInstances,
    currentPrimary: .status.currentPrimary, timeline: .status.timelineID, bootstrap_recovery: .spec.bootstrap.recovery,
    conditions: [.status.conditions[]? | {type, status, reason}]}'
  [[ $s == "Cluster in healthy state|1" ]] || die "chat-db-restore not healthy: $s"
  echo "healthy at $(kst)"
}

step_verify() {
  local pod src res
  pod="$(primary chat-db-restore)"
  src="${SOURCE_COUNTS:-$(k get cluster chat-db-restore -o jsonpath='{.metadata.annotations.bravo\.dev/source-counts}')}"
  [[ -n $src ]] || die "no source counts"
  log "row counts: chat-db at restore point $(k get cluster chat-db-restore -o jsonpath='{.metadata.annotations.bravo\.dev/restore-point}') vs chat-db-restore ($pod)"
  psql_on "$pod" -c "select pg_is_in_recovery() as in_recovery, timeline_id from pg_control_checkpoint()" \
    -c "select subname, subenabled from pg_subscription" -c "select count(*) as apply_workers from pg_stat_subscription where pid is not null" \
    -c "show max_logical_replication_workers"
  res="$(psql_on "$pod" -At -F '|' <<'SQL'
SELECT format('SELECT %L AS t, count(*) FROM %I.%I', schemaname || '.' || relname, schemaname, relname)
  FROM pg_stat_user_tables ORDER BY schemaname, relname \gexec
SQL
)"
  res="$(jq -Rn '[inputs | select(length > 0) | split("|") | {(.[0]): (.[1] | tonumber)}] | add' <<<"$res")"
  jq -rn --argjson s "$src" --argjson r "$res" '
    (["table", "chat-db (source)", "chat-db-restore", "equal"] | @tsv),
    ((($s | keys) + ($r | keys) | unique)[] as $t | [$t, ($s[$t] // "missing"), ($r[$t] // "missing"), ($s[$t] == $r[$t])] | @tsv)' \
    | column -t -s $'\t'
  jq -en --argjson s "$src" --argjson r "$res" '$s == $r' >/dev/null && echo "RESULT: all row counts equal" \
    || { echo "RESULT: row counts differ"; return 1; }
}

steps=("$@"); ((${#steps[@]})) || steps=(backup mark restore verify)
for s in "${steps[@]}"; do "step_$s"; done
