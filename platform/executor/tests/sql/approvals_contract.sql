-- Test fixture only: the columns of platform/api migrations 0001 `approvals` that the Executor
-- reads (approvals.py). The real table, its foreign keys and triggers belong to platform/api.
CREATE TABLE IF NOT EXISTS approvals (
  approval_id  uuid PRIMARY KEY,
  plan_id      uuid NOT NULL,
  stage        text NOT NULL,
  approver_id  uuid NOT NULL,
  requester_id uuid NOT NULL,
  expires_at   timestamptz NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  consumed_at  timestamptz,
  consumed_by  uuid
);
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS consumed_by uuid;
