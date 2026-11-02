-- Row level security, role grants and the separation-of-duties trigger.
-- Tenant comes from `SET LOCAL app.tenant_id` (set_config(..., true)) per transaction.
-- An unset tenant yields NULL, so every tenant policy fails closed.

CREATE FUNCTION bravo_current_tenant() RETURNS text
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT nullif(current_setting('app.tenant_id', true), '') $$;

CREATE FUNCTION bravo_current_principal() RETURNS uuid
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT nullif(current_setting('app.principal_id', true), '')::uuid $$;

-- RLS on every business table -----------------------------------------------------------

ALTER TABLE tenants      ENABLE ROW LEVEL SECURITY; ALTER TABLE tenants      FORCE ROW LEVEL SECURITY;
ALTER TABLE principals   ENABLE ROW LEVEL SECURITY; ALTER TABLE principals   FORCE ROW LEVEL SECURITY;
ALTER TABLE memberships  ENABLE ROW LEVEL SECURITY; ALTER TABLE memberships  FORCE ROW LEVEL SECURITY;
ALTER TABLE environments ENABLE ROW LEVEL SECURITY; ALTER TABLE environments FORCE ROW LEVEL SECURITY;
ALTER TABLE plans        ENABLE ROW LEVEL SECURITY; ALTER TABLE plans        FORCE ROW LEVEL SECURITY;
ALTER TABLE gate_verdicts ENABLE ROW LEVEL SECURITY; ALTER TABLE gate_verdicts FORCE ROW LEVEL SECURITY;
ALTER TABLE events       ENABLE ROW LEVEL SECURITY; ALTER TABLE events       FORCE ROW LEVEL SECURITY;
ALTER TABLE approvals    ENABLE ROW LEVEL SECURITY; ALTER TABLE approvals    FORCE ROW LEVEL SECURITY;
ALTER TABLE idempotency  ENABLE ROW LEVEL SECURITY; ALTER TABLE idempotency  FORCE ROW LEVEL SECURITY;
ALTER TABLE runs         ENABLE ROW LEVEL SECURITY; ALTER TABLE runs         FORCE ROW LEVEL SECURITY;
ALTER TABLE run_stages   ENABLE ROW LEVEL SECURITY; ALTER TABLE run_stages   FORCE ROW LEVEL SECURITY;
ALTER TABLE effects      ENABLE ROW LEVEL SECURITY; ALTER TABLE effects      FORCE ROW LEVEL SECURITY;
ALTER TABLE receipts     ENABLE ROW LEVEL SECURITY; ALTER TABLE receipts     FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON tenants USING (tenant_id = bravo_current_tenant());
CREATE POLICY own_or_tenant ON memberships
  USING (tenant_id = bravo_current_tenant() OR principal_id = bravo_current_principal());
CREATE POLICY tenant_members ON principals
  USING (id = bravo_current_principal()
         OR EXISTS (SELECT 1 FROM memberships m WHERE m.principal_id = principals.id
                    AND m.tenant_id = bravo_current_tenant()));

CREATE POLICY tenant_isolation ON environments
  USING (tenant_id = bravo_current_tenant()) WITH CHECK (tenant_id = bravo_current_tenant());
CREATE POLICY tenant_isolation ON plans
  USING (tenant_id = bravo_current_tenant()) WITH CHECK (tenant_id = bravo_current_tenant());
CREATE POLICY tenant_isolation ON gate_verdicts
  USING (tenant_id = bravo_current_tenant()) WITH CHECK (tenant_id = bravo_current_tenant());
CREATE POLICY tenant_isolation ON events
  USING (tenant_id = bravo_current_tenant()) WITH CHECK (tenant_id = bravo_current_tenant());
CREATE POLICY tenant_isolation ON approvals
  USING (tenant_id = bravo_current_tenant()) WITH CHECK (tenant_id = bravo_current_tenant());

-- Idempotency: the API may record any action; the MCP role may only record the three
-- operator-MCP write actions, never approvals or execution starts (08 9).
CREATE POLICY api_tenant ON idempotency TO bravo_api
  USING (tenant_id = bravo_current_tenant()) WITH CHECK (tenant_id = bravo_current_tenant());
