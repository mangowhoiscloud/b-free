#!/usr/bin/env bash
# Writes apps/demo/pilot-app.env from pilot-app.env.example and the Terraform outputs (read-only:
# `terraform output` reads the state, it never plans or applies).
#   source app: infra/envs/source-aws   outputs attachments_bucket, attachments_bucket_region
#   dest app:   infra/envs/target-onprem outputs attachments_bucket, attachments_bucket_region,
#               attachments_endpoint (proposed names; skipped until the folder has a state)
# An output that is not available stays empty, which turns attachments off for that instance.
# The RGW keys are not Terraform outputs; set APP_DST_S3_ACCESS_KEY_ID / APP_DST_S3_SECRET_ACCESS_KEY
# in the generated file from the landing zone Secret.
set -Eeuo pipefail
DEMO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$DEMO/../.." && pwd)"
OUT="${1:-$DEMO/pilot-app.env}"

tf_out() {  # tf_out <env dir> <output name>
  local dir="$REPO/infra/envs/$1"
  [ -d "$dir/.terraform" ] || return 0
  terraform -chdir="$dir" output -raw "$2" 2>/dev/null || true
}

src_bucket="$(tf_out source-aws attachments_bucket)"
src_region="$(tf_out source-aws attachments_bucket_region)"
dst_bucket="$(tf_out target-onprem attachments_bucket)"
dst_region="$(tf_out target-onprem attachments_bucket_region)"
dst_endpoint="$(tf_out target-onprem attachments_endpoint)"

umask 077
sed -e "s|^APP_SRC_S3_BUCKET=.*|APP_SRC_S3_BUCKET=${src_bucket}|" \
    -e "s|^APP_SRC_S3_REGION=.*|APP_SRC_S3_REGION=${src_region}|" \
    -e "s|^APP_DST_S3_BUCKET=.*|APP_DST_S3_BUCKET=${dst_bucket}|" \
    -e "s|^APP_DST_S3_REGION=.*|APP_DST_S3_REGION=${dst_region}|" \
    -e "s|^APP_DST_S3_ENDPOINT_URL=.*|APP_DST_S3_ENDPOINT_URL=${dst_endpoint}|" \
    "$DEMO/pilot-app.env.example" > "$OUT"
echo "wrote $OUT"
echo "  source bucket: ${src_bucket:-<not available: attachments off>} (${src_region:-no region})"
echo "  dest bucket:   ${dst_bucket:-<not available: attachments off>} ${dst_endpoint:+at $dst_endpoint}"
