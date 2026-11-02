-- Roles (CONTRACT.md 3). 00-bravo-init.sh hashes each password into a SCRAM-SHA-256 verifier
-- on the client side and exports it; \getenv reads it here, so neither psql's argv nor the SQL
-- text sent to the server carries a password. No secret is stored in this file.
\set ON_ERROR_STOP on
\getenv app_writer_verifier BRAVO_APP_WRITER_VERIFIER
\getenv replicator_verifier BRAVO_REPLICATOR_VERIFIER
\getenv executor_ctl_verifier BRAVO_EXECUTOR_CTL_VERIFIER
\getenv executor_observe_verifier BRAVO_EXECUTOR_OBSERVE_VERIFIER

-- Application writer: messages SELECT/INSERT and sequence USAGE only.
CREATE ROLE app_writer LOGIN PASSWORD :'app_writer_verifier';
GRANT SELECT, INSERT ON messages TO app_writer;
GRANT USAGE ON SEQUENCE messages_id_seq TO app_writer;
-- Attachments (D-04): insert and read; the only update is deleted_at. No privilege on
-- attachment_store or the guard functions. session_replication_role is superuser-only.
GRANT SELECT, INSERT ON attachments, message_attachments TO app_writer;
GRANT UPDATE (deleted_at) ON attachments TO app_writer;
-- Write-block switch: the app's admin port sets its own environment's row. UPDATE is also what
-- SELECT ... FOR SHARE and LOCK TABLE ... IN EXCLUSIVE MODE require.
GRANT SELECT, INSERT, UPDATE ON write_block TO app_writer;

-- Replication user used by the subscriptions on the other side.
CREATE ROLE replicator LOGIN REPLICATION PASSWORD :'replicator_verifier';
GRANT SELECT ON messages, bravo_barrier, attachments, message_attachments TO replicator;

-- Executor control role. REPLICATION: drop the forward slot at 6b and the reverse slot at
-- failback (pg_drop_replication_slot needs it); it never opens a walsender connection.
CREATE ROLE executor_ctl LOGIN CREATEROLE REPLICATION PASSWORD :'executor_ctl_verifier';
GRANT pg_signal_backend TO executor_ctl;          -- terminate app_writer sessions (step 4)
GRANT pg_monitor TO executor_ctl;                 -- pg_stat_subscription, pg_stat_activity, settings
GRANT pg_create_subscription TO executor_ctl;     -- owns bravo_fwd_sub / bravo_rev_sub
GRANT CREATE ON DATABASE chat TO executor_ctl;    -- required for a non-superuser subscription owner
-- PG16: a CREATEROLE role may only ALTER roles it has ADMIN on. No INHERIT, no SET.
GRANT app_writer TO executor_ctl WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
-- PG16: the subscription owner must be able to SET ROLE to the table owner for apply.
-- INHERIT FALSE: executor_ctl does not get chat_owner privileges in its own sessions. The
-- Executor uses SET LOCAL ROLE chat_owner only to REVOKE/GRANT the writer's INSERT (03 4.2 step 4).
GRANT chat_owner TO executor_ctl WITH INHERIT FALSE, SET TRUE;
-- CREATE SUBSCRIPTION carries the replicator password in its connection string: the Executor's
-- session turns statement logging off first (repl_setup.py _quiet), so no password reaches a log.
GRANT SET ON PARAMETER log_min_error_statement, log_statement TO executor_ctl;

GRANT SELECT, INSERT, UPDATE ON bravo_barrier, bravo_fence TO executor_ctl;
GRANT SELECT ON messages TO executor_ctl;
-- Attachment freeze and store switch (D-04): the platform updates attachment_store.
GRANT SELECT ON attachments, message_attachments TO executor_ctl;
GRANT SELECT, UPDATE ON attachment_store TO executor_ctl;
GRANT SELECT, INSERT, UPDATE ON write_block TO executor_ctl;
GRANT SELECT, UPDATE, USAGE ON SEQUENCE messages_id_seq, bravo_barrier_id_seq TO executor_ctl;

-- DB-AUTH control inputs (read-only).
GRANT SELECT ON pg_catalog.pg_hba_file_rules TO executor_ctl;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_hba_file_rules() TO executor_ctl;

CREATE SCHEMA bravo_catalog;
-- Returns only whether a password is set, never the secret.
CREATE FUNCTION bravo_catalog.role_has_password(role_name text)
  RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog
AS $$ SELECT rolpassword IS NOT NULL FROM pg_authid WHERE rolname = role_name $$;
REVOKE ALL ON FUNCTION bravo_catalog.role_has_password(text) FROM PUBLIC;
GRANT USAGE ON SCHEMA bravo_catalog TO executor_ctl;
GRANT EXECUTE ON FUNCTION bravo_catalog.role_has_password(text) TO executor_ctl;

-- Observe-only role (D-19): catalog, state and table reads only. Used by a restarted
-- Executor with unresolved effects; it cannot change roles, fences, subscriptions or data.
CREATE ROLE executor_observe LOGIN PASSWORD :'executor_observe_verifier';
GRANT pg_monitor TO executor_observe;
GRANT SELECT ON messages, bravo_barrier, bravo_fence, attachments, message_attachments, attachment_store
  TO executor_observe;
GRANT SELECT ON SEQUENCE messages_id_seq, bravo_barrier_id_seq TO executor_observe;
GRANT SELECT ON pg_catalog.pg_hba_file_rules TO executor_observe;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_hba_file_rules() TO executor_observe;
GRANT USAGE ON SCHEMA bravo_catalog TO executor_observe;
GRANT EXECUTE ON FUNCTION bravo_catalog.role_has_password(text) TO executor_observe;
