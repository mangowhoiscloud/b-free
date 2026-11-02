#!/usr/bin/env bash
# OBJ-01..06 behaviour tests (gate harness, D-22) against the on-prem RGW bucket, two ways:
#  1. in-cluster Job with the gate image (bravo_gate.probes.objstore, the code the gate and its
#     tests use) against https://rook-ceph-rgw-bravo.rook-ceph.svc; OBJ-06's http:// variant then
#     hits RGW's real plain listener on :80, where the TLS-only bucket policy must deny it.
#  2. the gate's pytest s3 tests (BRAVO_TEST_S3_*) from the laptop through a port-forward.
#     macOS does not let a user bind 127.0.0.1:443/80, so this run uses https://localhost:18443 and
#     its OBJ-06 can only reach the TLS port; run 1 is the authoritative OBJ-06.
# Keys: app = OBC owner (Secret chat/bravo-demo-attachments), other = CephObjectStoreUser
# bravo-gate-other (copied to chat/bravo-gate-other on a pipe). Never written to disk.
#   obj-tests.sh <kube-context>
set -euo pipefail
ctx=$1
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
gate="$(cd "$here/../../../platform/gate" && pwd)"
k() { kubectl --context "$ctx" "$@"; }
GATE_IMAGE_LOCAL=${GATE_IMAGE_LOCAL:-bravo-gate:pilot}
GATE_REPO=bravo.local/bravo-gate
GATE_DIGEST="$(docker image inspect "$GATE_IMAGE_LOCAL" --format '{{.Id}}')"

# Gate image into containerd (same import path as the app images).
docker tag "$GATE_IMAGE_LOCAL" "$GATE_REPO:import"
for n in bravo-onprem-server bravo-onprem-agent; do
  if ! limactl shell "$n" -- sudo k3s ctr -n k8s.io images ls -q </dev/null | grep -qx "$GATE_REPO@$GATE_DIGEST"; then
    docker save "$GATE_REPO:import" | limactl shell "$n" -- sh -c 'cat > /tmp/bravo-image.tar'
    limactl shell "$n" -- sudo sh -c "k3s ctr -n k8s.io images import --platform linux/arm64 /tmp/bravo-image.tar >/dev/null && k3s ctr -n k8s.io images tag --force $GATE_REPO:import $GATE_REPO@$GATE_DIGEST >/dev/null && rm -f /tmp/bravo-image.tar" </dev/null
  fi
done
docker rmi "$GATE_REPO:import" >/dev/null

k -n rook-ceph get secret rook-ceph-object-user-bravo-bravo-gate-other -o json \
  | jq '{apiVersion: "v1", kind: "Secret", type: "Opaque", metadata: {name: "bravo-gate-other", namespace: "chat"},
         data: {AccessKey: .data.AccessKey, SecretKey: .data.SecretKey}}' | k apply -f - >/dev/null

