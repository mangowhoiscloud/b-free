-- Read-only role for evidence_read (platform/rehearsal, D-26). Apply as the platform DB owner after
-- the platform/api migrations. The password comes from the environment (psql 15+ \getenv), never
-- from this file or argv:
--   BRAVO_REHEARSAL_RO_PASSWORD=... psql -v ON_ERROR_STOP=1 -f rehearsal_evidence_ro.sql
-- The role reads only the evidence columns of gate_verdicts, plans.analysis and rehearsals. The
-- existing tenant_isolation policies (FORCE RLS, no TO clause) apply to it, so every query must set
-- app.tenant_id; the tool also binds each row to the Trial's plan. Sessions are read-only.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bravo_rehearsal_ro') THEN
    CREATE ROLE bravo_rehearsal_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
  END IF;
END
$$;

\getenv rehearsal_ro_password BRAVO_REHEARSAL_RO_PASSWORD
\if :{?rehearsal_ro_password}
\else
  \warn 'BRAVO_REHEARSAL_RO_PASSWORD is not set'
  \quit 3
\endif
ALTER ROLE bravo_rehearsal_ro PASSWORD :'rehearsal_ro_password';
ALTER ROLE bravo_rehearsal_ro SET default_transaction_read_only = on;
ALTER ROLE bravo_rehearsal_ro SET statement_timeout = '5s';

GRANT USAGE ON SCHEMA public TO bravo_rehearsal_ro;
GRANT EXECUTE ON FUNCTION bravo_current_tenant() TO bravo_rehearsal_ro;
GRANT SELECT (verdict_id, tenant_id, plan_id, plan_revision, verdict, reasons, controls, readiness,
              input_digest, rule_version, created_at) ON gate_verdicts TO bravo_rehearsal_ro;
GRANT SELECT (plan_id, tenant_id, analysis, analyzed_at) ON plans TO bravo_rehearsal_ro;
GRANT SELECT (rehearsal_id, tenant_id, plan_id, attempt, task, role, status, reason, artifact_digest,
              verifier, created_at, ended_at) ON rehearsals TO bravo_rehearsal_ro;
