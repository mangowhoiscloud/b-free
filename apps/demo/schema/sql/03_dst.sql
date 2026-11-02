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

-- 심은 시나리오 (planted demo regression, CONTRACT.md 3): only with BRAVO_PLANT_REGRESSION=1.
-- It gives the first gate run something real to BLOCK on (DB-ANON-READ); it is not part of the
-- schema. deploy/pilot/fix-dst-grant.sql revokes it. Default (0): not planted.
\if :plant_regression
\echo 'bravo-init: 심은 시나리오 적용: GRANT SELECT ON messages TO PUBLIC (BRAVO_PLANT_REGRESSION=1)'
GRANT SELECT ON messages TO PUBLIC;
\else
\echo 'bravo-init: 심은 시나리오 없음 (BRAVO_PLANT_REGRESSION unset or 0)'
\endif
