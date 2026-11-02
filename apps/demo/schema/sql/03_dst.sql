-- Destination only (CONTRACT.md 3).
\set ON_ERROR_STOP on

-- Destination writes stay closed until cutover step 7 restores LOGIN.
ALTER ROLE app_writer NOLOGIN;

-- Reverse publication and its pre-created slot. The reverse subscription on src
-- uses create_slot = false, copy_data = false, origin = none.
CREATE PUBLICATION bravo_rev FOR TABLE messages, bravo_barrier, attachments, message_attachments;
SELECT pg_create_logical_replication_slot('bravo_rev_sub', 'pgoutput');

-- Active object store for attachment rows written on this side (D-04).
INSERT INTO attachment_store (id, active_store) VALUES (1, :'attachment_store_id');

-- Planted regression for the first gate run (CONTRACT.md 3).
-- deploy/pilot/fix-dst-grant.sql revokes it.
GRANT SELECT ON messages TO PUBLIC;
