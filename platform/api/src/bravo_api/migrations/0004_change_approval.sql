-- Spec r2 (§8.2 REHEARSE row, T10, T31): two approval kinds.
--   change  : after REHEARSE passes. Bound to the rehearsal artifact (diff), the image digest the
--             trusted pipeline built from it and the policy diff. Then the destination apply (T31),
--             then the GATE re-check.
--   cutover : bound to the plan revision and the gate result (unchanged). failback: unchanged.
-- The T31 pipeline is a separate worker (platform/apply) with its own role ``bravo_apply``. It talks
-- to the platform through the ``changes`` / ``change_stages`` tables only, like the Executor does with
-- ``runs`` / ``run_stages`` (CONTRACT.md 5). The API owns every state transition. Applied once, after 0003.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bravo_apply') THEN
    CREATE ROLE bravo_apply LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO bravo_apply;

-- Plans: the change approval request lives in REHEARSE --------------------------------------
ALTER TABLE plans DROP CONSTRAINT plans_approval_stage_check;
ALTER TABLE plans ADD CONSTRAINT plans_approval_stage_check
  CHECK (approval_stage IN ('change', 'cutover', 'failback'));
ALTER TABLE plans DROP CONSTRAINT plans_rehearsal_check;
ALTER TABLE plans ADD CONSTRAINT plans_rehearsal_check
  CHECK (rehearsal IN ('not_started', 'running', 'passed', 'failed', 'infra_error', 'needs_human'));
ALTER TABLE plans
  ADD COLUMN active_change_id uuid,   -- the change being prepared / approved / applied
  ADD COLUMN last_change_id   uuid,
  ADD COLUMN applied_image_digest text;  -- digest the destination runs after the last applied change

-- Changes: one row per change approval cycle ---------------------------------------------------
CREATE TABLE changes (
  change_id             uuid PRIMARY KEY,
  tenant_id             text NOT NULL,
  plan_id               uuid NOT NULL,
  rehearsal_id          uuid NOT NULL REFERENCES rehearsals (rehearsal_id),
  plan_revision         int NOT NULL,
  target_environment_id uuid NOT NULL,
  role                  text NOT NULL CHECK (role IN ('governance', 'app_migration', 'ci')),
  -- Bound values. The API computes the first four from the frozen rehearsal artifact.
  artifact_digest       text NOT NULL CHECK (artifact_digest ~ '^sha256:[0-9a-f]{64}$'),
  diff_digest           text CHECK (diff_digest ~ '^sha256:[0-9a-f]{64}$'),          -- sha256 of the unified diff text
  policy_diff_digest    text CHECK (policy_diff_digest ~ '^sha256:[0-9a-f]{64}$'),   -- sha256 of canonical translations
  policy_targets        jsonb NOT NULL DEFAULT '[]'::jsonb,                            -- [{type, name}] approved scope
  -- Written by the pipeline when it prepares the candidate (build, SBOM, scan).
  image_repository      text,
  image_digest          text CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
  sbom_digest           text CHECK (sbom_digest ~ '^sha256:[0-9a-f]{64}$'),
  scan                  jsonb,       -- scanner, version, db, counts by severity, policy result
  build                 jsonb,       -- builder, pinned tool versions, base source digest, reproducible
  -- Written by the pipeline when it applies.
  signature             jsonb,       -- key ref, signed digest, verify result
  deployed_digest       text CHECK (deployed_digest ~ '^sha256:[0-9a-f]{64}$'),
  policies_applied      jsonb,
  gate_recheck          jsonb,       -- the gate service report the pipeline read after applying
  status                text NOT NULL CHECK (status IN ('requested', 'preparing', 'prepared', 'prepare_failed',
                          'approved', 'applying', 'applied', 'refused', 'failed', 'superseded')),
  reason                text,
  worker                text,
  stage_seq             int NOT NULL DEFAULT 0,  -- last change_stages row turned into a plan event
  requested_by          uuid NOT NULL REFERENCES principals (id),  -- the operator who asked for the change
  approval_request_id   uuid,
  approval_id           uuid,
  created_at            timestamptz NOT NULL DEFAULT now(),
  prepared_at           timestamptz,
  approved_at           timestamptz,
  ended_at              timestamptz,
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, change_id),
  FOREIGN KEY (tenant_id, plan_id) REFERENCES plans (tenant_id, plan_id),
  -- T31: "deploy only if the deployed digest equals the approved digest", enforced by the DB too.
  CONSTRAINT changes_applied_digest CHECK (status <> 'applied' OR image_digest IS NULL OR deployed_digest = image_digest),
  CONSTRAINT changes_prepared_has_scope CHECK (
    status IN ('requested', 'preparing', 'prepare_failed', 'superseded')
    OR image_digest IS NOT NULL OR policy_diff_digest IS NOT NULL)
);
CREATE UNIQUE INDEX changes_one_open ON changes (plan_id)
  WHERE status IN ('requested', 'preparing', 'prepared', 'approved', 'applying');
