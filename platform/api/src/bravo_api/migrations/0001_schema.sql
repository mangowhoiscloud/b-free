-- Platform DB schema (CONTRACT.md 5, docs/architecture/08-operator-mcp.md 5 and 9).
-- Applied by `python -m bravo_api.migrate` as the database owner. Every business table
-- carries tenant_id with RLS ENABLE + FORCE; no service role has BYPASSRLS.

-- Service roles. Passwords are set by the migrate entrypoint from env, never in SQL.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bravo_api') THEN
    CREATE ROLE bravo_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bravo_mcp') THEN
    CREATE ROLE bravo_mcp LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bravo_executor') THEN
    CREATE ROLE bravo_executor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO bravo_api, bravo_mcp, bravo_executor;

-- Identity -------------------------------------------------------------------------------

CREATE TABLE tenants (
  tenant_id  text PRIMARY KEY CHECK (tenant_id ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  name       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Principal key is (iss, sub). Email is display only (08 5).
CREATE TABLE principals (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  iss        text NOT NULL,
  sub        text NOT NULL,
  email      text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (iss, sub)
);

CREATE TABLE memberships (
  principal_id uuid NOT NULL REFERENCES principals (id),
  tenant_id    text NOT NULL REFERENCES tenants (tenant_id),
  role         text NOT NULL CHECK (role IN ('viewer', 'operator', 'approver', 'security_approver')),
  PRIMARY KEY (principal_id, tenant_id, role)
);

-- Business tables ------------------------------------------------------------------------

CREATE TABLE environments (
  environment_id uuid PRIMARY KEY,
  tenant_id      text NOT NULL REFERENCES tenants (tenant_id),
  name           text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  role           text NOT NULL CHECK (role IN ('source', 'target')),
  engine         text NOT NULL CHECK (engine IN ('postgres')),
  -- Secret reference name only, scoped to the owning tenant (08 4, 5).
  secret_ref     text NOT NULL CHECK (secret_ref LIKE ('tenants/' || tenant_id || '/%') AND secret_ref !~ '\.\.'),
  created_by     uuid NOT NULL REFERENCES principals (id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, environment_id),
  UNIQUE (tenant_id, name)
);

CREATE TABLE plans (
  plan_id               uuid PRIMARY KEY,
  tenant_id             text NOT NULL REFERENCES tenants (tenant_id),
  workload              text NOT NULL CHECK (workload ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  source_environment_id uuid NOT NULL,
  target_environment_id uuid NOT NULL,
  state                 text NOT NULL CHECK (state IN ('COLLECT', 'ANALYZE', 'PROPOSE', 'REHEARSE', 'GATE',
                          'AWAIT_APPROVAL', 'CUTOVER', 'WATCH', 'ABORT_PRE_WRITE', 'SAFE_HOLD')),
  state_reason          text,
  -- Pilot scope: ANALYZE/PROPOSE/REHEARSE are not entered and not faked (team decision 11/2).
  rehearsal             text NOT NULL DEFAULT 'not_in_scope' CHECK (rehearsal IN ('not_in_scope')),
  collect_snapshot      jsonb,        -- read-only snapshot of Executor controls + readiness
  collected_at          timestamptz,
  version               int NOT NULL DEFAULT 1,   -- CAS counter, bumped on every update
  revision              int NOT NULL DEFAULT 1,   -- content revision bound by approvals
  epoch                 int NOT NULL DEFAULT 0,   -- fencing epoch handed to the Executor
  event_seq             bigint NOT NULL DEFAULT 0, -- per-plan event sequence (SSE id)
  last_gate_verdict_id  uuid,
  approval_request_id   uuid UNIQUE,
  approval_request_expires_at timestamptz,  -- shown before approval; the approval binds it
  approval_requested_by uuid REFERENCES principals (id),
  active_run_id         uuid,
  last_run_id           uuid,
  created_by            uuid NOT NULL REFERENCES principals (id),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, plan_id),
  CHECK (source_environment_id <> target_environment_id),
  -- Composite keys: a plan can only reference its own tenant's environments (08 5).
  FOREIGN KEY (tenant_id, source_environment_id) REFERENCES environments (tenant_id, environment_id),
  FOREIGN KEY (tenant_id, target_environment_id) REFERENCES environments (tenant_id, environment_id)
);

CREATE TABLE gate_verdicts (
  verdict_id    uuid PRIMARY KEY,
  tenant_id     text NOT NULL,
  plan_id       uuid NOT NULL,
  plan_revision int NOT NULL,
  verdict       text NOT NULL CHECK (verdict IN ('PASS', 'BLOCK')),
  reasons       text[] NOT NULL,
  controls      jsonb NOT NULL,
  readiness     jsonb NOT NULL,
  input_digest  text NOT NULL,
  rule_version  text NOT NULL,
  requested_by  uuid NOT NULL REFERENCES principals (id),
  channel       text NOT NULL CHECK (channel IN ('console', 'mcp')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, verdict_id),
  FOREIGN KEY (tenant_id, plan_id) REFERENCES plans (tenant_id, plan_id)
);

CREATE TABLE events (
  plan_id         uuid NOT NULL,
  seq             bigint NOT NULL,
  tenant_id       text NOT NULL,
  state           text NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('state.changed', 'gate.verdict', 'approval.granted',
                    'run.stage', 'run.done', 'receipt.saved')),
  at              timestamptz NOT NULL DEFAULT clock_timestamp(),
  mode            text NOT NULL CHECK (mode IN ('live', 'recorded')),
  run_id          uuid,
  actor_kind      text NOT NULL CHECK (actor_kind IN ('console', 'mcp', 'system')),
  actor_principal uuid REFERENCES principals (id),
  data            jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (plan_id, seq),
  FOREIGN KEY (tenant_id, plan_id) REFERENCES plans (tenant_id, plan_id)
);

CREATE TABLE approvals (
  approval_id         uuid PRIMARY KEY,
  tenant_id           text NOT NULL,
  plan_id             uuid NOT NULL,
  approval_request_id uuid NOT NULL UNIQUE,         -- one decision per request
  stage               text NOT NULL CHECK (stage IN ('cutover')),
  plan_revision       int NOT NULL,
  gate_verdict_id     uuid NOT NULL REFERENCES gate_verdicts (verdict_id),
  approver_id         uuid NOT NULL REFERENCES principals (id),
  proposer_id         uuid NOT NULL REFERENCES principals (id),  -- filled by trigger from plans
  requester_id        uuid NOT NULL REFERENCES principals (id),  -- filled by trigger from plans
  expires_at          timestamptz NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  consumed_at         timestamptz,
  consumed_by         uuid REFERENCES principals (id),
  run_idem_key        text UNIQUE,
  CONSTRAINT approvals_separation_of_duties CHECK (approver_id <> proposer_id AND approver_id <> requester_id),
  CONSTRAINT approvals_consumed_pair CHECK ((consumed_at IS NULL) = (consumed_by IS NULL)),
  CONSTRAINT approvals_expiry_after_creation CHECK (expires_at > created_at),
  FOREIGN KEY (tenant_id, plan_id) REFERENCES plans (tenant_id, plan_id)
);

-- Idempotency scope is (tenant, action, key). Same key with a different input digest -> 409.
CREATE TABLE idempotency (
  tenant_id      text NOT NULL REFERENCES tenants (tenant_id),
  action         text NOT NULL,
  idem_key       text NOT NULL CHECK (length(idem_key) BETWEEN 8 AND 200),
  request_digest text NOT NULL,
  principal_id   uuid NOT NULL REFERENCES principals (id),
  status_code    int,
  response       jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, action, idem_key)
);

