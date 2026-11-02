-- One active rehearsal per ROLE per plan (S2: the console starts governance and app_migration together at 0:00).
-- The FR-5 budget (3 FAIL verdicts, infrastructure-error cap) is kept per role in plans.rehearsal_budget:
-- {"<role>": {"failures": n, "infra_errors": n}}. A person's reset (NEEDS_HUMAN -> PROPOSE) empties it.
-- plans.rehearsal_failures / rehearsal_infra_errors keep the most recently settled role's counts for the console.
ALTER TABLE plans ADD COLUMN rehearsal_budget jsonb NOT NULL DEFAULT '{}'::jsonb;
GRANT UPDATE (rehearsal_budget) ON plans TO bravo_mcp;
-- Lookups by plan and role (the per-role "already running" check and the queued-change pick).
CREATE INDEX rehearsals_plan_role ON rehearsals (plan_id, role, status);
