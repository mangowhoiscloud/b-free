-- Read-only probe role of the platform gate (FR-6, FR-12), separate from the collector's collector_ro. Idempotent.
-- Run as a superuser with psql 15+ (deploy/platform/scripts/gate-credentials.sh, through kubectl exec -i on stdin).
-- No plaintext password anywhere: the caller sets psql variable gate_ro_verifier to a client-side SCRAM-SHA-256
-- verifier (the password stays in OpenBao secret/envs/<env>/gate-db). What the probes read (probes/postgres.py):
--   pg_settings (ssl, password_encryption, log_connections, shared_preload_libraries, archive_mode, pgaudit.log),
--   pg_hba_file_rules, pg_stat_archiver, pg_stat_ssl, pg_roles, has_table_privilege('public', <app table>, 'SELECT').
-- No table privilege, every session read-only.

SELECT starts_with(:'gate_ro_verifier', 'SCRAM-SHA-256$') AS gate_ro_verifier_ok \gset
\if :gate_ro_verifier_ok
\else
  SELECT 'gate_ro_verifier is not a SCRAM-SHA-256 verifier'::int;
\endif

SELECT format('CREATE ROLE gate_ro LOGIN PASSWORD %L', :'gate_ro_verifier')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gate_ro') \gexec

ALTER ROLE gate_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'gate_ro_verifier';
ALTER ROLE gate_ro SET default_transaction_read_only = on;
ALTER ROLE gate_ro CONNECTION LIMIT 4;

GRANT pg_read_all_settings TO gate_ro;
GRANT pg_read_all_stats TO gate_ro;

GRANT SELECT ON pg_catalog.pg_hba_file_rules TO gate_ro;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_hba_file_rules() TO gate_ro;

SELECT format('GRANT CONNECT ON DATABASE %I TO gate_ro', current_database()) \gexec