CREATE INDEX changes_status_idx ON changes (status, created_at);

CREATE TABLE change_stages (
  change_id  uuid NOT NULL REFERENCES changes (change_id),
  seq        int NOT NULL CHECK (seq >= 1),
  phase      text NOT NULL CHECK (phase IN ('prepare', 'apply')),
  name       text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_.-]{0,62}$'),
  started_at timestamptz NOT NULL,
  ms         int,
  result     text NOT NULL CHECK (result IN ('OK', 'FAIL', 'REFUSED', 'NOT_CONNECTED', 'SKIPPED')),
  detail     jsonb,
  PRIMARY KEY (change_id, seq)
);

ALTER TABLE changes       ENABLE ROW LEVEL SECURITY; ALTER TABLE changes       FORCE ROW LEVEL SECURITY;
ALTER TABLE change_stages ENABLE ROW LEVEL SECURITY; ALTER TABLE change_stages FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON changes TO bravo_api, bravo_mcp
  USING (tenant_id = bravo_current_tenant()) WITH CHECK (tenant_id = bravo_current_tenant());
CREATE POLICY via_change ON change_stages TO bravo_api, bravo_mcp
  USING (EXISTS (SELECT 1 FROM changes c WHERE c.change_id = change_stages.change_id));
-- The pipeline is a single data-plane cell like the Executor: scoped by grants, not by tenant.
CREATE POLICY apply_cell ON changes       TO bravo_apply USING (true) WITH CHECK (true);
CREATE POLICY apply_cell ON change_stages TO bravo_apply USING (true) WITH CHECK (true);
CREATE POLICY apply_cell ON rehearsals    TO bravo_apply USING (true);
CREATE POLICY apply_cell ON approvals     TO bravo_apply USING (true);
CREATE POLICY apply_cell ON environments  TO bravo_apply USING (true);

-- Who may move a change where. The pipeline cannot approve, cannot touch bound values after
-- preparing, and cannot report "applied" for a change nobody approved.
CREATE FUNCTION bravo_changes_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public
  AS $$
DECLARE
  who text := current_user;
  ok boolean;
BEGIN
  IF (NEW.change_id, NEW.tenant_id, NEW.plan_id, NEW.rehearsal_id, NEW.plan_revision, NEW.target_environment_id,
      NEW.role, NEW.artifact_digest, NEW.diff_digest, NEW.policy_diff_digest, NEW.policy_targets, NEW.requested_by,
      NEW.created_at)
     IS DISTINCT FROM
     (OLD.change_id, OLD.tenant_id, OLD.plan_id, OLD.rehearsal_id, OLD.plan_revision, OLD.target_environment_id,
      OLD.role, OLD.artifact_digest, OLD.diff_digest, OLD.policy_diff_digest, OLD.policy_targets, OLD.requested_by,
      OLD.created_at) THEN
    RAISE EXCEPTION 'change_binding_immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.status NOT IN ('requested', 'preparing')
     AND (NEW.image_repository, NEW.image_digest, NEW.sbom_digest, NEW.scan, NEW.build)
         IS DISTINCT FROM (OLD.image_repository, OLD.image_digest, OLD.sbom_digest, OLD.scan, OLD.build) THEN
    RAISE EXCEPTION 'change_candidate_immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    ok := CASE
      WHEN who = 'bravo_apply' THEN (OLD.status, NEW.status) IN (
        ('requested', 'preparing'), ('preparing', 'prepared'), ('preparing', 'prepare_failed'),
        ('approved', 'applying'), ('applying', 'applied'), ('applying', 'refused'), ('applying', 'failed'))
      WHEN who = 'bravo_api' THEN (OLD.status, NEW.status) IN (
        ('prepared', 'approved'), ('requested', 'superseded'), ('prepared', 'superseded'),
        ('prepare_failed', 'superseded'))
      ELSE false
    END;
    IF NOT ok THEN
      RAISE EXCEPTION 'change_status_not_allowed' USING ERRCODE = 'check_violation',
        DETAIL = format('%s: %s -> %s', who, OLD.status, NEW.status);
    END IF;
    IF NEW.status = 'approved' AND NOT EXISTS (
         SELECT 1 FROM approvals a WHERE a.approval_id = NEW.approval_id AND a.stage = 'change'
            AND a.change_id = NEW.change_id AND a.image_digest IS NOT DISTINCT FROM NEW.image_digest
            AND a.policy_diff_digest IS NOT DISTINCT FROM NEW.policy_diff_digest) THEN
      RAISE EXCEPTION 'change_not_approved' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF who = 'bravo_apply' AND NEW.approval_id IS DISTINCT FROM OLD.approval_id THEN
    RAISE EXCEPTION 'change_approval_owned_by_api' USING ERRCODE = 'check_violation';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
