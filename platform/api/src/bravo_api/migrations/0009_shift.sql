-- 0009: SHIFT (contract 15 §10). The operator's console start moves AWAIT_APPROVAL -> SHIFT; the Executor walks
-- Edge weights 10/50/100 %; DONE -> CUTOVER with the same approval. Two things need storage:
--   * the write-pause budget the operator gave at the start, used when the system starts the cutover after SHIFT;
--   * restart resume for plans in SHIFT (bravo_inflight_runs also returns the state, so the watcher derives
--     the shift run's Idempotency-Key shift-<approval_id> from the consumed approval's cutover-<approval_id>).

ALTER TABLE plans ADD COLUMN write_pause_budget_ms int
  CHECK (write_pause_budget_ms IS NULL OR write_pause_budget_ms BETWEEN 100 AND 60000);

DROP FUNCTION bravo_inflight_runs();
CREATE FUNCTION bravo_inflight_runs()
  RETURNS TABLE (tenant_id text, plan_id uuid, run_id uuid, run_idem_key text, state text)
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, public
  AS $$
    SELECT p.tenant_id, p.plan_id, p.active_run_id, a.run_idem_key, p.state
      FROM plans p
      LEFT JOIN LATERAL (SELECT run_idem_key FROM approvals
                          WHERE approvals.plan_id = p.plan_id AND consumed_at IS NOT NULL
                          ORDER BY consumed_at DESC LIMIT 1) a ON true
     WHERE p.state IN ('SHIFT', 'CUTOVER', 'FAILBACK')
  $$;
REVOKE ALL ON FUNCTION bravo_inflight_runs() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bravo_inflight_runs() TO bravo_api;
