-- Executor resource locks (D-19, platform/executor/README.md "자원 잠금").
-- A run reserves its whole resource set in one transaction before its first PREPARED
-- effect. A lock is released only when the run has ended and every effect of the run is
-- CONFIRMED_*. Lease expiry alone never releases it (E3).
-- Applied by the compose one-shot `executor-migrate` as the platform DB owner, after the
-- API migrations (runs must exist). Idempotent. Run with psql -v ON_ERROR_STOP=1.

CREATE TABLE IF NOT EXISTS resource_locks (
  resource text PRIMARY KEY,                 -- normalised resource ID, e.g. pgbouncer/0
  run_id uuid NOT NULL REFERENCES runs,
  owner text NOT NULL,                       -- Executor instance that took the lock
  epoch int NOT NULL,
  lease_expires_at timestamptz NOT NULL,     -- informational: expiry does not release
  acquired_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS resource_locks_run_idx ON resource_locks (run_id);

ALTER TABLE resource_locks ENABLE ROW LEVEL SECURITY;
ALTER TABLE resource_locks FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bravo_executor')
     AND NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'resource_locks' AND policyname = 'executor_cell') THEN
    EXECUTE 'CREATE POLICY executor_cell ON resource_locks TO bravo_executor USING (true) WITH CHECK (true)';
    EXECUTE 'GRANT SELECT, INSERT, DELETE ON resource_locks TO bravo_executor';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bravo_api')
     AND NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'resource_locks' AND policyname = 'via_run') THEN
    EXECUTE 'CREATE POLICY via_run ON resource_locks TO bravo_api USING (EXISTS (SELECT 1 FROM runs r WHERE r.run_id = resource_locks.run_id))';
    EXECUTE 'GRANT SELECT ON resource_locks TO bravo_api';
  END IF;
END $$;
