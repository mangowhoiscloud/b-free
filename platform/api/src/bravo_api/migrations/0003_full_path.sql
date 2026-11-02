-- Full spec path (§8.2): ANALYZE, PROPOSE, REHEARSE, NEEDS_HUMAN, WATCH -> DONE, FAILBACK.
-- Replaces the pilot's `rehearsal: not_in_scope` marker with a real rehearsal record (FR-5, S2)
-- and adds the failback approval stage (S4). Applied once, after 0001 and 0002.

-- States ----------------------------------------------------------------------------------
ALTER TABLE plans DROP CONSTRAINT plans_state_check;
ALTER TABLE plans ADD CONSTRAINT plans_state_check CHECK (state IN (
  'COLLECT', 'ANALYZE', 'PROPOSE', 'REHEARSE', 'GATE', 'AWAIT_APPROVAL', 'SHIFT', 'CUTOVER', 'WATCH',
  'DONE', 'FAILBACK', 'DONE_ROLLED_BACK', 'NEEDS_HUMAN', 'SAFE_HOLD', 'ABORT_PRE_WRITE'));

-- Rehearsal status on the plan: what the console shows instead of the pilot marker.
ALTER TABLE plans DROP CONSTRAINT plans_rehearsal_check;
UPDATE plans SET rehearsal = 'not_started' WHERE rehearsal = 'not_in_scope';
ALTER TABLE plans ALTER COLUMN rehearsal SET DEFAULT 'not_started';
ALTER TABLE plans ADD CONSTRAINT plans_rehearsal_check
  CHECK (rehearsal IN ('not_started', 'running', 'passed', 'failed', 'infra_error', 'needs_human'));

ALTER TABLE plans
  ADD COLUMN analysis              jsonb,                       -- ANALYZE output: gaps + inventory status
  ADD COLUMN analyzed_at           timestamptz,
  ADD COLUMN approval_stage        text CHECK (approval_stage IN ('cutover', 'failback')),
  ADD COLUMN active_rehearsal_id   uuid,
  ADD COLUMN last_rehearsal_id     uuid,
  ADD COLUMN rehearsal_failures    int NOT NULL DEFAULT 0 CHECK (rehearsal_failures >= 0),   -- FAIL verdicts (FR-5 cap 3)
  ADD COLUMN rehearsal_infra_errors int NOT NULL DEFAULT 0 CHECK (rehearsal_infra_errors >= 0),
  ADD COLUMN approved_artifact_digest text;                     -- artifact of the last passing rehearsal
UPDATE plans SET approval_stage = 'cutover' WHERE approval_request_id IS NOT NULL;

-- Rehearsals: one row per attempt. The attempt counter lives here, owned by the platform DB
-- (05 6 "재시도 카운터 주인"). Terminal status is set once (CAS on status).
CREATE TABLE rehearsals (
  rehearsal_id     uuid PRIMARY KEY,
  tenant_id        text NOT NULL,
  plan_id          uuid NOT NULL,
  attempt          int NOT NULL CHECK (attempt >= 1),
  task             text NOT NULL CHECK (task ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  role             text NOT NULL CHECK (role IN ('discovery', 'db', 'governance', 'app_migration', 'ci', 'report')),
  trial_id         text,                                        -- assigned by the rehearsal service
  idem_key         text NOT NULL UNIQUE,                        -- Idempotency-Key sent to the rehearsal service
  status           text NOT NULL CHECK (status IN ('requested', 'running', 'proposed', 'PASS', 'FAIL', 'INFRA_ERROR')),
  reason           text,
  artifact_digest  text,
  artifact         jsonb,
  verifier         jsonb,                                       -- verifier run id, pod uid, image digest, reward
  tool_seq         int NOT NULL DEFAULT 0,                      -- last tool event turned into a plan event
  requested_by     uuid NOT NULL REFERENCES principals (id),
  channel          text NOT NULL CHECK (channel IN ('console', 'mcp')),
  created_at       timestamptz NOT NULL DEFAULT now(),
  ended_at         timestamptz,
  UNIQUE (plan_id, attempt, idem_key),
  FOREIGN KEY (tenant_id, plan_id) REFERENCES plans (tenant_id, plan_id)
);
CREATE INDEX rehearsals_plan_idx ON rehearsals (plan_id, created_at);
ALTER TABLE rehearsals ENABLE ROW LEVEL SECURITY; ALTER TABLE rehearsals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rehearsals
  USING (tenant_id = bravo_current_tenant()) WITH CHECK (tenant_id = bravo_current_tenant());

-- Events -----------------------------------------------------------------------------------
ALTER TABLE events DROP CONSTRAINT events_kind_check;
ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN (
  'state.changed', 'gate.verdict', 'approval.granted', 'run.stage', 'run.done', 'receipt.saved',
  'analysis.done', 'trial.started', 'trial.proposed', 'tool.denied', 'trial.verdict'));

