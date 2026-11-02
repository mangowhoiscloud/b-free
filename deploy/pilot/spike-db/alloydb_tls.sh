#!/usr/bin/env bash
# SPK-11 (TLS part): does a direct AlloyDB connection (private IP :5432, no Auth Proxy) pass verify-ca /
# verify-full with the CA the AlloyDB API publishes? No AlloyDB password is needed: a deliberately wrong
# password tells TLS failure ("certificate verify failed", "does not match host name") from TLS success
# ("password authentication failed"). TCP path: laptop -> IAP tunnel 127.0.0.1:45533 -> link VM :7071 -> AlloyDB.
set -euo pipefail
PROJECT=b-free-target-20261102; REGION=asia-northeast1
TF="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../infra/envs/spike-db/gcp" && pwd)"
CLUSTER=bravo-spike-db-alloydb; INSTANCE=bravo-spike-db-alloydb-primary
OUT=${SPIKE_RUN:?}; TLS=${SPIKE_TLS:?}
AIP="$(terraform -chdir="$TF" output -raw alloydb_ip)"
TOK="$(gcloud auth print-access-token)"
api() { curl -sS -H @<(printf 'Authorization: Bearer %s\n' "$TOK") -H 'Content-Type: application/json' "$@"; }
BASE="https://alloydb.googleapis.com/v1/projects/$PROJECT/locations/$REGION/clusters/$CLUSTER"
api "$BASE/instances/$INSTANCE/connectionInfo" > "$OUT/alloydb-connection-info.json"
jq -r '.pemCertificateChain[]? // empty' "$OUT/alloydb-connection-info.json" > "$TLS/alloydb-conninfo-chain.pem" || true
# CA the AlloyDB connectors trust (generateClientCertificate returns caCert); a throwaway client key, not kept
key="$(mktemp)"; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$key" 2>/dev/null
pub="$(openssl pkey -in "$key" -pubout)"; rm -f "$key"
jq -n --arg p "$pub" '{publicKey: $p, certDuration: "3600s"}' | api -X POST --data-binary @- "$BASE:generateClientCertificate" \
  | jq '{caCert, pemCertificateChain: (.pemCertificateChain | length)}' > "$OUT/alloydb-client-cert-response.json"
jq -r '.caCert // empty' "$OUT/alloydb-client-cert-response.json" > "$TLS/alloydb-ca.pem"
{ echo "# $(date -u +%FT%TZ) AlloyDB direct connection $AIP:5432 through IAP -> link VM"
  echo "## server certificate (STARTTLS postgres)"
  openssl s_client -starttls postgres -connect 127.0.0.1:45533 -showcerts </dev/null 2>/dev/null \
    | python3 -I -c 'import re,sys; [open(sys.argv[1]+"/alloydb-served-%d.pem" % i,"w").write(c) for i,c in enumerate(re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", sys.stdin.read(), re.S), 1)]' "$TLS"
  for f in "$TLS"/alloydb-served-[0-9]*.pem; do openssl x509 -in "$f" -noout -subject -issuer -ext subjectAltName 2>/dev/null; done
  echo "## CA files: conninfo chain $(grep -c BEGIN "$TLS/alloydb-conninfo-chain.pem" || true) certs, caCert $(grep -c BEGIN "$TLS/alloydb-ca.pem" || true) certs"
  openssl x509 -in "$TLS/alloydb-ca.pem" -noout -subject 2>/dev/null || true
  hostgw="$(docker run --rm --add-host host.docker.internal:host-gateway postgres:16.15 getent ahostsv4 host.docker.internal | awk 'NR==1{print $1}')"
  try() { echo "## $1"; docker run --rm --add-host host.docker.internal:host-gateway -v "$TLS:/tls:ro" postgres:16.15 \
      psql "$2 hostaddr=$hostgw port=45533 dbname=postgres user=postgres password=deliberately-wrong connect_timeout=10" \
      -Atc 'select 1' 2>&1 | tail -1 || true; }
  try "require, no root CA (encryption only)" "host=$AIP sslmode=require sslrootcert=/nonexistent"
  try "verify-ca, AlloyDB API caCert (root.alloydb)" "host=$AIP sslmode=verify-ca sslrootcert=/tls/alloydb-ca.pem"
  try "verify-full, AlloyDB API caCert, host=IP" "host=$AIP sslmode=verify-full sslrootcert=/tls/alloydb-ca.pem"
  try "verify-ca, pinning the served self-signed cert" "host=$AIP sslmode=verify-ca sslrootcert=/tls/alloydb-served-1.pem"
  try "verify-full, pinning the served cert, host=localhost (its only SAN)" "host=localhost sslmode=verify-full sslrootcert=/tls/alloydb-served-1.pem"
  try "verify-full, pinning the served cert, host=IP" "host=$AIP sslmode=verify-full sslrootcert=/tls/alloydb-served-1.pem"
  try "plaintext" "host=$AIP sslmode=disable"
} 2>&1 | tee "$OUT/spk11-alloydb-tls.txt"
