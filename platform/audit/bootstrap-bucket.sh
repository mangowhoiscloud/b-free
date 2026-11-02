#!/usr/bin/env bash
# T24 (P1): create the platform audit bucket with S3 Object Lock in the source AWS account (component=platform).
# Idempotent: an existing bucket with Object Lock enabled is checked and its settings re-applied.
#
#   bootstrap-bucket.sh <bucket-name> [region]         e.g. bravo-platform-audit-$(openssl rand -hex 6)
#
# Settings (all through the S3 API, so the same calls work on Rook-Ceph RGW with --endpoint-url):
#   Object Lock: default retention COMPLIANCE, ${BRAVO_AUDIT_RETENTION_DAYS:-1} day(s). Nobody, root included,
#                can delete or shorten a locked version before it expires. Keep the value small for the demo:
#                the bucket can only be emptied after the last retention ends.
#   Versioning:  on (required by Object Lock)
#   Public access block: all four on. Encryption: SSE-S3 (AES256) by default.
#   Bucket policy: deny any request without TLS. BoolIfExists, so RGW (where plain-HTTP requests lack the
#                  aws:SecureTransport key) also denies plaintext (review condition 2).
#   Tags: component=platform, env=platform, managed-by=platform/audit/bootstrap-bucket.sh
set -euo pipefail
BUCKET=${1:?usage: bootstrap-bucket.sh <bucket-name> [region]}
REGION=${2:-${AWS_REGION:-ap-northeast-2}}
DAYS=${BRAVO_AUDIT_RETENTION_DAYS:-1}
EP=()
if [[ -n ${BRAVO_AUDIT_S3_ENDPOINT:-} ]]; then EP=(--endpoint-url "$BRAVO_AUDIT_S3_ENDPOINT"); fi
[[ $BUCKET =~ ^[a-z0-9][a-z0-9-]{2,62}$ ]] || { echo "bad bucket name: $BUCKET" >&2; exit 2; }
[[ $DAYS =~ ^[0-9]+$ && $DAYS -ge 1 ]] || { echo "BRAVO_AUDIT_RETENTION_DAYS must be >= 1" >&2; exit 2; }
s3() { aws s3api ${EP[@]+"${EP[@]}"} --region "$REGION" "$@"; }

if s3 head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "bucket exists: $BUCKET"
  s3 get-object-lock-configuration --bucket "$BUCKET" >/dev/null \
    || { echo "existing bucket has no Object Lock; refusing (Object Lock can only be set at creation on AWS)" >&2; exit 1; }
else
  if [[ $REGION == us-east-1 ]]; then
    s3 create-bucket --bucket "$BUCKET" --object-lock-enabled-for-bucket >/dev/null
  else
    s3 create-bucket --bucket "$BUCKET" --object-lock-enabled-for-bucket \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
  echo "created: $BUCKET ($REGION) with Object Lock"
fi
s3 put-bucket-tagging --bucket "$BUCKET" --tagging \
  'TagSet=[{Key=component,Value=platform},{Key=env,Value=platform},{Key=managed-by,Value=platform/audit/bootstrap-bucket.sh}]'
s3 put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
s3 put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":false}]}'
s3 put-object-lock-configuration --bucket "$BUCKET" --object-lock-configuration \
  "{\"ObjectLockEnabled\":\"Enabled\",\"Rule\":{\"DefaultRetention\":{\"Mode\":\"COMPLIANCE\",\"Days\":$DAYS}}}"
s3 put-bucket-policy --bucket "$BUCKET" --policy "$(printf '%s' '{"Version":"2012-10-17","Statement":[{"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::BUCKET","arn:aws:s3:::BUCKET/*"],"Condition":{"BoolIfExists":{"aws:SecureTransport":"false"}}}]}' | sed "s/BUCKET/$BUCKET/g")"
echo "settings applied:"
s3 get-object-lock-configuration --bucket "$BUCKET" --output json | jq -c .ObjectLockConfiguration
s3 get-bucket-versioning --bucket "$BUCKET" --output json | jq -c .
s3 get-bucket-tagging --bucket "$BUCKET" --output json | jq -c '[.TagSet[] | "\(.Key)=\(.Value)"]'
