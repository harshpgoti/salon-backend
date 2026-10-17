-- Session identity is resolved by the API; callers cannot select another identity.
-- Returns membership/branding only, never client, appointment or communication data.
CREATE FUNCTION identity_workspace_list(p_issuer text, p_subject text)
RETURNS TABLE(tenant_id uuid, business_name text, role text)
LANGUAGE sql SECURITY DEFINER STABLE SET search_path = pg_catalog, public AS $$
    SELECT t.id, t.display_name, m.role
    FROM public.identity_users u
    JOIN public.memberships m ON m.user_id=u.id
    JOIN public.tenants t ON t.id=m.tenant_id
    WHERE u.issuer=p_issuer AND u.subject=p_subject
    ORDER BY t.display_name, t.id;
$$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
        WHERE p.oid='identity_workspace_list(text,text)'::regprocedure AND (r.rolsuper OR r.rolbypassrls)) THEN
        RAISE EXCEPTION 'workspace discovery owner must see FORCE RLS membership tables';
    END IF;
END;
$$;
REVOKE ALL ON FUNCTION identity_workspace_list(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity_workspace_list(text,text) TO salon_app;
