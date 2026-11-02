#!/usr/bin/env bash
# Copy the app images into the GKE target's Artifact Registry BY DIGEST from the source registry (spec §8.8: every side
# runs the same digest; no build for GKE). skopeo copy --preserve-digests --multi-arch all keeps the image index
# digest, so the destination digest equals the source digest; the script checks that after the copy.
#
#   infra/envs/target-gcp/scripts/images.sh            copy what is missing (DEMO_DIGEST, PGB_DIGEST from versions.env)
#   infra/envs/target-gcp/scripts/images.sh --check    only report present / missing
#
# Credentials: the operator's AWS chain (aws ecr get-login-password) and gcloud (print-access-token), handed to
# skopeo as an auth file on a pipe (/dev/fd, process substitution). No token on argv, none written to disk.
# Source registry: SOURCE_REGISTRY (default the source EKS account's ECR, repositories bravo-source/<name>).
source "$(dirname "$0")/lib.sh"
SOURCE_REGISTRY="${SOURCE_REGISTRY:-721622471953.dkr.ecr.ap-northeast-2.amazonaws.com/bravo-source}"
SOURCE_ECR_REGION="${SOURCE_ECR_REGION:-ap-northeast-2}"
check_only=0; [[ ${1-} == --check ]] && check_only=1
command -v skopeo >/dev/null || die "needs skopeo"
load_outputs

auth_json() {
  { aws ecr get-login-password --region "$SOURCE_ECR_REGION"; gcloud auth print-access-token; } | python3 -I -c '
import base64, json, sys
ecr, gcp = sys.stdin.readline().strip(), sys.stdin.readline().strip()
b = lambda u, p: base64.b64encode(f"{u}:{p}".encode()).decode()
print(json.dumps({"auths": {sys.argv[1]: {"auth": b("AWS", ecr)}, sys.argv[2]: {"auth": b("oauth2accesstoken", gcp)}}}))
' "${SOURCE_REGISTRY%%/*}" "${IMAGE_REGISTRY%%/*}"
}

for spec in "$DEMO_IMAGE_NAME@$DEMO_DIGEST" "$PGB_IMAGE_NAME@$PGB_DIGEST"; do
  name=${spec%@*}; digest=${spec#*@}
  if gcloud artifacts docker images describe "$IMAGE_REGISTRY/$spec" --project "$PROJECT" >/dev/null 2>&1; then
    log "present: $IMAGE_REGISTRY/$spec"; continue
  fi
  ((check_only)) && { log "MISSING: $IMAGE_REGISTRY/$spec"; continue; }
  log "copy $SOURCE_REGISTRY/$spec -> $IMAGE_REGISTRY/$name (preserve digests, all platforms)"
  # One pipe per side: skopeo reads the source and destination auth separately (a shared pipe is empty the 2nd time).
  skopeo copy --src-authfile <(auth_json) --dest-authfile <(auth_json) --preserve-digests --multi-arch all --retry-times 2 \
    "docker://$SOURCE_REGISTRY/$spec" "docker://$IMAGE_REGISTRY/$name@$digest" >/dev/null
  got="$(skopeo inspect --raw --authfile <(auth_json) "docker://$IMAGE_REGISTRY/$name@$digest" | shasum -a 256 | cut -d' ' -f1)"
  [[ "sha256:$got" == "$digest" ]] || die "digest after copy sha256:$got != $digest"
  log "copied: $IMAGE_REGISTRY/$name@$digest (manifest sha256 matches)"
done
