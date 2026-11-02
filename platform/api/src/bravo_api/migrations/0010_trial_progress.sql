-- Rehearsal progress events (S2, B04/P2): trial.progress carries each Trial phase change (provisioning,
-- agent_running, proposed, verifying, done) with the Trial's own timings, so a Trial started at 0:00 of the demo shows
-- where it is. Additive: the kind is appended to whatever events_kind_check allows now, so a concurrent migration that
-- added other kinds is not undone by this one.
DO $$
DECLARE def text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO def FROM pg_constraint
   WHERE conname = 'events_kind_check' AND conrelid = 'events'::regclass;
  IF def IS NULL THEN
    RAISE EXCEPTION 'events_kind_check not found';
  END IF;
  IF position('trial.progress' IN def) = 0 THEN
    IF position('''trial.verdict''::text' IN def) = 0 THEN
      RAISE EXCEPTION 'events_kind_check has no trial.verdict to extend: %', def;
    END IF;
    ALTER TABLE events DROP CONSTRAINT events_kind_check;
    EXECUTE 'ALTER TABLE events ADD CONSTRAINT events_kind_check '
      || replace(def, '''trial.verdict''::text', '''trial.verdict''::text, ''trial.progress''::text');
  END IF;
END $$;
