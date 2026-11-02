-- SECURITY DEFINER functions run as the database owner. On the platform cluster (CNPG) the owner is not a
-- superuser and has no BYPASSRLS, so FORCE ROW LEVEL SECURITY hid every row from them: principal lookup
-- (bravo_resolve_principal) found nobody -> 403 for real Access users, JIT membership could not see existing
-- memberships, and the run / rehearsal / change watchers (bravo_inflight_*) saw no work. Found 2026-11-02 on
-- bravo-platform-rke2. Fix: one permissive policy per RLS table for the owner role only. The owner can already
-- ALTER or disable RLS on its own tables, so this grants nothing new; the service roles (bravo_api, bravo_mcp,
-- bravo_executor, bravo_apply) are unchanged. Applied once, after 0006.

DO $$
DECLARE
  t record;
BEGIN
  FOR t IN
    SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity
       AND pg_get_userbyid(c.relowner) = current_user
  LOOP
    EXECUTE format('CREATE POLICY owner_definer ON public.%I TO %I USING (true) WITH CHECK (true)', t.relname, current_user);
  END LOOP;
END $$;