k -n chat delete job bravo-obj-tests --ignore-not-found >/dev/null
k apply -f - >/dev/null <<YAML
apiVersion: batch/v1
kind: Job
metadata: {name: bravo-obj-tests, namespace: chat}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: obj
          image: $GATE_REPO@$GATE_DIGEST
          imagePullPolicy: Never
          command: [python, -c]
          args:
            - |
              import json
              from bravo_gate.environments import parse_registry
              from bravo_gate.probes.objstore import ObjectStoreProbe
              sec = {"bucket": "bravo-demo-attachments", "endpoint_url": "https://rook-ceph-rgw-bravo.rook-ceph.svc",
                     "region": "us-east-1", "addressing_style": "path", "canary_key": "_gate/canary", "write_probes": True,
                     "app_credential": {"access_key_env": "APP_KEY", "secret_key_env": "APP_SECRET"},
                     "other_credential": {"access_key_env": "OTHER_KEY", "secret_key_env": "OTHER_SECRET"}}
              p = ObjectStoreProbe(parse_registry({"environments": {"onprem": {"object_store": sec}}}).get("onprem").object_store)
              print("canary:", p.canary().kind)
              for qid, fn in [("OBJ-01", p.obj01), ("OBJ-02", p.obj02), ("OBJ-03", p.obj03), ("OBJ-04", p.obj04),
                              ("OBJ-05", p.obj05), ("OBJ-06", p.obj06), ("OBJ-ENC", p.obj_enc)]:
                  o = fn()
                  ev = {k: v for k, v in o.evidence.items() if k in ("request", "response", "plain_endpoint", "https_head", "head")}
                  print(f"{qid}: {o.verdict:7} {o.reason}  {json.dumps(ev, ensure_ascii=False)}")
          env:
            - {name: AWS_CA_BUNDLE, value: /ca/ca.crt}
            - {name: APP_KEY, valueFrom: {secretKeyRef: {name: bravo-demo-attachments, key: AWS_ACCESS_KEY_ID}}}
            - {name: APP_SECRET, valueFrom: {secretKeyRef: {name: bravo-demo-attachments, key: AWS_SECRET_ACCESS_KEY}}}
            - {name: OTHER_KEY, valueFrom: {secretKeyRef: {name: bravo-gate-other, key: AccessKey}}}
            - {name: OTHER_SECRET, valueFrom: {secretKeyRef: {name: bravo-gate-other, key: SecretKey}}}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          volumeMounts: [{name: ca, mountPath: /ca, readOnly: true}]
      volumes: [{name: ca, configMap: {name: bravo-rgw-ca}}]
YAML
for _ in $(seq 1 90); do
  st="$(k -n chat get job bravo-obj-tests -o jsonpath='{.status.succeeded}{.status.failed}')"
  [[ -n $st ]] && break; sleep 2
done
echo "== in-cluster (gate image $GATE_REPO@$GATE_DIGEST, endpoint https://rook-ceph-rgw-bravo.rook-ceph.svc)"
k -n chat logs job/bravo-obj-tests

echo "== gate pytest s3 tests (laptop, port-forward https://localhost:18443)"
work="$(mktemp -d)"; chmod 700 "$work"
k -n rook-ceph port-forward svc/rook-ceph-rgw-bravo 18443:443 >"$work/pf.log" 2>&1 &
pf=$!
trap 'kill $pf 2>/dev/null || true; rm -rf "$work"' EXIT
for _ in $(seq 1 30); do grep -q Forwarding "$work/pf.log" && break; sleep 0.5; done
k -n rook-ceph get configmap bravo-rgw-ca -o jsonpath='{.data.ca\.crt}' >"$work/ca.crt"   # public CA cert
sec() { k -n "$1" get secret "$2" -o jsonpath="{.data.$3}" | base64 -d; }
unset AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
cd "$gate"
BRAVO_TEST_S3_ENDPOINT=https://localhost:18443 BRAVO_TEST_S3_BUCKET=bravo-demo-attachments \
BRAVO_TEST_S3_REGION=us-east-1 BRAVO_TEST_S3_ADDRESSING_STYLE=path BRAVO_TEST_S3_WRITE_PROBES=1 \
AWS_CA_BUNDLE="$work/ca.crt" \
BRAVO_TEST_S3_ACCESS_KEY_ID="$(sec chat bravo-demo-attachments AWS_ACCESS_KEY_ID)" \
BRAVO_TEST_S3_SECRET_ACCESS_KEY="$(sec chat bravo-demo-attachments AWS_SECRET_ACCESS_KEY)" \
BRAVO_TEST_S3_OTHER_ACCESS_KEY_ID="$(sec chat bravo-gate-other AccessKey)" \
BRAVO_TEST_S3_OTHER_SECRET_ACCESS_KEY="$(sec chat bravo-gate-other SecretKey)" \
  uv run --quiet pytest -m s3 tests/test_engines.py -rA -q -p no:cacheprovider 2>&1 | tail -8