CREATE TRIGGER changes_guard BEFORE UPDATE ON changes FOR EACH ROW EXECUTE FUNCTION bravo_changes_guard();

-- Approvals: stage change binds the change row and its three digests -------------------------
ALTER TABLE approvals DROP CONSTRAINT approvals_stage_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_stage_check CHECK (stage IN ('change', 'cutover', 'failback'));
ALTER TABLE approvals
  ADD COLUMN change_id          uuid REFERENCES changes (change_id),
  ADD COLUMN diff_digest        text,
  ADD COLUMN image_digest       text,
  ADD COLUMN policy_diff_digest text;
ALTER TABLE approvals DROP CONSTRAINT approvals_stage_binding;
ALTER TABLE approvals ADD CONSTRAINT approvals_stage_binding CHECK (
  (stage = 'cutover' AND gate_verdict_id IS NOT NULL)
  OR (stage = 'failback' AND bound_run_id IS NOT NULL)
  OR (stage = 'change' AND change_id IS NOT NULL AND artifact_digest IS NOT NULL
      AND (image_digest IS NOT NULL OR policy_diff_digest IS NOT NULL)));

CREATE OR REPLACE FUNCTION bravo_approvals_bind() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public
  AS $$
DECLARE
  p plans%ROWTYPE;
  c changes%ROWTYPE;
BEGIN
  SELECT * INTO p FROM plans WHERE plan_id = NEW.plan_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'plan_not_found' USING ERRCODE = 'foreign_key_violation';
  END IF;
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
  ELSIF NEW.stage = 'failback' THEN
    IF p.state <> 'WATCH' THEN
      RAISE EXCEPTION 'state_mismatch' USING ERRCODE = 'check_violation';
    END IF;
    IF p.last_run_id IS DISTINCT FROM NEW.bound_run_id THEN
      RAISE EXCEPTION 'approval_stale' USING ERRCODE = 'check_violation';
    END IF;
  ELSE  -- change
    IF p.state <> 'REHEARSE' THEN
      RAISE EXCEPTION 'state_mismatch' USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO c FROM changes WHERE change_id = NEW.change_id FOR UPDATE;
    IF NOT FOUND OR c.plan_id <> NEW.plan_id OR c.status <> 'prepared'
       OR p.active_change_id IS DISTINCT FROM c.change_id
       OR c.approval_request_id IS DISTINCT FROM NEW.approval_request_id
       OR c.plan_revision <> NEW.plan_revision
       OR c.artifact_digest IS DISTINCT FROM NEW.artifact_digest
       OR c.diff_digest IS DISTINCT FROM NEW.diff_digest
       OR c.image_digest IS DISTINCT FROM NEW.image_digest
       OR c.policy_diff_digest IS DISTINCT FROM NEW.policy_diff_digest THEN
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
      NEW.artifact_digest, NEW.bound_run_id, NEW.change_id, NEW.diff_digest, NEW.image_digest, NEW.policy_diff_digest)
     IS DISTINCT FROM
     (OLD.approval_id, OLD.tenant_id, OLD.plan_id, OLD.approval_request_id, OLD.stage, OLD.plan_revision,
      OLD.gate_verdict_id, OLD.approver_id, OLD.proposer_id, OLD.requester_id, OLD.expires_at, OLD.created_at,
      OLD.artifact_digest, OLD.bound_run_id, OLD.change_id, OLD.diff_digest, OLD.image_digest, OLD.policy_diff_digest)
  THEN
    RAISE EXCEPTION 'approval_immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.consumed_at IS NOT NULL THEN
    RAISE EXCEPTION 'approval_consumed' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- Gate verdicts: the post-apply re-check is run by the system, not by a person -----------------
