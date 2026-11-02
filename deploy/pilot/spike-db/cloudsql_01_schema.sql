-- Cloud SQL for PostgreSQL variant of apps/demo/schema/sql/01_schema.sql (spike, 12-db-migration-matrix 4.5).
-- Cloud SQL: CREATE ROLE gives the creator no membership (not even ADMIN), so ALTER FUNCTION ... OWNER
-- and CREATE PUBLICATION on chat_owner tables fail. Two grants after CREATE ROLE chat_owner fix it.
-- Chat schema, identical on src and dst (CONTRACT.md 3).
-- Tables are owned by the NOLOGIN role chat_owner, not by the superuser, so that a
-- non-superuser subscription owner (executor_ctl) can run apply as the table owner
-- (PostgreSQL 16 run_as_owner = false requires SET ROLE to the table owner).
\set ON_ERROR_STOP on

CREATE ROLE chat_owner NOLOGIN;
GRANT chat_owner TO CURRENT_USER WITH SET TRUE, INHERIT TRUE;   -- Cloud SQL: creator gets no membership
GRANT CREATE ON SCHEMA public TO chat_owner;                     -- PG15+: a new owner needs CREATE on the schema

CREATE TABLE messages (
  id bigserial PRIMARY KEY,
  room text NOT NULL,
  author text NOT NULL,
  body text NOT NULL,
  idem_key uuid NOT NULL UNIQUE,      -- client-generated idempotency key
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bravo_barrier (
  id bigserial PRIMARY KEY,
  plan_id text NOT NULL,
  epoch int NOT NULL,
  at timestamptz NOT NULL DEFAULT now()
);

-- Resource-side fence (D-19). One row per database, never published.
CREATE TABLE bravo_fence (
  id int PRIMARY KEY CHECK (id = 1),
  epoch int NOT NULL
);
INSERT INTO bravo_fence VALUES (1, 0);

-- Attachments (D-04, proposal). The object key is the content hash (att/<sha256>) and is never
-- overwritten. One row per version: a replacement is a new row with the same logical_id, a
-- deletion sets deleted_at. UUID primary keys: the Executor bumps only the bigserial sequences
-- at cutover, so these tables must not depend on a sequence.
CREATE TABLE attachments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  logical_id uuid NOT NULL,             -- stable across replacements
  idem_key uuid NOT NULL UNIQUE,        -- client-generated idempotency key
  room text NOT NULL,
  author text NOT NULL,
  object_key text NOT NULL,
  sha256 text NOT NULL,
  size_bytes integer NOT NULL,
  content_type text NOT NULL,
  filename text NOT NULL,
  store_id text NOT NULL,               -- object store that holds object_key
  created_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  CONSTRAINT attachments_sha256_hex CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT attachments_key_is_hash CHECK (object_key = 'att/' || sha256),
  CONSTRAINT attachments_size_cap CHECK (size_bytes > 0 AND size_bytes <= 5242880)
);
CREATE INDEX attachments_logical_id_idx ON attachments (logical_id);

-- A chat message that carries an attachment. Separate table so that messages keeps the
-- column set the Executor digests (CONTRACT.md 3).
CREATE TABLE message_attachments (
  message_id bigint PRIMARY KEY REFERENCES messages (id),
  attachment_id uuid NOT NULL REFERENCES attachments (id)
);
CREATE INDEX message_attachments_attachment_idx ON message_attachments (attachment_id);

-- Attachment write control, one row per database, never published (like bravo_fence).
-- The platform freezes it before cutover and switches active_store; the app role has no
-- privilege on it. Initial row: 03_<side>.sql.
CREATE TABLE attachment_store (
  id int PRIMARY KEY CHECK (id = 1),
  active_store text NOT NULL,
  generation bigint NOT NULL DEFAULT 1,
  frozen boolean NOT NULL DEFAULT false,
  freeze_watermark timestamptz,
  CONSTRAINT attachment_store_watermark CHECK (NOT frozen OR freeze_watermark IS NOT NULL)
);

