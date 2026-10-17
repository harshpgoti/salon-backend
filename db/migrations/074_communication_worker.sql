-- TASK-027: let the polling worker discover only tenant IDs with due outbox rows.
-- The worker still reads/claims message content through the normal forced-RLS scope.
CREATE FUNCTION due_communication_tenants(p_now timestamptz, p_limit integer)
RETURNS TABLE(tenant_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT c.tenant_id
    FROM public.communications AS c
    WHERE c.state = 'recorded'
      AND (c.scheduled_at IS NULL OR c.scheduled_at <= p_now)
    GROUP BY c.tenant_id
    ORDER BY min(c.scheduled_at) NULLS FIRST
    LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 50), 500))
$$;

REVOKE ALL ON FUNCTION due_communication_tenants(timestamptz, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION due_communication_tenants(timestamptz, integer) TO salon_app;

COMMENT ON FUNCTION due_communication_tenants(timestamptz, integer) IS
    'Returns bounded tenant IDs only; dispatch content reads remain protected by forced RLS.';