ALTER TABLE gate_verdicts DROP CONSTRAINT gate_verdicts_channel_check;
ALTER TABLE gate_verdicts ADD CONSTRAINT gate_verdicts_channel_check CHECK (channel IN ('console', 'mcp', 'system'));
ALTER TABLE gate_verdicts ALTER COLUMN requested_by DROP NOT NULL;
ALTER TABLE gate_verdicts ADD COLUMN purpose text NOT NULL DEFAULT 'gate'
  CHECK (purpose IN ('gate', 'control_retest', 'change_recheck'));
ALTER TABLE gate_verdicts ADD CONSTRAINT gate_verdicts_requester
  CHECK ((channel = 'system') = (requested_by IS NULL));

-- Events ------------------------------------------------------------------------------------------
ALTER TABLE events DROP CONSTRAINT events_kind_check;
ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN (
  'state.changed', 'gate.verdict', 'approval.requested', 'approval.granted', 'run.stage', 'run.done',
  'receipt.saved', 'analysis.done', 'trial.started', 'trial.proposed', 'tool.denied', 'trial.verdict',
  'change.requested', 'change.stage', 'change.prepared', 'change.prepare_failed', 'change.applied',
  'change.refused', 'change.failed'));

-- Environments: the secret reference is resolved against the platform OpenBao (audit B11) ---------
ALTER TABLE environments
  ADD COLUMN secret_status text NOT NULL DEFAULT 'unverified'
    CHECK (secret_status IN ('resolved', 'unverified')),
  ADD COLUMN secret_reason text,
  ADD COLUMN secret_checked_at timestamptz;

-- Watcher: in-flight changes across tenants, identifiers only ----------------------------------------
CREATE FUNCTION bravo_inflight_changes()
  RETURNS TABLE (tenant_id text, plan_id uuid, change_id uuid)
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, public
  AS $$
    SELECT c.tenant_id, c.plan_id, c.change_id FROM changes c
      JOIN plans p ON p.plan_id = c.plan_id AND p.active_change_id = c.change_id
     WHERE c.status IN ('requested', 'preparing', 'prepared', 'prepare_failed', 'approved', 'applying',
                        'applied', 'refused', 'failed')
  $$;
REVOKE ALL ON FUNCTION bravo_inflight_changes() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bravo_inflight_changes() TO bravo_api;

-- Grants --------------------------------------------------------------------------------------------
GRANT SELECT, INSERT ON changes TO bravo_api;
GRANT UPDATE (status, reason, approval_request_id, approval_id, approved_at, stage_seq, updated_at) ON changes TO bravo_api;
GRANT SELECT ON change_stages TO bravo_api;
GRANT UPDATE (active_change_id, last_change_id, applied_image_digest) ON plans TO bravo_api;
GRANT UPDATE (secret_status, secret_reason, secret_checked_at) ON environments TO bravo_api;
GRANT SELECT ON changes, change_stages TO bravo_mcp;

GRANT SELECT ON changes, change_stages, rehearsals, approvals, environments TO bravo_apply;
GRANT UPDATE (status, reason, worker, image_repository, image_digest, sbom_digest, scan, build, signature,
              deployed_digest, policies_applied, gate_recheck, prepared_at, ended_at, updated_at)
  ON changes TO bravo_apply;
GRANT INSERT ON change_stages TO bravo_apply;

-- Audit A2: the Executor verifies the approval it is given against this table with its own role.
CREATE POLICY executor_cell ON approvals TO bravo_executor USING (true);
GRANT SELECT ON approvals TO bravo_executor;