-- Approvals: stage failback (S4) binds the plan revision and the cutover run it reverses;
-- stage cutover binds the gate verdict and the rehearsal artifact digest (FR-7, S2-AC3).
ALTER TABLE approvals DROP CONSTRAINT approvals_stage_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_stage_check CHECK (stage IN ('cutover', 'failback'));
ALTER TABLE approvals ALTER COLUMN gate_verdict_id DROP NOT NULL;
ALTER TABLE approvals
  ADD COLUMN artifact_digest text,
  ADD COLUMN bound_run_id uuid,
  ADD CONSTRAINT approvals_stage_binding CHECK (
    (stage = 'cutover' AND gate_verdict_id IS NOT NULL)
    OR (stage = 'failback' AND bound_run_id IS NOT NULL));

CREATE OR REPLACE FUNCTION bravo_approvals_bind() RETURNS trigger
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
  -- COALESCE: an API image from before 0003 never sets approval_stage; its requests are cutover.
  IF COALESCE(p.approval_stage, 'cutover') IS DISTINCT FROM NEW.stage THEN
    RAISE EXCEPTION 'approval_stale' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.stage = 'cutover' THEN
    IF p.state <> 'AWAIT_APPROVAL' THEN
      RAISE EXCEPTION 'state_mismatch' USING ERRCODE = 'check_violation';
    END IF;
    IF p.last_gate_verdict_id IS DISTINCT FROM NEW.gate_verdict_id
       OR p.approved_artifact_digest IS DISTINCT FROM NEW.artifact_digest THEN
      RAISE EXCEPTION 'approval_stale' USING ERRCODE = 'check_violation';
    END IF;
  ELSE
    IF p.state <> 'WATCH' THEN
      RAISE EXCEPTION 'state_mismatch' USING ERRCODE = 'check_violation';
    END IF;
    IF p.last_run_id IS DISTINCT FROM NEW.bound_run_id THEN
      RAISE EXCEPTION 'approval_stale' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF p.approval_request_id IS DISTINCT FROM NEW.approval_request_id
     OR p.revision <> NEW.plan_revision
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

CREATE OR REPLACE FUNCTION bravo_approvals_immutable() RETURNS trigger
  LANGUAGE plpgsql
  AS $$
BEGIN
  IF (NEW.approval_id, NEW.tenant_id, NEW.plan_id, NEW.approval_request_id, NEW.stage, NEW.plan_revision,
      NEW.gate_verdict_id, NEW.approver_id, NEW.proposer_id, NEW.requester_id, NEW.expires_at, NEW.created_at,
      NEW.artifact_digest, NEW.bound_run_id)
     IS DISTINCT FROM
     (OLD.approval_id, OLD.tenant_id, OLD.plan_id, OLD.approval_request_id, OLD.stage, OLD.plan_revision,
      OLD.gate_verdict_id, OLD.approver_id, OLD.proposer_id, OLD.requester_id, OLD.expires_at, OLD.created_at,
      OLD.artifact_digest, OLD.bound_run_id)
  THEN
    RAISE EXCEPTION 'approval_immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.consumed_at IS NOT NULL THEN
    RAISE EXCEPTION 'approval_consumed' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- Idempotency: the MCP role may now also record rehearsal_start (08 9: rehearsal + gate only).
DROP POLICY mcp_tenant ON idempotency;
CREATE POLICY mcp_tenant ON idempotency TO bravo_mcp
  USING (tenant_id = bravo_current_tenant())
  WITH CHECK (tenant_id = bravo_current_tenant()
              AND action IN ('env_register', 'plan_create', 'gate_run', 'rehearsal_start'));

-- In-flight runs now include failback.
CREATE OR REPLACE FUNCTION bravo_inflight_runs()
  RETURNS TABLE (tenant_id text, plan_id uuid, run_id uuid, run_idem_key text)
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, public
  AS $$
    SELECT p.tenant_id, p.plan_id, p.active_run_id, a.run_idem_key
      FROM plans p
      LEFT JOIN LATERAL (SELECT run_idem_key FROM approvals
                          WHERE approvals.plan_id = p.plan_id AND consumed_at IS NOT NULL
                          ORDER BY consumed_at DESC LIMIT 1) a ON true
     WHERE p.state IN ('CUTOVER', 'FAILBACK')
  $$;

-- In-flight rehearsals across tenants, identifiers only (the watcher then works per tenant).
CREATE FUNCTION bravo_inflight_rehearsals()
  RETURNS TABLE (tenant_id text, plan_id uuid, rehearsal_id uuid)
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, public
  AS $$
    SELECT r.tenant_id, r.plan_id, r.rehearsal_id FROM rehearsals r
     WHERE r.status IN ('requested', 'running', 'proposed')
  $$;
REVOKE ALL ON FUNCTION bravo_inflight_rehearsals() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bravo_inflight_rehearsals() TO bravo_api;

-- Grants -------------------------------------------------------------------------------------
GRANT SELECT, INSERT ON rehearsals TO bravo_api, bravo_mcp;
GRANT UPDATE (trial_id, status, reason, artifact_digest, artifact, verifier, tool_seq, ended_at)
  ON rehearsals TO bravo_api, bravo_mcp;
GRANT UPDATE (analysis, analyzed_at, approval_stage, active_rehearsal_id, last_rehearsal_id, rehearsal,
              rehearsal_failures, rehearsal_infra_errors, approved_artifact_digest, revision)
  ON plans TO bravo_mcp;
