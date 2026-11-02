-- Removes the planted regression on dst (CONTRACT.md 3): PUBLIC must not read messages.
-- Run as the table owner or a superuser against dst-db, database chat:
--   docker compose -f deploy/pilot/compose.yaml exec -T dst-db \
--     sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -U postgres -d chat' \
--     < deploy/pilot/fix-dst-grant.sql
\set ON_ERROR_STOP on

REVOKE SELECT ON messages FROM PUBLIC;

-- Evidence for the operator: must print f.
SELECT has_table_privilege('public', 'messages', 'SELECT') AS public_can_read_messages;
