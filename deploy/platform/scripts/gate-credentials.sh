#!/usr/bin/env bash
# Platform gate credentials (FR-6, FR-12, D-26): the gate's OWN least-privilege read identity in every environment,
# separate from the collector. Values exist only in the platform OpenBao and in two Secrets on the platform; they move
# through pipes only (never argv, files, logs). Re-run to rotate the on-prem token (168h) or after a cluster rebuild.
#
#   gate-credentials.sh [step ...]     steps: rbac onprem_token rgw_other db_roles ca secret   (default: all)
#
# Identities (docs/architecture/15-r2-contracts.md §2, deploy/platform/link/gate-rbac/README.md):
#   source-aws    AWS: web identity role bravo-platform-gate-ro-* (infra/envs/platform-aws gate.tf) -> EKS access entry
#                 group bravo-gate (RBAC gate-rbac/source.yaml), S3 GetObject on _gate/canary only, read-only facts.
#                 OBJ-05 other credential: role bravo-platform-gate-other-* (no permissions), AWS_CONFIG_FILE profile.
#   target-onprem K8s: SA kube-system/bravo-gate token (168h) -> secret/envs/target-onprem/k8s-gate -> kubeconfig
#                 Secret bravo-control/gate-onprem-kubeconfig (server = D-16 link dst-k8s). RGW: the app's bucket
#                 credential (secret/envs/target-onprem/object-store, the gate questions are about it) and the
#                 no-grant user bravo-gate-other (copied once from Rook into secret/envs/target-onprem/gate-other-s3).
#   target-gcp    K8s: Google SA bravo-gate-ro via WIF (infra/envs/target-gcp platform_gate_reader.tf), no key.
#                 GCS: the app HMAC (object-store) and the no-permission HMAC (gate-other-s3).
#   every DB      role gate_ro (platform/gate/sql/gate_ro.sql, read-only), password secret/envs/<env>/gate-db.
# Platform objects: Secret bravo-control/gate-credentials (env of the gate), Secret gate-onprem-kubeconfig,
# ConfigMap gate-ca (public CA certificates only). Secrets go by server-side apply (no last-applied annotation).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
KEYS="$REPO/deploy/pilot/scripts/keys.sh"
RBAC="$REPO/deploy/platform/link/gate-rbac"
PLATFORM="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"
SOURCE="${BRAVO_SOURCE_CONTEXT:-bravo-source}"
ONPREM="${BRAVO_ONPREM_CONTEXT:-bravo-onprem}"
GKE_KUBECONFIG="${BRAVO_GKE_KUBECONFIG:-$REPO/infra/envs/target-gcp/.terraform/bravo/kubeconfig}"
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:$PATH"   # gke-gcloud-auth-plugin

log() { printf 'gate-credentials: %s\n' "$*"; }
k() { local ctx=$1; shift; if [[ $ctx == gke ]]; then KUBECONFIG="$GKE_KUBECONFIG" kubectl --context bravo-target-gcp "$@"; else kubectl --context "$ctx" "$@"; fi; }
ssa() { kubectl --context "$PLATFORM" apply --server-side --force-conflicts --field-manager=bravo-secrets -f - >/dev/null; }
ctx_of() { case $1 in source-aws) echo "$SOURCE" ;; target-onprem) echo "$ONPREM" ;; target-gcp) echo gke ;; esac; }

# Client-side SCRAM-SHA-256 verifier of the password on stdin (same as deploy/landing-zone/overlays/eks/install.sh).
scram() {
  perl -MDigest::SHA=hmac_sha256,sha256 -MMIME::Base64=encode_base64 -e '
    local $/; my $pw = <STDIN>;
    die "scram: empty or non-printable password\n" unless defined $pw && $pw =~ /\A[\x21-\x7e]+\z/;
    open(my $rnd, "<:raw", "/dev/urandom") or die; read($rnd, my $salt, 16) == 16 or die; close $rnd;
    my $u = hmac_sha256($salt . pack("N", 1), $pw); my $salted = $u;
    for (2 .. 4096) { $u = hmac_sha256($u, $pw); $salted ^= $u; }
    printf "SCRAM-SHA-256\$4096:%s\$%s:%s", encode_base64($salt, ""),
      encode_base64(sha256(hmac_sha256("Client Key", $salted)), ""), encode_base64(hmac_sha256("Server Key", $salted), "");'
}

