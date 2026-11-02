-- Read-only collection role for bravo-collector (FR-1, D-05). Idempotent. Run as a superuser with psql 15+.
-- No plaintext password in this file, in psql argv or in the SQL text sent to the server (audit B19):
-- the role password arrives as a client-side SCRAM-SHA-256 verifier in an environment variable.
--   export COLLECTOR_RO_SCRAM_VERIFIER="$(<password source> | python -m bravo_collector scram-verifier)"
--   psql "$SUPERUSER_DSN" -v ON_ERROR_STOP=1 -f collector_ro.sql
-- The role gets no table write privilege and every session defaults to read-only.

\getenv collector_ro_verifier COLLECTOR_RO_SCRAM_VERIFIER
\if :{?collector_ro_verifier}
\else
  SELECT 'COLLECTOR_RO_SCRAM_VERIFIER is not set'::int;
\endif
-- Refuse anything that is not a verifier, so a plaintext password is never stored or sent in DDL.
SELECT starts_with(:'collector_ro_verifier', 'SCRAM-SHA-256$') AS collector_ro_verifier_ok \gset
\if :collector_ro_verifier_ok
\else
  SELECT 'COLLECTOR_RO_SCRAM_VERIFIER is not a SCRAM-SHA-256 verifier'::int;
\endif

SELECT format('CREATE ROLE collector_ro LOGIN PASSWORD %L', :'collector_ro_verifier')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'collector_ro') \gexec

ALTER ROLE collector_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'collector_ro_verifier';
ALTER ROLE collector_ro SET default_transaction_read_only = on;

GRANT pg_read_all_settings TO collector_ro;
GRANT pg_read_all_stats TO collector_ro;
GRANT pg_monitor TO collector_ro;

-- pg_hba.conf rules (control inputs; no secrets in this view).
GRANT SELECT ON pg_catalog.pg_hba_file_rules TO collector_ro;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_hba_file_rules() TO collector_ro;

SELECT format('GRANT CONNECT ON DATABASE %I TO collector_ro', current_database()) \gexec
