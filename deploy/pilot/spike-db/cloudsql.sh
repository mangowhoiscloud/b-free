#!/usr/bin/env bash
# Cloud SQL side of the spike (SPIKE_SIDE=cloudsql). Project is always explicit (no gcloud default).
#   cloudsql.sh ca            server CA chain -> $SPIKE_TLS/cloudsql-server-ca.pem; cert SAN -> $SPIKE_RUN
#   cloudsql.sh set-password  postgres password from OpenBao -> gcloud --prompt-for-password (stdin pipe)
#   cloudsql.sh tunnel-fwd    IAP tunnel 127.0.0.1:45533 -> link VM :7071 (-> Cloud SQL :5432)   [foreground]
#   cloudsql.sh tunnel-agent  IAP tunnel 127.0.0.1:47071 -> link VM :7070 (relay agent port)     [foreground]
set -euo pipefail
export SPIKE_SIDE=cloudsql
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
INSTANCE="$(tfout cloudsql_instance)"
VM="$(tfout link_vm)"
case ${1:?usage} in
  ca)
    gcloud sql instances describe "$INSTANCE" --project "$GCP_PROJECT" --format=json \
      | jq -r '.serverCaCert.cert' > "$M_CA"
    gcloud sql ssl server-ca-certs list --instance "$INSTANCE" --project "$GCP_PROJECT" --format=json \
      | jq -r '.[].cert' > "$SPIKE_TLS/cloudsql-server-ca-all.pem"
    gcloud sql instances describe "$INSTANCE" --project "$GCP_PROJECT" --format=json \
      | jq '{dnsName, connectionName, ipAddresses, serverCaMode: .settings.ipConfiguration.serverCaMode,
             sslMode: .settings.ipConfiguration.sslMode, availabilityType: .settings.availabilityType,
             tier: .settings.tier, edition: .settings.edition, databaseVersion, flags: .settings.databaseFlags,
             serverCaCert: (.serverCaCert | {commonName, expirationTime, instance})}' > "$SPIKE_RUN/cloudsql-instance.json"
    cat "$SPIKE_RUN/cloudsql-instance.json"
    ;;
  set-password)
    "$SPIKE_HERE/bao.sh" get "$M_SECRET" postgres \
      | gcloud sql users set-password postgres --instance "$INSTANCE" --project "$GCP_PROJECT" --prompt-for-password
    ;;
  tunnel-fwd)
    exec gcloud compute start-iap-tunnel "$VM" 7071 --local-host-port=127.0.0.1:45533 --zone "$GCP_ZONE" --project "$GCP_PROJECT"
    ;;
  tunnel-agent)
    exec gcloud compute start-iap-tunnel "$VM" 7070 --local-host-port=127.0.0.1:47071 --zone "$GCP_ZONE" --project "$GCP_PROJECT"
    ;;
esac
