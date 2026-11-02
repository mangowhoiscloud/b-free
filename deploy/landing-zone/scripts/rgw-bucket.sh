#!/usr/bin/env bash
# Configure the attachments bucket on RGW as its owner (keys generated in-cluster by the OBC):
# Block Public Access, a TLS-only bucket policy (aws:SecureTransport with BoolIfExists), and the
# platform canary object _gate/canary that the gate's OBJ probes need. Keys reach the aws CLI only
# through its environment, read from the cluster on a pipe; nothing is written to disk.
#   rgw-bucket.sh <kube-context>
set -euo pipefail
ctx=$1 ns=chat obc=bravo-demo-attachments bucket=bravo-demo-attachments port=${RGW_PF_PORT:-18443}
k() { kubectl --context "$ctx" "$@"; }
work="$(mktemp -d)"; chmod 700 "$work"
k -n "$ns" port-forward svc/rook-ceph-rgw-bravo "$port:443" -n rook-ceph >"$work/pf.log" 2>&1 &
pf=$!
trap 'kill $pf 2>/dev/null || true; rm -rf "$work"' EXIT
k -n rook-ceph get configmap bravo-rgw-ca -o jsonpath='{.data.ca\.crt}' >"$work/ca.crt"   # public CA cert
for _ in $(seq 1 30); do grep -q Forwarding "$work/pf.log" && break; sleep 0.5; done

s3() {
  AWS_ACCESS_KEY_ID="$(k -n "$ns" get secret "$obc" -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' | base64 -d)" \
  AWS_SECRET_ACCESS_KEY="$(k -n "$ns" get secret "$obc" -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' | base64 -d)" \
  AWS_REGION=us-east-1 AWS_CA_BUNDLE="$work/ca.crt" AWS_REQUEST_CHECKSUM_CALCULATION=when_required \
  AWS_RESPONSE_CHECKSUM_VALIDATION=when_required AWS_PAGER="" \
    aws --endpoint-url "https://localhost:$port" "$@"
}

policy="$(jq -nc --arg b "$bucket" '{Version: "2012-10-17", Statement: [{
  Sid: "DenyInsecureTransport", Effect: "Deny", Principal: "*", Action: "s3:*",
  Resource: ["arn:aws:s3:::\($b)", "arn:aws:s3:::\($b)/*"],
  Condition: {BoolIfExists: {"aws:SecureTransport": "false"}}}]}')"

s3 s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
s3 s3api put-bucket-policy --bucket "$bucket" --policy "$policy"
printf 'bravo gate canary %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$work/canary.txt"   # not secret
s3 s3api put-object --bucket "$bucket" --key _gate/canary --body "$work/canary.txt" --content-type text/plain >/dev/null
echo "rgw-bucket: public access block:"; s3 s3api get-public-access-block --bucket "$bucket" --output json | jq -c .
echo "rgw-bucket: bucket policy:"; s3 s3api get-bucket-policy --bucket "$bucket" --output json | jq -r .Policy | jq -c .
echo "rgw-bucket: canary:"; s3 s3api head-object --bucket "$bucket" --key _gate/canary --output json | jq -c '{ContentLength, ETag}'
