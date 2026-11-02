#!/usr/bin/env bash
# D-16 environment-to-environment DB links (replace the 임시 링크 laptop path):
#   on-prem ns chat: src-db-link -> t1-src-db (service token dst-src-db, OpenBao envs/target-onprem/cf-link-src-db)
#   source  ns chat: dst-db-link -> t1-dst-db (service token src-dst-db, OpenBao envs/source-aws/cf-link-dst-db)
#     (same Service name as the old reverse relay, so target-dst.ini and the reverse subscription keep it)
#   env-link.sh apply | status
# Token values move only through keys.sh env-pipe (stdin) and jq env -> kubectl apply -f - (no file, no argv).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
KEYS="$REPO/deploy/pilot/scripts/keys.sh"
render() {  # render NAME HOSTNAME SECRET
  sed -e "s#\${NAME}#$1#g" -e "s#\${HOSTNAME}#$2#g" -e "s#\${SECRET}#$3#g" "$CLOUD_DIR/env-link/link.yaml.tmpl"
}
deliver() {  # deliver CONTEXT OPENBAO_ENV OPENBAO_NAME SECRET_NAME
  "$KEYS" env-pipe "$2" "$3" client_id -- env CTX="$1" KEYS="$KEYS" OENV="$2" ONAME="$3" SNAME="$4" sh -c '
    CID="$(cat)"; export CID
    "$KEYS" env-pipe "$OENV" "$ONAME" client_secret -- jq -Rs --arg n "$SNAME" "{apiVersion: \"v1\", kind: \"Secret\",
      type: \"Opaque\", metadata: {name: \$n, namespace: \"chat\", labels: {\"bravo.io/source\": \"openbao\"}},
      data: {client_id: (env.CID | @base64), client_secret: (. | @base64)}}" | kubectl --context "$CTX" apply -f -'
}
case "${1:-status}" in
  apply)
    deliver "$DST_CONTEXT" "$DST_ENV" cf-link-src-db cf-link-src-db
    render src-db-link t1-src-db.mangowhois.ai cf-link-src-db | kubectl --context "$DST_CONTEXT" apply -f -
    deliver "$SRC_CONTEXT" "$SRC_ENV" cf-link-dst-db cf-link-dst-db
    # the 임시 링크 reverse relay (laptop agent) is replaced by the D-16 client under the same Service name
    kubectl --context "$SRC_CONTEXT" -n chat delete deploy/dst-db-link configmap/dst-db-link-relay networkpolicy/dst-db-link --ignore-not-found
    render dst-db-link t1-dst-db.mangowhois.ai cf-link-dst-db | kubectl --context "$SRC_CONTEXT" apply -f -
    ;;
  status)
    for c in "$DST_CONTEXT:src-db-link" "$SRC_CONTEXT:dst-db-link"; do
      kubectl --context "${c%%:*}" -n chat get deploy "${c#*:}" -o jsonpath='{.metadata.name} ready={.status.readyReplicas}{"\n"}' 2>&1
      kubectl --context "${c%%:*}" -n chat logs "deploy/${c#*:}" --tail=3 2>&1 | cut -c1-200
    done
    ;;
  *) fail "usage: env-link.sh apply | status" ;;
esac
