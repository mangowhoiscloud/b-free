#!/usr/bin/env bash
# Landing zone on the EKS source (source-aws, D-23, CLOUD-E2E-CONTRACT): Cilium chaining + Gateway,
# CNPG chat-db + schema, ESO <- Secrets Manager, PgBouncer, demo app, cloudflared.
#   overlays/eks/install.sh <kube-context> [step ...]
# Steps (in order; naming steps runs only those; each is idempotent):
#   cilium base cnpg schema secrets eso app gateway canary verify
# Inputs: `terraform -chdir=infra/envs/source-aws output` (ECR, KMS, Secrets Manager ARN, bucket, API host).
# Secrets (D-26): values move only on pipes: CNPG Secret -> Secrets Manager / OpenBao, OpenBao ->
# kubectl (deploy/pilot/scripts/keys.sh env-generate / env-pipe / env-put). Nothing is written to a file
# or passed in argv; this script prints names and versions only.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lz="$(cd "$here/../.." && pwd)"
repo="$(cd "$lz/../.." && pwd)"
keys="$repo/deploy/pilot/scripts/keys.sh"
tfdir="$repo/infra/envs/source-aws"
ctx=${1:?usage: install.sh <kube-context> [step ...]}
shift
# shellcheck source=../../versions.env
set -a; . "$lz/versions.env"; set +a

