#!/bin/sh
# docker-entrypoint-initdb.d script for the rehearsal synthetic database (db_rehearse).
# Creates the only role the tool layer uses: rehearse_rw owns schema app and nothing else. Its
# transactions are always rolled back by the tool; the role cannot create databases or roles.
# REHEARSE_RW_PASSWORD comes from the container environment, never from this file.
set -eu
: "${REHEARSE_RW_PASSWORD:?set REHEARSE_RW_PASSWORD}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv rw_password REHEARSE_RW_PASSWORD
CREATE ROLE rehearse_rw LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'rw_password';
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA app AUTHORIZATION rehearse_rw;
ALTER ROLE rehearse_rw SET search_path = app;
ALTER ROLE rehearse_rw SET statement_timeout = '5s';
ALTER ROLE rehearse_rw SET lock_timeout = '2s';
SQL
