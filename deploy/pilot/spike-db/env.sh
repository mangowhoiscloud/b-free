# Sourced by the spike scripts. No secret values here: only names, ports and paths.
# SPIKE_SIDE=rds (default) | cloudsql selects the managed endpoint.
SPIKE_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPIKE_REPO="$(cd "$SPIKE_HERE/../../.." && pwd)"
SPIKE_SIDE="${SPIKE_SIDE:-rds}"
SPIKE_TLS="${SPIKE_TLS:?set SPIKE_TLS to the spike TLS dir (ca.crt, server.*, provider CA bundles)}"
SPIKE_RUN="${SPIKE_RUN:?set SPIKE_RUN to deploy/pilot/runs/spike-<side>-<ts>}"
CTX=bravo-source
NS=spike-db
PG_LOCAL_PORT=45432           # laptop spike PG (bravo-spike-pg)
GCP_PROJECT=b-free-target-20261102
GCP_ZONE=asia-northeast1-a
if [[ $SPIKE_SIDE == rds ]]; then
  SPIKE_TF="$SPIKE_REPO/infra/envs/spike-db"
  tfout() { terraform -chdir="$SPIKE_TF" output -raw "$1"; }
  M_LOCAL_PORT=45433          # laptop end of the forward link (kubectl port-forward deploy/rds-fwd)
  M_HOST="$(tfout endpoint)"
  M_MASTER=bravo_master
  M_CA="$SPIKE_TLS/rds-ap-northeast-2-bundle.pem"
  M_SECRET=envs/spike-db/rds
  RDS_SECRET_ARN="$(tfout master_user_secret_arn)"
  # Master password straight from the RDS-managed secret into one process's environment.
  m_master_pw() { aws secretsmanager get-secret-value --region ap-northeast-2 --secret-id "$RDS_SECRET_ARN" \
    --query SecretString --output text | jq -er .password; }
else
  SPIKE_TF="$SPIKE_REPO/infra/envs/spike-db/gcp"
  tfout() { terraform -chdir="$SPIKE_TF" output -raw "$1"; }
  M_LOCAL_PORT=45533          # laptop end of the forward link (IAP tunnel -> link VM :7071 -> Cloud SQL)
  # PSA + GOOGLE_MANAGED_CAS_CA: the name is in dnsNames (recordManager CUSTOMER); the legacy dnsName is null.
  M_HOST="$(gcloud sql instances describe "$(tfout cloudsql_instance)" --project "$GCP_PROJECT" --format=json \
    | jq -r '[.dnsNames[]? | select(.connectionType == "PRIVATE_SERVICES_ACCESS")][0].name')"  # keep the trailing dot: the SAN has it (spike-cloudsql-*/n6-tls-client.txt)
  M_MASTER=postgres
  M_CA="$SPIKE_TLS/cloudsql-server-ca.pem"
  M_SECRET=envs/spike-db/cloudsql
  m_master_pw() { "$SPIKE_HERE/bao.sh" get "$M_SECRET" postgres; }
fi
# compatibility names used by rds_schema.sh
RDS_HOST="$M_HOST"; RDS_LOCAL_PORT="$M_LOCAL_PORT"; RDS_CA="$M_CA"
rds_master_pw() { m_master_pw; }
# libpq to the managed DB through the link: verify the provider name (host=), send TCP to the link (hostaddr=).
m_dsn() { printf 'host=%s hostaddr=127.0.0.1 port=%s dbname=chat user=%s sslmode=verify-full sslrootcert=%s connect_timeout=5' \
  "$M_HOST" "$M_LOCAL_PORT" "$1" "$M_CA"; }
rds_dsn() { m_dsn "$@"; }
pg_dsn() { printf 'host=pilot-pg.spike.bravo.invalid hostaddr=127.0.0.1 port=%s dbname=chat user=%s sslmode=verify-full sslrootcert=%s/ca.crt connect_timeout=5' \
  "$PG_LOCAL_PORT" "$1" "$SPIKE_TLS"; }