-- INSERT guard: reject when frozen or when the row's store is not the active store.
-- FOR SHARE: a concurrent freeze (UPDATE) waits for in-flight inserts, and inserts that start
-- after it see frozen = true, so freeze_watermark is exact. Ordinary (ORIGIN) triggers do not
-- fire in logical replication apply, so replicated rows are never rejected.
CREATE FUNCTION attachments_guard_insert() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
  s public.attachment_store%ROWTYPE;
BEGIN
  SELECT * INTO s FROM public.attachment_store WHERE id = 1 FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'attachment_store is not initialised' USING ERRCODE = 'ZB004';
  END IF;
  IF s.frozen THEN
    RAISE EXCEPTION 'attachment writes are frozen (generation %)', s.generation
      USING ERRCODE = 'ZB001';
  END IF;
  IF NEW.store_id IS DISTINCT FROM s.active_store THEN
    RAISE EXCEPTION 'attachment store % is not the active store %', NEW.store_id, s.active_store
      USING ERRCODE = 'ZB002';
  END IF;
  IF NEW.deleted_at IS NOT NULL THEN
    RAISE EXCEPTION 'a new attachment row cannot be deleted' USING ERRCODE = 'ZB003';
  END IF;
  RETURN NEW;
END
$$;

-- UPDATE guard: every column is immutable except deleted_at, which may only go NULL -> time.
CREATE FUNCTION attachments_guard_update() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
  IF (NEW.id, NEW.logical_id, NEW.idem_key, NEW.room, NEW.author, NEW.object_key, NEW.sha256,
      NEW.size_bytes, NEW.content_type, NEW.filename, NEW.store_id, NEW.created_at)
     IS DISTINCT FROM
     (OLD.id, OLD.logical_id, OLD.idem_key, OLD.room, OLD.author, OLD.object_key, OLD.sha256,
      OLD.size_bytes, OLD.content_type, OLD.filename, OLD.store_id, OLD.created_at) THEN
    RAISE EXCEPTION 'attachment rows are immutable' USING ERRCODE = 'ZB003';
  END IF;
  IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN
    RAISE EXCEPTION 'deleted_at can only change from NULL to a time' USING ERRCODE = 'ZB003';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER attachments_guard_insert BEFORE INSERT ON attachments
  FOR EACH ROW EXECUTE FUNCTION attachments_guard_insert();
CREATE TRIGGER attachments_guard_update BEFORE UPDATE ON attachments
  FOR EACH ROW EXECUTE FUNCTION attachments_guard_update();

-- Write-block switch (spec r2 §8.10, cutover step 2): one row per app environment, never
-- published. Business writes take FOR SHARE on this table in their own transaction; the switch
-- takes LOCK TABLE ... IN EXCLUSIVE MODE before it changes a row, so once the switch commits no
-- write that saw the old state can still commit. Each database keeps its own rows: the state
-- belongs to the database the writes reach.
CREATE TABLE write_block (
  env_id text PRIMARY KEY CHECK (env_id ~ '^[a-z0-9][a-z0-9-]{0,31}$'),
  blocked boolean NOT NULL,
  since timestamptz NOT NULL DEFAULT now(),
  changed_by text NOT NULL
);

ALTER TABLE messages OWNER TO chat_owner;
ALTER TABLE bravo_barrier OWNER TO chat_owner;
ALTER TABLE bravo_fence OWNER TO chat_owner;
ALTER TABLE attachments OWNER TO chat_owner;
ALTER TABLE message_attachments OWNER TO chat_owner;
ALTER TABLE attachment_store OWNER TO chat_owner;
ALTER TABLE write_block OWNER TO chat_owner;
ALTER FUNCTION attachments_guard_insert() OWNER TO chat_owner;
ALTER FUNCTION attachments_guard_update() OWNER TO chat_owner;
REVOKE ALL ON FUNCTION attachments_guard_insert() FROM PUBLIC;
REVOKE ALL ON FUNCTION attachments_guard_update() FROM PUBLIC;