step_rbac() {
  local ctx f pinned live
  for pair in "$SOURCE:source" "$ONPREM:onprem" "gke:gke"; do
    ctx=${pair%%:*}; f="$RBAC/${pair#*:}.yaml"
    k "$ctx" apply -f "$f" >/dev/null
    # The NET-PATHS admission policy pins the app image; a new app image fails closed until the pin follows.
    pinned="$(sed -n 's/^  annotations: {bravo.io\/app-image: "\(.*\)"}$/\1/p' "$f")"
    live="$(k "$ctx" -n chat get deploy chat-bravo-demo -o jsonpath='{.spec.template.spec.containers[0].image}')"
    [[ $pinned == "$live" ]] || log "WARNING ${pair#*:}: ValidatingAdmissionPolicy bravo-gate-netprobe pins $pinned but chat-bravo-demo runs $live (NET-PATHS probe Pods are denied until $f is updated)"
  done
  log "RBAC bravo-gate-read + ValidatingAdmissionPolicy bravo-gate-netprobe on source, on-prem, GKE"
}

step_onprem_token() {
  local ca
  ca="$(kubectl config view --raw -o json | jq -r --arg c "$ONPREM" \
    '(.contexts[] | select(.name == $c) | .context.cluster) as $cl | .clusters[] | select(.name == $cl) | .cluster["certificate-authority-data"]')"
  [[ -n $ca && $ca != null ]] || { echo "gate-credentials: no CA for context $ONPREM" >&2; exit 1; }
  "$KEYS" env-delete target-onprem k8s-gate >/dev/null 2>&1 || true
  k "$ONPREM" -n kube-system create token bravo-gate --duration=168h | tr -d '\n' | "$KEYS" env-put target-onprem k8s-gate token
  "$KEYS" env-pipe target-onprem k8s-gate token -- jq -Rs --arg ca "$ca" '{
    apiVersion: "v1", kind: "Secret", type: "Opaque",
    metadata: {name: "gate-onprem-kubeconfig", namespace: "bravo-control",
               labels: {"app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "openbao"}},
    data: {kubeconfig: ({apiVersion: "v1", kind: "Config", "current-context": "target-onprem",
      clusters: [{name: "target-onprem", cluster: {server: "https://dst-k8s.bravo-link.svc.cluster.local:443",
        "tls-server-name": "kubernetes.default.svc", "certificate-authority-data": $ca}}],
      users: [{name: "bravo-gate", user: {token: .}}],
      contexts: [{name: "target-onprem", context: {cluster: "target-onprem", user: "bravo-gate"}}]} | tojson | @base64)}}' \
    | ssa
  log "Secret bravo-control/gate-onprem-kubeconfig from secret/envs/target-onprem/k8s-gate (168h, values not shown)"
}

step_rgw_other() {
  k "$ONPREM" -n rook-ceph get secret rook-ceph-object-user-bravo-bravo-gate-other -o json \
    | jq -c '{access_key_id: (.data.AccessKey | @base64d), secret_access_key: (.data.SecretKey | @base64d)}' \
    | "$KEYS" env-put-json target-onprem gate-other-s3
}

step_db_roles() {
  local env ctx pod
  for env in source-aws target-onprem target-gcp; do
    ctx="$(ctx_of "$env")"
    "$KEYS" env-generate "$env" gate-db password
    pod="$(k "$ctx" -n chat get pod -l cnpg.io/cluster=chat-db,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}')"
    {
      printf "\\\\set gate_ro_verifier '%s'\n" "$("$KEYS" env-pipe "$env" gate-db password -- cat 2>/dev/null | scram)"
      cat "$REPO/platform/gate/sql/gate_ro.sql"
    } | k "$ctx" -n chat exec -i "$pod" -c postgres -- psql -X -q -1 -v ON_ERROR_STOP=1 -d chat -f - >/dev/null
    log "$env: role gate_ro on chat-db ($pod), verifier only"
  done
}

step_ca() {
  local tmp; tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' RETURN
  k "$ONPREM" -n rook-ceph get configmap bravo-rgw-ca -o jsonpath='{.data.ca\.crt}' >"$tmp/onprem-rgw.crt"
  k "$SOURCE" -n chat get secret chat-db-ca -o jsonpath='{.data.ca\.crt}' | base64 -d >"$tmp/source-aws-chat-db.crt"
  k "$ONPREM" -n chat get secret chat-db-ca -o jsonpath='{.data.ca\.crt}' | base64 -d >"$tmp/target-onprem-chat-db.crt"
  k gke -n chat get secret chat-db-ca -o jsonpath='{.data.ca\.crt}' | base64 -d >"$tmp/target-gcp-chat-db.crt"
  for f in "$tmp"/*.crt; do grep -q 'BEGIN CERTIFICATE' "$f" || { echo "gate-credentials: $f is not a certificate" >&2; exit 1; }; done
  kubectl --context "$PLATFORM" -n bravo-control create configmap gate-ca --from-file="$tmp" --dry-run=client -o yaml \
    | kubectl --context "$PLATFORM" apply --server-side --force-conflicts --field-manager=bravo-secrets -f - >/dev/null
  log "ConfigMap bravo-control/gate-ca (public CA certificates: on-prem RGW, three chat-db CAs)"
}

# One field of secret/envs/ENV/NAME, base64, on stdout (pipe only).
b64() { "$KEYS" env-pipe "$1" "$2" "$3" -- jq -Rrs '@base64' 2>/dev/null; }
# libpq DSN for gate_ro with verify-full against the DB's own name; the gate dials the link (registry dial_host).
dsn() { # env
  "$KEYS" env-pipe "$1" gate-db password -- jq -Rrs --arg ca "/etc/bravo/gate-ca/$1-chat-db.crt" \
    '"host=chat-db-rw.chat.svc port=5432 dbname=chat user=gate_ro password=" + . + " sslmode=verify-full sslrootcert=" + $ca + " application_name=bravo-gate" | @base64' 2>/dev/null
}

step_secret() {
  local data='{}' kv name val
  for kv in "SRC_PG_DSN dsn source-aws" "ONPREM_PG_DSN dsn target-onprem" "GCP_PG_DSN dsn target-gcp" \
            "ONPREM_S3_AK b64 target-onprem object-store access_key_id" "ONPREM_S3_SK b64 target-onprem object-store secret_access_key" \
            "ONPREM_OTHER_AK b64 target-onprem gate-other-s3 access_key_id" "ONPREM_OTHER_SK b64 target-onprem gate-other-s3 secret_access_key" \
            "GCS_AK b64 target-gcp object-store access_key_id" "GCS_SK b64 target-gcp object-store secret_access_key" \
            "GCS_OTHER_AK b64 target-gcp gate-other-s3 access_key_id" "GCS_OTHER_SK b64 target-gcp gate-other-s3 secret_access_key"; do
    read -r name fn a1 a2 a3 <<<"$kv"
    if [[ $fn == dsn ]]; then val="$(dsn "$a1")"; else val="$(b64 "$a1" "$a2" "$a3")"; fi
    [[ -n $val ]] || { echo "gate-credentials: no value for $name" >&2; exit 1; }
    data="$(printf '%s\n%s\n' "$data" "$val" | jq -c -s --arg k "$name" -R 'split("\n") | (.[0] | fromjson) + {($k): .[1]}')"
  done
  printf '%s' "$data" | jq '{apiVersion: "v1", kind: "Secret", type: "Opaque",
    metadata: {name: "gate-credentials", namespace: "bravo-control",
               labels: {"app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "openbao"}}, data: .}' | ssa
  data=""; val=""
  log "Secret bravo-control/gate-credentials: 11 keys from OpenBao (values not shown)"
}

steps=("$@"); ((${#steps[@]})) || steps=(rbac onprem_token rgw_other db_roles ca secret)
for s in "${steps[@]}"; do "step_$s"; done
