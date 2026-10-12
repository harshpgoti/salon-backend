-- TASK-050 (part 2) / FR-070: registry read functions.
--
-- Company overview is cross-tenant by definition, but every tenant table
-- carries FORCE RLS for salon sessions. Rather than punching per-table
-- company holes into tenant policies (which risks the isolation crown
-- jewels), these two SECURITY DEFINER functions are the single contained
-- bypass: owned by the migration superuser, fixed search_path, aggregates
-- only. They select no client name, contact, note or message column — a
-- reviewer verifies absence by reading the two queries below. EXECUTE is
-- granted to salon_app; the company role check stays in the API layer.

CREATE OR REPLACE FUNCTION company_registry_overview()
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COALESCE(jsonb_agg(row ORDER BY row.created_at), '[]'::jsonb)
    FROM (
        SELECT t.id AS tenant_id, t.display_name AS business_name,
               t.created_at,
               p.code AS plan_code, p.name AS plan_name, s.status AS subscription_status,
               (SELECT count(*) FROM locations l WHERE l.tenant_id = t.id) AS locations,
               (SELECT COALESCE(jsonb_object_agg(state, count), '{}'::jsonb)
                 FROM (SELECT state, count(*) FROM tenant_setup_steps st
                       WHERE st.tenant_id = t.id GROUP BY state) steps) AS setup_steps,
               (SELECT count(*) FROM sms_senders ss WHERE ss.tenant_id = t.id) AS sms_senders_total,
               (SELECT count(*) FROM sms_senders ss
                 WHERE ss.tenant_id = t.id AND ss.status = 'verified') AS sms_senders_verified,
               (SELECT count(*) FROM whatsapp_senders ws WHERE ws.tenant_id = t.id) AS whatsapp_senders_total,
               (SELECT count(*) FROM whatsapp_senders ws
                 WHERE ws.tenant_id = t.id AND ws.status = 'approved') AS whatsapp_senders_approved,
               (SELECT count(*) FROM communications c
                 WHERE c.tenant_id = t.id AND c.state = 'failed') AS failed_communications,
               (SELECT count(*) FROM dead_letters d
                 WHERE d.tenant_id = t.id AND d.resolved_at IS NULL) AS open_dead_letters,
               (SELECT count(*) FROM follow_up_tasks f
                 WHERE f.tenant_id = t.id AND f.status = 'open') AS open_follow_up_tasks,
               (SELECT count(DISTINCT client_id) FROM consents co
                 WHERE co.tenant_id = t.id AND co.status = 'granted'
                   AND co.purpose = 'marketing') AS marketing_reachable_clients
        FROM tenants t
        LEFT JOIN tenant_subscriptions s ON s.tenant_id = t.id
        LEFT JOIN billing_plans p ON p.id = s.plan_id
    ) row
$$;

CREATE OR REPLACE FUNCTION company_registry_tenant(p_tenant uuid)
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT to_jsonb(row) FROM (
        SELECT t.id AS tenant_id, t.display_name AS business_name,
               t.created_at,
               p.code AS plan_code, p.name AS plan_name, s.status AS subscription_status,
               (SELECT count(*) FROM locations l WHERE l.tenant_id = t.id) AS locations,
               (SELECT COALESCE(jsonb_object_agg(state, count), '{}'::jsonb)
                 FROM (SELECT state, count(*) FROM tenant_setup_steps st
                       WHERE st.tenant_id = t.id GROUP BY state) steps) AS setup_steps,
               (SELECT count(*) FROM sms_senders ss WHERE ss.tenant_id = t.id) AS sms_senders_total,
               (SELECT count(*) FROM sms_senders ss
                 WHERE ss.tenant_id = t.id AND ss.status = 'verified') AS sms_senders_verified,
               (SELECT count(*) FROM whatsapp_senders ws WHERE ws.tenant_id = t.id) AS whatsapp_senders_total,
               (SELECT count(*) FROM whatsapp_senders ws
                 WHERE ws.tenant_id = t.id AND ws.status = 'approved') AS whatsapp_senders_approved,
               (SELECT count(*) FROM communications c
                 WHERE c.tenant_id = t.id AND c.state = 'failed') AS failed_communications,
               (SELECT count(*) FROM dead_letters d
                 WHERE d.tenant_id = t.id AND d.resolved_at IS NULL) AS open_dead_letters,
               (SELECT count(*) FROM follow_up_tasks f
                 WHERE f.tenant_id = t.id AND f.status = 'open') AS open_follow_up_tasks,
               (SELECT count(DISTINCT client_id) FROM consents co
                 WHERE co.tenant_id = t.id AND co.status = 'granted'
                   AND co.purpose = 'marketing') AS marketing_reachable_clients
        FROM tenants t
        LEFT JOIN tenant_subscriptions s ON s.tenant_id = t.id
        LEFT JOIN billing_plans p ON p.id = s.plan_id
        WHERE t.id = p_tenant
    ) row
$$;

REVOKE ALL ON FUNCTION company_registry_overview() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company_registry_overview() TO salon_app;
REVOKE ALL ON FUNCTION company_registry_tenant(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company_registry_tenant(uuid) TO salon_app;
