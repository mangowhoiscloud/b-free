-- Read-only collection role for bravo-collector (FR-1, D-05). Run as a superuser with psql:
--   psql "$SUPERUSER_DSN" -v ON_ERROR_STOP=1 -v collector_ro_password="$COLLECTOR_RO_PASSWORD" -f collector_ro.sql
-- The password comes from a psql variable; no secret is stored in this file. Idempotent.
-- The role gets no table write privilege and every session defaults to read-only.

SELECT format('CREATE ROLE collector_ro LOGIN PASSWORD %L', :'collector_ro_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'collector_ro') \gexec

ALTER ROLE collector_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'collector_ro_password';
ALTER ROLE collector_ro SET default_transaction_read_only = on;

GRANT pg_read_all_settings TO collector_ro;
GRANT pg_read_all_stats TO collector_ro;
GRANT pg_monitor TO collector_ro;

-- pg_hba.conf rules (control inputs; no secrets in this view).
GRANT SELECT ON pg_catalog.pg_hba_file_rules TO collector_ro;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_hba_file_rules() TO collector_ro;

SELECT format('GRANT CONNECT ON DATABASE %I TO collector_ro', current_database()) \gexec
