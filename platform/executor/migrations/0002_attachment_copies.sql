-- Attachment copy ledger (D-04): one row per (source store, target store, object key).
-- VERIFIED means the target holds the object and its SHA-256 was checked there.
-- The copy job skips VERIFIED keys, so a restarted job resumes with the set difference.
-- Applied by the compose one-shot `executor-migrate`. Idempotent. Run with psql -v ON_ERROR_STOP=1.

CREATE TABLE IF NOT EXISTS attachment_copies (
  source_store text NOT NULL,
  target_store text NOT NULL,
  object_key text NOT NULL CHECK (object_key ~ '^att/[0-9a-f]{64}$'),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes integer NOT NULL,
  status text NOT NULL CHECK (status IN ('COPYING', 'VERIFIED', 'FAILED')),
  copied boolean NOT NULL DEFAULT false,      -- true when this job wrote the object (not found in place)
  attempts integer NOT NULL DEFAULT 1,
  error text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_store, target_store, object_key)
);

ALTER TABLE attachment_copies ENABLE ROW LEVEL SECURITY;
ALTER TABLE attachment_copies FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bravo_executor')
     AND NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'attachment_copies' AND policyname = 'executor_cell') THEN
    EXECUTE 'CREATE POLICY executor_cell ON attachment_copies TO bravo_executor USING (true) WITH CHECK (true)';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON attachment_copies TO bravo_executor';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bravo_api')
     AND NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'attachment_copies' AND policyname = 'api_read') THEN
    EXECUTE 'CREATE POLICY api_read ON attachment_copies FOR SELECT TO bravo_api USING (true)';
    EXECUTE 'GRANT SELECT ON attachment_copies TO bravo_api';
  END IF;
END $$;
