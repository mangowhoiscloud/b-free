#!/usr/bin/env bash
# Backup bucket for CNPG chat-db on RGW (DR-01): user chat-db-backup (overlays/rke2/rook/chat-db-backup.yaml) owns
# bucket bravo-chat-db-backup. Block Public Access and a TLS-only bucket policy, like the attachments bucket.
# Keys (D-26): generated once in the platform OpenBao secret/envs/<env>/chat-db-backup (fields access_key_id,
# secret_access_key; keys.sh env-generate keeps an existing value) and piped into
#   rook-ceph/chat-db-backup-keys  (access-key, secret-key)            <- the RGW user reads it (spec.keys)
#   chat/chat-db-backup-s3         (ACCESS_KEY_ID, ACCESS_SECRET_KEY)  <- Cluster chat-db barmanObjectStore
# The RGW CA (public) goes to Secret chat/chat-db-backup-ca (CNPG endpointCA takes a Secret, not a ConfigMap).
# Nothing is written to disk or argv. Run after install.sh steps rgw_tls and rgw; re-runs are idempotent.
#   rgw-backup-bucket.sh <kube-context> [env-id]
set -euo pipefail
ctx=$1 env=${2:-target-onprem}
user=chat-db-backup bucket=bravo-chat-db-backup port=${RGW_PF_PORT:-18445}
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
keys="$(cd "$here/../../pilot/scripts" && pwd)/keys.sh"
k() { kubectl --context "$ctx" "$@"; }

"$keys" env-generate "$env" chat-db-backup access_key_id 10
"$keys" env-generate "$env" chat-db-backup secret_access_key 20

# secret <ns> <name> <k8s key>=<OpenBao field> ...  (values on pipes only)
secret() {
  local ns=$1 name=$2 spec; shift 2
  for spec in "$@"; do
    printf '%s\t' "${spec%%=*}"
    "$keys" env-pipe "$env" chat-db-backup "${spec#*=}" -- base64 | tr -d '\n'
    printf '\n'
  done | jq -Rn --arg ns "$ns" --arg name "$name" \
    '{apiVersion: "v1", kind: "Secret", type: "Opaque", metadata: {name: $name, namespace: $ns},
      data: ([inputs | split("\t") | {(.[0]): .[1]}] | add)}' | k apply -f - >/dev/null
  echo "secret $ns/$name <- OpenBao secret/envs/$env/chat-db-backup (values not shown)"
}
secret rook-ceph chat-db-backup-keys access-key=access_key_id secret-key=secret_access_key
secret chat chat-db-backup-s3 ACCESS_KEY_ID=access_key_id ACCESS_SECRET_KEY=secret_access_key
k -n rook-ceph get configmap bravo-rgw-ca -o jsonpath='{.data.ca\.crt}' \
  | k -n chat create secret generic chat-db-backup-ca --from-file=ca.crt=/dev/stdin --dry-run=client -o yaml \
  | k apply -f - >/dev/null
echo "secret chat/chat-db-backup-ca <- ConfigMap rook-ceph/bravo-rgw-ca (public CA)"

k apply -f "$here/../overlays/rke2/rook/chat-db-backup.yaml" >/dev/null
k -n rook-ceph wait --for=jsonpath='{.status.phase}'=Ready "cephobjectstoreuser/$user" --timeout=5m >/dev/null

work="$(mktemp -d)"; chmod 700 "$work"
k -n rook-ceph port-forward svc/rook-ceph-rgw-bravo "$port:443" >"$work/pf.log" 2>&1 &
pf=$!
trap 'kill $pf 2>/dev/null || true; rm -rf "$work"' EXIT
k -n rook-ceph get configmap bravo-rgw-ca -o jsonpath='{.data.ca\.crt}' >"$work/ca.crt"   # public CA cert
for _ in $(seq 1 30); do grep -q Forwarding "$work/pf.log" && break; sleep 0.5; done

s3() {
  AWS_ACCESS_KEY_ID="$(k -n chat get secret chat-db-backup-s3 -o jsonpath='{.data.ACCESS_KEY_ID}' | base64 -d)" \
  AWS_SECRET_ACCESS_KEY="$(k -n chat get secret chat-db-backup-s3 -o jsonpath='{.data.ACCESS_SECRET_KEY}' | base64 -d)" \
  AWS_REGION=us-east-1 AWS_CA_BUNDLE="$work/ca.crt" AWS_REQUEST_CHECKSUM_CALCULATION=when_required \
  AWS_RESPONSE_CHECKSUM_VALIDATION=when_required AWS_PAGER="" AWS_CONFIG_FILE=/dev/null \
  AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    env -u AWS_PROFILE -u AWS_SESSION_TOKEN aws --endpoint-url "https://localhost:$port" "$@"
}
if s3 s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then echo "rgw-backup-bucket: $bucket exists; kept"
else s3 s3api create-bucket --bucket "$bucket" >/dev/null; echo "rgw-backup-bucket: $bucket created (owner $user)"; fi

policy="$(jq -nc --arg b "$bucket" '{Version: "2012-10-17", Statement: [{
  Sid: "DenyInsecureTransport", Effect: "Deny", Principal: "*", Action: "s3:*",
  Resource: ["arn:aws:s3:::\($b)", "arn:aws:s3:::\($b)/*"],
  Condition: {BoolIfExists: {"aws:SecureTransport": "false"}}}]}')"
s3 s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
s3 s3api put-bucket-policy --bucket "$bucket" --policy "$policy"
echo "rgw-backup-bucket: public access block:"; s3 s3api get-public-access-block --bucket "$bucket" --output json | jq -c .
echo "rgw-backup-bucket: bucket policy:"; s3 s3api get-bucket-policy --bucket "$bucket" --output json | jq -r .Policy | jq -c .