ENV_ID=${BRAVO_ENV_ID:-source-aws}
# Cilium 1.20.4 documents and tests Gateway API v1.6.3 (Documentation/conf.py gateway_api_version).
EKS_GATEWAY_API_VERSION=${EKS_GATEWAY_API_VERSION:-v1.6.3}
# Demo app 0.3.0 (APP_ENV_ID required, /readyz reads the DB through PgBouncer, attachments after HeadBucket).
EKS_DEMO_IMAGE_TAG=${EKS_DEMO_IMAGE_TAG:-0.3.0}
EKS_DEMO_IMAGE_DIGEST=${EKS_DEMO_IMAGE_DIGEST:-sha256:df08e969193efd318299864c6fd34e3e6e64c1a8d171e67ebfd109e60477ed57}
# QR target: the customer front name (CNAME to the active environment's tunnel, contract §6), so phones
# follow the cutover instead of staying on the source.
PUBLIC_URL=${PUBLIC_URL:-https://b-free-chat.mangowhois.ai}
PGBOUNCER_IMAGE_TAG=${PGBOUNCER_IMAGE_TAG:-1.26.1-pilot}

k() { kubectl --context "$ctx" "$@"; }
h() { helm --kube-context "$ctx" "$@"; }
log() { printf '\n== landing-zone[eks/%s] %s\n' "$ctx" "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

tf_out() { terraform -chdir="$tfdir" output -raw "$1"; }
load_outputs() {
  local ecr
  ecr="$(terraform -chdir="$tfdir" output -json ecr_repositories)"
  export AWS_REGION; AWS_REGION="$(tf_out attachments_bucket_region)"
  export EBS_KMS_KEY_ARN; EBS_KMS_KEY_ARN="$(tf_out ebs_kms_key_arn)"
  export DB_SECRET_ARN; DB_SECRET_ARN="$(tf_out db_secret_arn)"
  export DB_SECRET_NAME; DB_SECRET_NAME="$(aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id "$DB_SECRET_ARN" --query Name --output text)"
  export ATTACH_BUCKET; ATTACH_BUCKET="$(tf_out attachments_bucket)"
  export DEMO_IMAGE_REPO; DEMO_IMAGE_REPO="$(jq -r '."bravo-demo"' <<<"$ecr")"
  export PGBOUNCER_IMAGE_REPO_ECR; PGBOUNCER_IMAGE_REPO_ECR="$(jq -r '."bravo-pgbouncer"' <<<"$ecr")"
  export API_HOST; API_HOST="$(tf_out api_endpoint | sed 's#^https://##')"
  # The ECR copies must resolve to exactly these digests (same bytes as every other environment).
  export DEMO_IMAGE_DIGEST="$EKS_DEMO_IMAGE_DIGEST" PGBOUNCER_IMAGE_DIGEST
  export PGBOUNCER_IMAGE="$PGBOUNCER_IMAGE_REPO_ECR@$PGBOUNCER_IMAGE_DIGEST"
  export IMAGE_PULL_POLICY=IfNotPresent
}
render() {
  envsubst '${PG_IMAGE} ${CLOUDFLARED_IMAGE} ${PGBOUNCER_IMAGE} ${IMAGE_PULL_POLICY} ${EBS_KMS_KEY_ARN}
    ${AWS_REGION} ${DB_SECRET_NAME} ${ATTACH_BUCKET} ${DEMO_IMAGE_REPO} ${DEMO_IMAGE_DIGEST}'
}

# Fails unless the ECR tag resolves to the expected digest (images are pushed by scripts outside this file).
check_image() {
  local repo=$1 tag=$2 want=$3 got
  got="$(aws ecr describe-images --region "$AWS_REGION" --repository-name "${repo#*/}" \
    --image-ids imageTag="$tag" --query 'imageDetails[0].imageDigest' --output text)"
  [[ $got == "$want" ]] || die "ECR ${repo#*/}:$tag is $got, expected $want"
  echo "ECR ${repo#*/}:$tag = $got"
}

# Pods started before Cilium have no CiliumEndpoint: no policy, no Hubble from them (Cilium docs,
# "AWS VPC CNI plugin" > Restart existing pods). Restart the controller-owned ones.
restart_unmanaged() {
  local ns pods ceps p
  for ns in $(k get ns -o jsonpath='{.items[*].metadata.name}'); do
    ceps=" $(k -n "$ns" get cep -o jsonpath='{.items[*].metadata.name}' 2>/dev/null) "
    for p in $(k -n "$ns" get pod -o jsonpath='{range .items[?(@.spec.hostNetwork!=true)]}{.metadata.name}{" "}{.metadata.ownerReferences[0].kind}{"\n"}{end}' | awk '$2!=""{print $1}'); do
      [[ $ceps == *" $p "* ]] && continue
      echo "restart $ns/$p (no CiliumEndpoint)"
      k -n "$ns" delete pod "$p" --wait=false
    done
  done
}

step_cilium() {
  log "Gateway API $EKS_GATEWAY_API_VERSION CRDs, Cilium $CILIUM_CHART_VERSION (aws-cni chaining, KPR, Hubble)"
  local c base="https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/$EKS_GATEWAY_API_VERSION/config/crd/standard"
  for c in gatewayclasses gateways httproutes referencegrants grpcroutes backendtlspolicies tlsroutes; do
    k apply --server-side -f "$base/gateway.networking.k8s.io_$c.yaml" >/dev/null
  done
  h upgrade --install cilium cilium/cilium --version "$CILIUM_CHART_VERSION" -n kube-system \
    -f "$here/cilium-values.yaml" --set k8sServiceHost="$API_HOST" --wait --timeout 10m >/dev/null
  k -n kube-system rollout status ds/cilium --timeout=5m
  k -n kube-system rollout status deploy/cilium-operator --timeout=5m
  k -n kube-system rollout status deploy/hubble-relay --timeout=5m
  restart_unmanaged
}

step_base() {
  log "namespaces, StorageClasses, PgBouncer, cloudflared (kustomize overlay eks)"
  check_image "$PGBOUNCER_IMAGE_REPO_ECR" "$PGBOUNCER_IMAGE_TAG" "$PGBOUNCER_IMAGE_DIGEST"
  k kustomize "$here" | render | k apply -f -
}

step_cnpg() {
  log "CloudNativePG chart $CNPG_CHART_VERSION, Cluster chat-db"
  h upgrade --install cnpg cnpg/cloudnative-pg --version "$CNPG_CHART_VERSION" -n cnpg-system \
    --set replicaCount=1 --set resources.requests.cpu=50m --set resources.requests.memory=128Mi \
    --set resources.limits.memory=256Mi --wait --timeout 5m >/dev/null
  k -n cnpg-system rollout status deploy/cnpg-cloudnative-pg --timeout=5m
  render <"$here/chat-db.yaml" | k apply -f -
  k -n chat wait --for=condition=Ready cluster/chat-db --timeout=15m
}

# CNPG app password (owner app_writer) on stdin -> CMD. Only [A-Za-z0-9] is accepted (psql \set quoting).
cnpg_app_pw() {
  k -n chat get secret chat-db-app -o jsonpath='{.data.password}' | base64 -d \
    | { IFS= read -r -d '' v || true; [[ $v =~ ^[A-Za-z0-9]{32,}$ ]] || die "chat-db-app password not alnum"; printf '%s' "$v"; }
}
sha() { shasum -a 256 | cut -c1-16; }
# Password on stdin -> PostgreSQL SCRAM-SHA-256 verifier on stdout (same algorithm as
# apps/demo/schema/00-bravo-init.sh, but the password comes from a pipe instead of the environment).
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

step_schema() {
  log "app credential: CNPG chat-db-app -> Secrets Manager $DB_SECRET_NAME, OpenBao secret/envs/$ENV_ID/chat-db"
  [[ $(k -n chat get secret chat-db-app -o jsonpath='{.data.username}' | base64 -d) == app_writer ]] \
    || die "chat-db-app is not the app_writer credential"
  local have want
  want="$(cnpg_app_pw | sha)"
  have="$(aws secretsmanager get-secret-value --region "$AWS_REGION" --secret-id "$DB_SECRET_ARN" \
    --query SecretString --output text 2>/dev/null | tr -d '\n' | sha || true)"
  if [[ $have == "$want" ]]; then
    echo "Secrets Manager: AWSCURRENT already equals the CNPG app password (sha256/16 $want)"
  else
    cnpg_app_pw | aws secretsmanager put-secret-value --region "$AWS_REGION" --secret-id "$DB_SECRET_ARN" \
      --secret-string file:///dev/stdin --query '{name:Name,version:VersionId,stages:VersionStages}' --output json
  fi
  cnpg_app_pw | "$keys" env-put "$ENV_ID" chat-db app_writer
  local f
  for f in replicator executor_ctl executor_observe pgb_admin pgb_stats; do
    "$keys" env-generate "$ENV_ID" chat-db "$f"
  done

  log "schema apps/demo/schema/sql 01 -> 02 -> 03_src on chat-db (as postgres, local socket)"
  local pod
  pod="$(k -n chat get pod -l cnpg.io/cluster=chat-db,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}')"
  if [[ $(k -n chat exec "$pod" -c postgres -- psql -XAtq -d chat -c "select to_regclass('public.messages') is not null") == t ]]; then
    echo "schema already present on $pod"
  else
    # initdb made app_writer the database owner. Hand the database to postgres and drop the bootstrap role;
    # 02_roles.sql recreates app_writer (same CNPG password) with only the grants the contract gives it.
    # One transaction; the guard stops before any change if a value did not arrive through its pipe.
    # 02_roles.sql reads SCRAM verifiers with \getenv; the variables are unset in the pod, so psql keeps
    # the values set here (psql: \getenv leaves the variable unchanged when the env var is undefined).
    # Only verifiers reach the server, as with 00-bravo-init.sh.
    cnpg_app_pw >/dev/null
    {
      printf '\\set ON_ERROR_STOP on\n'
      printf "\\\\set app_writer_verifier '%s'\n" "$(cnpg_app_pw | scram)"
      for f in replicator executor_ctl executor_observe; do
        printf "\\\\set %s_verifier '%s'\n" "$f" "$("$keys" env-pipe "$ENV_ID" chat-db "$f" -- cat 2>/dev/null | scram)"
      done
      printf "\\\\set attachment_store_id 'src'\n"
      printf "SELECT 1 / (:'app_writer_verifier' LIKE 'SCRAM-SHA-256\$4096:%%' AND :'replicator_verifier' LIKE 'SCRAM-SHA-256\$4096:%%' AND :'executor_ctl_verifier' LIKE 'SCRAM-SHA-256\$4096:%%' AND :'executor_observe_verifier' LIKE 'SCRAM-SHA-256\$4096:%%')::int AS verifiers_present;\n"
      printf 'ALTER DATABASE chat OWNER TO postgres;\nDROP ROLE app_writer;\n'
      cat "$repo/apps/demo/schema/sql/01_schema.sql" "$repo/apps/demo/schema/sql/02_roles.sql" \
        "$repo/apps/demo/schema/sql/03_src.sql"
    } | k -n chat exec -i "$pod" -c postgres -- psql -X -q -1 -v ON_ERROR_STOP=1 -d chat -f - >/dev/null
    echo "schema applied on $pod"
  fi
  k -n chat exec "$pod" -c postgres -- psql -XAt -d chat -c \
    "select 'roles: '||string_agg(rolname||(case when rolcanlogin then '(login)' else '' end), ' ' order by rolname) from pg_roles where rolname in ('app_writer','replicator','executor_ctl','executor_observe','chat_owner')
     union all select 'publication: '||string_agg(pubname||'='||(select count(*) from pg_publication_tables t where t.pubname=p.pubname), ' ') from pg_publication p
     union all select 'wal_level='||current_setting('wal_level')||' max_replication_slots='||current_setting('max_replication_slots')||' max_slot_wal_keep_size='||current_setting('max_slot_wal_keep_size')
     union all select 'db owner: '||pg_get_userbyid(datdba) from pg_database where datname='chat'
     union all select 'extensions: '||string_agg(extname||' '||extversion, ', ') from pg_extension"
}

secret_apply() { k apply -f - >/dev/null; }

step_secrets() {
  log "Secrets from the platform OpenBao (secret/envs/$ENV_ID/*), pipes only"
  "$keys" env-generate "$ENV_ID" chat-admin token 32
  "$keys" env-pipe "$ENV_ID" chat-admin token -- \
    kubectl --context "$ctx" -n chat create secret generic chat-admin --from-file=token=/dev/stdin \
      --dry-run=client -o yaml | secret_apply
  echo "secret chat/chat-admin <- OpenBao chat-admin/token"
  # PgBouncer platform logins; app_writer is merged in by ESO from Secrets Manager (eso.yaml).
  {
    printf '{"apiVersion":"v1","kind":"Secret","type":"Opaque","metadata":{"name":"chat-pgbouncer-auth","namespace":"chat"},"data":{'
    printf '"pgb_admin":"%s",' "$("$keys" env-pipe "$ENV_ID" chat-db pgb_admin -- base64 2>/dev/null | tr -d '\n')"
    printf '"pgb_stats":"%s"}}' "$("$keys" env-pipe "$ENV_ID" chat-db pgb_stats -- base64 2>/dev/null | tr -d '\n')"
  } | secret_apply
  echo "secret chat/chat-pgbouncer-auth <- OpenBao chat-db/{pgb_admin,pgb_stats}"
  # DSN without password: the source app reads its password from Secrets Manager (chart passwordSource).
  k -n chat create secret generic bravo-demo-db \
    --from-literal=dsn="host=chat-pgbouncer.chat.svc.cluster.local port=6432 dbname=chat user=app_writer" \
    --dry-run=client -o yaml | secret_apply
  echo "secret chat/bravo-demo-db <- dsn (no password)"
  "$keys" env-pipe "$ENV_ID" cf-tunnel token -- \
    kubectl --context "$ctx" -n cf-edge create secret generic cf-tunnel --from-file=token=/dev/stdin \
      --dry-run=client -o yaml | secret_apply
  echo "secret cf-edge/cf-tunnel <- OpenBao cf-tunnel/token"
}

step_eso() {
  log "External Secrets chart $ESO_CHART_VERSION (Pod Identity), SecretStore -> Secrets Manager"
  h upgrade --install external-secrets external-secrets/external-secrets --version "$ESO_CHART_VERSION" \
    -n external-secrets --set installCRDs=true --set replicaCount=1 \
    --set resources.requests.cpu=20m --set resources.requests.memory=64Mi \
    --set webhook.resources.requests.cpu=10m --set webhook.resources.requests.memory=32Mi \
    --set certController.resources.requests.cpu=10m --set certController.resources.requests.memory=32Mi \
    --wait --timeout 5m >/dev/null
  render <"$here/eso.yaml" | k apply -f -
  k -n chat wait --for=condition=Ready secretstore/aws-secrets-manager --timeout=2m
  k -n chat wait --for=condition=Ready externalsecret/chat-pgbouncer-auth --timeout=3m
  echo "chat-pgbouncer-auth keys: $(k -n chat get secret chat-pgbouncer-auth -o json | jq -c '.data | keys')"
  k -n chat rollout restart deploy/chat-pgbouncer >/dev/null
  k -n chat rollout status deploy/chat-pgbouncer --timeout=3m
}

step_app() {
  # ci/eks-values.yaml is render-test only (EXAMPLE values); the deployable values come from
  # apps/demo/scripts/render-values.sh eks (terraform output + the ECR digest). No OTel collector runs
  # here yet, so otel.endpoint stays at the chart default "" (exporter off).
  log "demo app: Helm release chat (values from render-values.sh eks)"
  check_image "$DEMO_IMAGE_REPO" "$EKS_DEMO_IMAGE_TAG" "$DEMO_IMAGE_DIGEST"
  local gen="$repo/apps/demo/chart/values-eks.generated.yaml"
  "$repo/apps/demo/scripts/render-values.sh" eks --image-repository "$DEMO_IMAGE_REPO" \
    --image-digest "$DEMO_IMAGE_DIGEST" --public-url "$PUBLIC_URL" --out "$gen"
  h upgrade --install chat "$repo/apps/demo/chart" -n chat \
    -f "$gen" \
    --wait --timeout 5m >/dev/null
  k -n chat rollout status deploy/chat-bravo-demo --timeout=3m
}

step_gateway() {
  log "Gateway bravo-gateway (ns bravo-edge, class bravo-cilium, Service NodePort) + HTTPRoute chat"
  k apply -f "$here/gateway.yaml"
  k -n bravo-edge wait --for=condition=Programmed gateway/bravo-gateway --timeout=3m
  k -n bravo-edge get svc cilium-gateway-bravo-gateway
  k -n cf-edge rollout status deploy/cloudflared --timeout=3m
}

# Gate canary (platform/gate): written as the app identity, because the strong bucket policy denies
# every other principal on objects. Then: anonymous GET must be 403.
step_canary() {
  log "gate canary s3://$ATTACH_BUCKET/_gate/canary as chat/chat-app (Pod Identity)"
  k -n chat delete job gate-canary --ignore-not-found >/dev/null
  render <"$here/gate-canary-job.yaml" | k apply -f -
  k -n chat wait --for=condition=Complete job/gate-canary --timeout=3m
  k -n chat logs job/gate-canary
  printf 'anonymous GET: HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' \
    "https://$ATTACH_BUCKET.s3.$AWS_REGION.amazonaws.com/_gate/canary")"
}

step_verify() {
  log "verify"
  k get pods -A -o wide
  k -n chat get cluster chat-db
  k get gateway -A; k get httproute -A
  k -n kube-system exec ds/cilium -c cilium-agent -- hubble observe --last 20 --namespace chat
}

load_outputs
steps=("$@")
((${#steps[@]})) || steps=(cilium base cnpg schema secrets eso app gateway canary verify)
for s in "${steps[@]}"; do
  declare -F "step_$s" >/dev/null || die "unknown step: $s"
  "step_$s"
done