-- Executor tables: exactly CONTRACT.md 5. The Executor writes these. -------------------

CREATE TABLE runs (
  run_id uuid PRIMARY KEY, plan_id uuid NOT NULL, kind text NOT NULL,      -- cutover | failback
  epoch int NOT NULL, idem_key text NOT NULL UNIQUE, status text NOT NULL, -- RUNNING | DONE | ABORT_PRE_WRITE | SAFE_HOLD
  write_pause_ms int, started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz);
CREATE TABLE run_stages (
  run_id uuid NOT NULL REFERENCES runs, seq int NOT NULL, name text NOT NULL,
  started_at timestamptz NOT NULL, ms int, result text NOT NULL, detail jsonb, PRIMARY KEY (run_id, seq));
CREATE TABLE effects (
  effect_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs, resource text NOT NULL, action text NOT NULL,
  epoch int NOT NULL, state text NOT NULL,   -- PREPARED | IN_FLIGHT | CONFIRMED_APPLIED | CONFIRMED_NOT_APPLIED | UNKNOWN
  observed_at timestamptz, evidence jsonb, UNIQUE (run_id, resource, action));
CREATE TABLE receipts (
  receipt_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs, body jsonb NOT NULL,
  verdict text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());

CREATE INDEX runs_plan_idx ON runs (plan_id);
CREATE INDEX receipts_run_idx ON receipts (run_id);
CREATE INDEX gate_verdicts_plan_idx ON gate_verdicts (plan_id, created_at);
CREATE INDEX approvals_plan_idx ON approvals (plan_id);
CREATE INDEX memberships_tenant_idx ON memberships (tenant_id);