CREATE POLICY mcp_tenant ON idempotency TO bravo_mcp
  USING (tenant_id = bravo_current_tenant())
  WITH CHECK (tenant_id = bravo_current_tenant()
              AND action IN ('env_register', 'plan_create', 'gate_run'));

-- Executor tables keep the contract columns (no tenant_id). Readers see a run only through
-- a plan visible in their tenant. The Executor is a single-tenant data-plane cell in the
-- pilot; its role is scoped by grants (these four tables only), not by tenant.
CREATE POLICY via_plan ON runs TO bravo_api, bravo_mcp
  USING (EXISTS (SELECT 1 FROM plans p WHERE p.plan_id = runs.plan_id));
CREATE POLICY via_run ON run_stages TO bravo_api, bravo_mcp
  USING (EXISTS (SELECT 1 FROM runs r WHERE r.run_id = run_stages.run_id));
CREATE POLICY via_run ON effects TO bravo_api, bravo_mcp
  USING (EXISTS (SELECT 1 FROM runs r WHERE r.run_id = effects.run_id));
CREATE POLICY via_run ON receipts TO bravo_api, bravo_mcp
  USING (EXISTS (SELECT 1 FROM runs r WHERE r.run_id = receipts.run_id));
CREATE POLICY executor_cell ON runs       TO bravo_executor USING (true) WITH CHECK (true);
CREATE POLICY executor_cell ON run_stages TO bravo_executor USING (true) WITH CHECK (true);
CREATE POLICY executor_cell ON effects    TO bravo_executor USING (true) WITH CHECK (true);
CREATE POLICY executor_cell ON receipts   TO bravo_executor USING (true) WITH CHECK (true);

-- Principal lookup before the tenant is known. Exact (iss, sub) match only; returns the
-- caller's own memberships. SECURITY DEFINER with a pinned search_path.
CREATE FUNCTION bravo_resolve_principal(p_iss text, p_sub text)
  RETURNS TABLE (principal_id uuid, email text, tenant_id text, role text)
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, public
  AS $$
    SELECT p.id, p.email, m.tenant_id, m.role
      FROM principals p JOIN memberships m ON m.principal_id = p.id
     WHERE p.iss = p_iss AND p.sub = p_sub
     ORDER BY m.tenant_id, m.role
  $$;
REVOKE ALL ON FUNCTION bravo_resolve_principal(text, text) FROM PUBLIC;

-- Separation of duties (README "승인 결정", A4). Proposer and requester come from the
-- plan row, not from the application, and must differ from the approver. The binding
-- (state, request id, revision, verdict) is re-checked here so a buggy caller cannot
-- record an approval for a stale revision.
CREATE FUNCTION bravo_approvals_bind() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public
  AS $$
DECLARE
  p plans%ROWTYPE;
BEGIN
  SELECT * INTO p FROM plans WHERE plan_id = NEW.plan_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'plan_not_found' USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF p.state <> 'AWAIT_APPROVAL' THEN
    RAISE EXCEPTION 'state_mismatch' USING ERRCODE = 'check_violation';
  END IF;
  IF p.approval_request_id IS DISTINCT FROM NEW.approval_request_id
     OR p.revision <> NEW.plan_revision
     OR p.last_gate_verdict_id IS DISTINCT FROM NEW.gate_verdict_id
     OR p.approval_request_expires_at IS DISTINCT FROM NEW.expires_at
     OR p.approval_request_expires_at <= now() THEN
    RAISE EXCEPTION 'approval_stale' USING ERRCODE = 'check_violation';
  END IF;
  NEW.proposer_id := p.created_by;
  NEW.requester_id := p.approval_requested_by;
  IF NEW.requester_id IS NULL THEN
    RAISE EXCEPTION 'approval_request_missing' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.approver_id = NEW.proposer_id OR NEW.approver_id = NEW.requester_id THEN
    RAISE EXCEPTION 'separation_of_duties' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER approvals_bind BEFORE INSERT ON approvals
  FOR EACH ROW EXECUTE FUNCTION bravo_approvals_bind();

