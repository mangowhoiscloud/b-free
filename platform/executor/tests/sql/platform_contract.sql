-- Test fixture only: the four platform tables the Executor writes, verbatim from
-- deploy/pilot/CONTRACT.md 5. The real migrations live in platform/api/migrations.
CREATE TABLE IF NOT EXISTS runs (
  run_id uuid PRIMARY KEY, plan_id uuid NOT NULL, kind text NOT NULL,
  epoch int NOT NULL, idem_key text NOT NULL UNIQUE, status text NOT NULL,
  write_pause_ms int, started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz);
CREATE TABLE IF NOT EXISTS run_stages (
  run_id uuid NOT NULL REFERENCES runs, seq int NOT NULL, name text NOT NULL,
  started_at timestamptz NOT NULL, ms int, result text NOT NULL, detail jsonb, PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS effects (
  effect_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs, resource text NOT NULL, action text NOT NULL,
  epoch int NOT NULL, state text NOT NULL,
  observed_at timestamptz, evidence jsonb, UNIQUE (run_id, resource, action));
CREATE TABLE IF NOT EXISTS receipts (
  receipt_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs, body jsonb NOT NULL,
  verdict text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
