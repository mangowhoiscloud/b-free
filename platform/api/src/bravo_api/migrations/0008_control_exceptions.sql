-- Control exceptions (spec S1 exception flow 5b, contract 15 §9). Applied once, after 0007.
--
-- "목적지에서 대체할 수 없는 통제가 있다 -> 승인자가 예외를 승인하면 '예외 승인'으로 따로 표시하고 동등 판정에서
-- 뺀다." An operator requests an exception for one control of one plan, bound to the plan's destination, the
-- gate verdict it was seen in, the measured pair (dst FAIL, src PASS or FAIL) and the destination evidence
-- digest. A human approver (not the requester, not the plan's author) approves or rejects it in the console.
-- The API applies an approved, unexpired exception when it assembles a gate verdict and records it as stale
-- when the control is measured differently. UNKNOWN can never be excepted (5a): the insert check below needs
-- a measured FAIL row with evidence in the plan's last gate verdict.

CREATE TABLE control_exceptions (
  exception_id          uuid PRIMARY KEY,
  tenant_id             text NOT NULL,
  plan_id               uuid NOT NULL,
  control_id            text NOT NULL CHECK (control_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  target_environment_id uuid NOT NULL,
  gate_verdict_id       uuid NOT NULL REFERENCES gate_verdicts (verdict_id),
  src                   text NOT NULL CHECK (src IN ('PASS', 'FAIL')),
  dst                   text NOT NULL CHECK (dst = 'FAIL'),
  evidence_digest       text NOT NULL CHECK (evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
  reason                text NOT NULL CHECK (char_length(btrim(reason)) BETWEEN 10 AND 2000),
  -- Spec §8.5: an exception is shown with its approval, period and compensating control.
  compensating_control  text NOT NULL CHECK (char_length(btrim(compensating_control)) BETWEEN 10 AND 2000),
  expires_at            timestamptz NOT NULL,
  status                text NOT NULL DEFAULT 'requested'
                          CHECK (status IN ('requested', 'approved', 'rejected', 'stale', 'expired')),
  stale_reason          text CHECK (stale_reason IN ('evidence_changed', 'verdict_changed')),
  requested_by          uuid NOT NULL REFERENCES principals (id),
  requested_at          timestamptz NOT NULL DEFAULT now(),
  decided_by            uuid REFERENCES principals (id),
  decided_at            timestamptz,
  decision_note         text CHECK (decision_note IS NULL OR char_length(decision_note) <= 2000),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, exception_id),
  FOREIGN KEY (tenant_id, plan_id) REFERENCES plans (tenant_id, plan_id),
  FOREIGN KEY (tenant_id, target_environment_id) REFERENCES environments (tenant_id, environment_id),
  CONSTRAINT control_exceptions_decided CHECK (
    (status IN ('approved', 'rejected')) = (decided_by IS NOT NULL AND decided_at IS NOT NULL)
    OR (status IN ('stale', 'expired'))),
  CONSTRAINT control_exceptions_stale_reason CHECK ((status = 'stale') = (stale_reason IS NOT NULL)),
  CONSTRAINT control_exceptions_expiry CHECK (expires_at > requested_at)
);
-- One open exception per plan and control.
CREATE UNIQUE INDEX control_exceptions_one_open ON control_exceptions (plan_id, control_id)
  WHERE status IN ('requested', 'approved');
CREATE INDEX control_exceptions_plan_idx ON control_exceptions (plan_id, requested_at);

ALTER TABLE control_exceptions ENABLE ROW LEVEL SECURITY; ALTER TABLE control_exceptions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON control_exceptions TO bravo_api, bravo_mcp
  USING (tenant_id = bravo_current_tenant()) WITH CHECK (tenant_id = bravo_current_tenant());
-- Same owner policy as 0007 for every RLS table (the owner role name differs per database).
DO $$
BEGIN
  EXECUTE format('CREATE POLICY owner_definer ON public.control_exceptions TO %I USING (true) WITH CHECK (true)',
                 current_user);
END $$;

-- Gate verdicts: the exceptions applied to that verdict (binding + expiry + approver + compensating control),
-- shown as "예외 승인", and the end of the gate evidence validity (gate report expires_at; NULL when the gate
-- report was not read). Spec §8.5: expired evidence is never approved.
ALTER TABLE gate_verdicts
  ADD COLUMN exceptions jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN evidence_expires_at timestamptz;

-- Does this gate verdict, with unexpired evidence, hold this exact measured FAIL row (control, src, dst FAIL,
-- destination evidence digest)?
CREATE FUNCTION bravo_exception_row_matches(p_verdict uuid, p_control text, p_src text, p_digest text)
  RETURNS boolean
  LANGUAGE sql STABLE
  SET search_path = pg_catalog, public
  AS $$
    SELECT EXISTS (
      SELECT 1 FROM gate_verdicts g, jsonb_array_elements(g.controls) c
       WHERE g.verdict_id = p_verdict
         AND g.evidence_expires_at > now()
         AND c->>'control_id' = p_control
         AND c->>'src' = p_src AND c->>'src' IN ('PASS', 'FAIL')
         AND c->>'dst' = 'FAIL'
         AND c->>'evidence_digest' = p_digest)
  $$;
REVOKE ALL ON FUNCTION bravo_exception_row_matches(uuid, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bravo_exception_row_matches(uuid, text, text, text) TO bravo_api, bravo_mcp;

CREATE FUNCTION bravo_control_exceptions_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public
  AS $$
DECLARE
  p plans%ROWTYPE;
  who uuid := bravo_current_principal();
BEGIN
  SELECT * INTO p FROM plans WHERE plan_id = NEW.plan_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'plan_not_found' USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'requested' OR NEW.decided_by IS NOT NULL OR NEW.stale_reason IS NOT NULL THEN
      RAISE EXCEPTION 'exception_must_start_requested' USING ERRCODE = 'check_violation';
    END IF;
    IF who IS NULL OR NEW.requested_by IS DISTINCT FROM who THEN
      RAISE EXCEPTION 'exception_requester_not_session' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.target_environment_id IS DISTINCT FROM p.target_environment_id THEN
      RAISE EXCEPTION 'exception_target_mismatch' USING ERRCODE = 'check_violation';
    END IF;
    IF p.last_gate_verdict_id IS DISTINCT FROM NEW.gate_verdict_id
       OR NOT bravo_exception_row_matches(NEW.gate_verdict_id, NEW.control_id, NEW.src, NEW.evidence_digest) THEN
      RAISE EXCEPTION 'exception_stale' USING ERRCODE = 'check_violation';
    END IF;
    NEW.requested_at := now();
    NEW.updated_at := now();
    RETURN NEW;
  END IF;

  -- UPDATE: bound values never change; only allowed transitions.
  IF (NEW.exception_id, NEW.tenant_id, NEW.plan_id, NEW.control_id, NEW.target_environment_id, NEW.gate_verdict_id,
      NEW.src, NEW.dst, NEW.evidence_digest, NEW.reason, NEW.compensating_control, NEW.expires_at, NEW.requested_by,
      NEW.requested_at)
     IS DISTINCT FROM
     (OLD.exception_id, OLD.tenant_id, OLD.plan_id, OLD.control_id, OLD.target_environment_id, OLD.gate_verdict_id,
      OLD.src, OLD.dst, OLD.evidence_digest, OLD.reason, OLD.compensating_control, OLD.expires_at, OLD.requested_by,
      OLD.requested_at) THEN
    RAISE EXCEPTION 'exception_binding_immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    IF NOT (OLD.status, NEW.status) IN (
         ('requested', 'approved'), ('requested', 'rejected'), ('requested', 'stale'), ('requested', 'expired'),
         ('approved', 'stale'), ('approved', 'expired')) THEN
      RAISE EXCEPTION 'exception_transition_not_allowed' USING ERRCODE = 'check_violation',
        DETAIL = format('%s -> %s', OLD.status, NEW.status);
    END IF;
  END IF;
  IF NEW.status IN ('approved', 'rejected') AND OLD.status = 'requested' THEN
    -- Separation of duties: the decider is the session principal, an approver of this tenant, and neither the
    -- requester nor the plan's author.
    IF who IS NULL OR NEW.decided_by IS DISTINCT FROM who THEN
      RAISE EXCEPTION 'exception_decider_not_session' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.decided_by = NEW.requested_by OR NEW.decided_by = p.created_by THEN
      RAISE EXCEPTION 'separation_of_duties' USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM memberships m WHERE m.principal_id = NEW.decided_by
                    AND m.tenant_id = NEW.tenant_id AND m.role = 'approver') THEN
      RAISE EXCEPTION 'exception_decider_not_approver' USING ERRCODE = 'check_violation';
    END IF;
    NEW.decided_at := now();
    IF NEW.status = 'approved' THEN
      IF NEW.expires_at <= now() THEN
        RAISE EXCEPTION 'exception_expired' USING ERRCODE = 'check_violation';
      END IF;
      IF NOT bravo_exception_row_matches(p.last_gate_verdict_id, NEW.control_id, NEW.src, NEW.evidence_digest) THEN
        RAISE EXCEPTION 'exception_stale' USING ERRCODE = 'check_violation';
      END IF;
    END IF;
  ELSIF (NEW.decided_by, NEW.decided_at, NEW.decision_note) IS DISTINCT FROM
        (OLD.decided_by, OLD.decided_at, OLD.decision_note) THEN
    RAISE EXCEPTION 'exception_decision_immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.status = 'expired' AND NEW.expires_at > now() THEN
    RAISE EXCEPTION 'exception_not_expired' USING ERRCODE = 'check_violation';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
CREATE TRIGGER control_exceptions_guard BEFORE INSERT OR UPDATE ON control_exceptions
  FOR EACH ROW EXECUTE FUNCTION bravo_control_exceptions_guard();
-- No role gets DELETE: decisions and stale / expired marks stay as the audit record.

-- Events ------------------------------------------------------------------------------------------
ALTER TABLE events DROP CONSTRAINT events_kind_check;
ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN (
  'state.changed', 'gate.verdict', 'approval.requested', 'approval.granted', 'run.stage', 'run.done',
  'receipt.saved', 'analysis.done', 'trial.started', 'trial.proposed', 'tool.denied', 'trial.verdict',
  'change.requested', 'change.stage', 'change.prepared', 'change.prepare_failed', 'change.applied',
  'change.refused', 'change.failed',
  'exception.requested', 'exception.approved', 'exception.rejected', 'exception.stale', 'exception.expired'));

-- Grants --------------------------------------------------------------------------------------------
-- Console API: request, decide, mark stale / expired. Operator MCP: read, and mark stale / expired when its
-- gate_run assembles a verdict (same handler); it cannot decide (no decided_* columns).
GRANT SELECT, INSERT ON control_exceptions TO bravo_api;
GRANT UPDATE (status, stale_reason, decided_by, decided_at, decision_note, updated_at) ON control_exceptions TO bravo_api;
GRANT SELECT ON control_exceptions TO bravo_mcp;
GRANT UPDATE (status, stale_reason, updated_at) ON control_exceptions TO bravo_mcp;