-- Approvals are append-only except for consumption.
CREATE FUNCTION bravo_approvals_immutable() RETURNS trigger
  LANGUAGE plpgsql
  AS $$
BEGIN
  IF (NEW.approval_id, NEW.tenant_id, NEW.plan_id, NEW.approval_request_id, NEW.stage, NEW.plan_revision,
      NEW.gate_verdict_id, NEW.approver_id, NEW.proposer_id, NEW.requester_id, NEW.expires_at, NEW.created_at)
     IS DISTINCT FROM
     (OLD.approval_id, OLD.tenant_id, OLD.plan_id, OLD.approval_request_id, OLD.stage, OLD.plan_revision,
      OLD.gate_verdict_id, OLD.approver_id, OLD.proposer_id, OLD.requester_id, OLD.expires_at, OLD.created_at)
  THEN
    RAISE EXCEPTION 'approval_immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.consumed_at IS NOT NULL THEN
    RAISE EXCEPTION 'approval_consumed' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER approvals_immutable BEFORE UPDATE ON approvals
  FOR EACH ROW EXECUTE FUNCTION bravo_approvals_immutable();

-- Grants -----------------------------------------------------------------------------------

GRANT EXECUTE ON FUNCTION bravo_current_tenant(), bravo_current_principal() TO bravo_api, bravo_mcp;
GRANT EXECUTE ON FUNCTION bravo_resolve_principal(text, text) TO bravo_api, bravo_mcp;

-- Console API role.
GRANT SELECT ON tenants, principals, memberships TO bravo_api;
GRANT SELECT, INSERT ON environments, gate_verdicts, events TO bravo_api;
GRANT SELECT, INSERT, UPDATE ON plans TO bravo_api;
GRANT SELECT, INSERT ON approvals TO bravo_api;
GRANT UPDATE (consumed_at, consumed_by, run_idem_key) ON approvals TO bravo_api;
GRANT SELECT, INSERT ON idempotency TO bravo_api;
GRANT UPDATE (status_code, response) ON idempotency TO bravo_api;
GRANT SELECT ON runs, run_stages, effects, receipts TO bravo_api;

-- Operator MCP role: no write on approvals, no execution columns on plans (08 9).
GRANT SELECT ON tenants, principals, memberships TO bravo_mcp;
GRANT SELECT, INSERT ON environments, plans, gate_verdicts, events TO bravo_mcp;
GRANT UPDATE (state, state_reason, version, updated_at, event_seq, last_gate_verdict_id,
              approval_request_id, approval_request_expires_at, approval_requested_by,
              collect_snapshot, collected_at) ON plans TO bravo_mcp;
GRANT SELECT ON approvals TO bravo_mcp;
GRANT SELECT, INSERT ON idempotency TO bravo_mcp;
GRANT UPDATE (status_code, response) ON idempotency TO bravo_mcp;
GRANT SELECT ON runs, run_stages, effects, receipts TO bravo_mcp;

-- Executor role: the four contract tables only.
GRANT SELECT, INSERT, UPDATE ON runs, run_stages, effects, receipts TO bravo_executor;

-- Run watcher resume after restart: plans with an in-flight run, across tenants.
-- Returns identifiers only; the watcher then works inside each plan's tenant.
-- run_id is NULL when the API stopped between consuming the approval and recording the
-- Executor's answer; run_idem_key then lets the watcher look the run up instead of re-sending.
CREATE FUNCTION bravo_inflight_runs()
  RETURNS TABLE (tenant_id text, plan_id uuid, run_id uuid, run_idem_key text)
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, public
  AS $$
    SELECT p.tenant_id, p.plan_id, p.active_run_id, a.run_idem_key
      FROM plans p
      LEFT JOIN LATERAL (SELECT run_idem_key FROM approvals
                          WHERE approvals.plan_id = p.plan_id AND consumed_at IS NOT NULL
                          ORDER BY consumed_at DESC LIMIT 1) a ON true
     WHERE p.state = 'CUTOVER'
  $$;
REVOKE ALL ON FUNCTION bravo_inflight_runs() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bravo_inflight_runs() TO bravo_api;
