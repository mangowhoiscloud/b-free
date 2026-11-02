#!/usr/bin/env bash
# Publish the platform RKE2 service-account JWKS (public keys only) to the OIDC discovery bucket, so STS can
# verify projected tokens for AssumeRoleWithWebIdentity (infra/envs/platform-aws rke2.tf). Re-run after the
# service-account signing key changes. The discovery document itself is managed by Terraform.
set -euo pipefail
CTX="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"
TF="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../infra/envs/platform-aws" && pwd)"
BUCKET="$(terraform -chdir="$TF" output -raw sa_oidc_bucket)"
ISSUER="$(terraform -chdir="$TF" output -raw sa_oidc_issuer)"
got="$(kubectl --context "$CTX" get --raw /.well-known/openid-configuration | jq -r .issuer)"
[[ $got == "$ISSUER" ]] || { echo "publish-sa-jwks: cluster issuer $got != $ISSUER" >&2; exit 1; }
kubectl --context "$CTX" get --raw /openid/v1/jwks | jq -e '.keys | length > 0' >/dev/null
kubectl --context "$CTX" get --raw /openid/v1/jwks \
  | aws s3 cp - "s3://$BUCKET/openid/v1/jwks" --content-type application/json --only-show-errors
curl -sf "$ISSUER/openid/v1/jwks" | jq -c '{published_kids: [.keys[].kid]}'
