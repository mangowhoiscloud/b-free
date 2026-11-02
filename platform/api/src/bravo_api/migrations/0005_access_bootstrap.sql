-- Audit A3: real Cloudflare Access identities only. No seeded or invented users.
-- First-login bootstrap: the API calls this once, for the one email named by BRAVO_BOOTSTRAP_EMAIL, and only
-- while the tenant named by BRAVO_BOOTSTRAP_TENANT has no members at all. The principal is the verified
-- token's (iss, sub, email). Every later member is granted by an admin (python -m bravo_api.admin grant).
-- Applied once, after 0004.

CREATE FUNCTION bravo_bootstrap_principal(p_iss text, p_sub text, p_email text, p_tenant text, p_roles text[])
  RETURNS boolean
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, public
  AS $$
DECLARE
  pid uuid := gen_random_uuid();
BEGIN
  IF coalesce(p_iss, '') = '' OR coalesce(p_sub, '') = '' OR coalesce(p_email, '') !~ '@'
     OR coalesce(p_tenant, '') = '' OR coalesce(cardinality(p_roles), 0) = 0 THEN
    RAISE EXCEPTION 'bootstrap_invalid' USING ERRCODE = 'check_violation';
  END IF;
  IF NOT p_roles <@ ARRAY['viewer', 'operator', 'approver', 'security_approver']::text[] THEN
    RAISE EXCEPTION 'bootstrap_role_unknown' USING ERRCODE = 'check_violation';
  END IF;
  -- Separation of duties (D-18): the first member never both requests and approves.
  IF 'operator' = ANY (p_roles) AND p_roles && ARRAY['approver', 'security_approver']::text[] THEN
    RAISE EXCEPTION 'bootstrap_separation_of_duties' USING ERRCODE = 'check_violation';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtext('bravo-bootstrap:' || p_tenant));
  PERFORM set_config('app.tenant_id', p_tenant, true);
  IF EXISTS (SELECT 1 FROM memberships WHERE tenant_id = p_tenant) THEN
    RETURN false;  -- the tenant already has members: only an admin grants from now on
  END IF;
  INSERT INTO tenants (tenant_id, name) VALUES (p_tenant, p_tenant) ON CONFLICT (tenant_id) DO NOTHING;
  PERFORM set_config('app.principal_id', pid::text, true);
  INSERT INTO principals (id, iss, sub, email) VALUES (pid, p_iss, p_sub, p_email)
    ON CONFLICT (iss, sub) DO NOTHING;
  SELECT id INTO pid FROM principals WHERE iss = p_iss AND sub = p_sub;
  INSERT INTO memberships (principal_id, tenant_id, role)
    SELECT pid, p_tenant, r FROM unnest(p_roles) AS r ON CONFLICT DO NOTHING;
  RETURN true;
END $$;
REVOKE ALL ON FUNCTION bravo_bootstrap_principal(text, text, text, text, text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bravo_bootstrap_principal(text, text, text, text, text[]) TO bravo_api, bravo_mcp;
