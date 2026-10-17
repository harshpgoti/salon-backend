-- TASK-045: expose numerators, denominators and freshness for supported
-- company metrics. No tenant or salon-client rows leave these aggregates.
CREATE OR REPLACE FUNCTION company_compute_metrics()
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    WITH onboarding AS (
        SELECT count(*)::integer AS tenant_count,
               count(*) FILTER (WHERE setup_steps > 0 AND open_steps = 0)::integer AS complete_count
        FROM (
            SELECT t.id,
                   (SELECT count(*) FROM tenant_setup_steps st
                    WHERE st.tenant_id=t.id) AS setup_steps,
                   (SELECT count(*) FROM tenant_setup_steps st
                    WHERE st.tenant_id=t.id AND st.state <> 'complete') AS open_steps
            FROM tenants t
        ) tenant_progress
    ), support AS (
        SELECT count(*)::integer AS total_cases,
               count(*) FILTER (WHERE status NOT IN ('resolved', 'closed'))::integer AS open_cases
        FROM support_cases
    ), usage AS (
        SELECT count(*) FILTER (WHERE state <> 'voided')::integer AS messages,
               COALESCE(sum(estimated_minor) FILTER (WHERE state <> 'voided'), 0)::bigint AS estimated_minor
        FROM usage_reservations
        WHERE created_at >= date_trunc('month', now())
          AND created_at <= now()
    ), provider_cost AS (
        SELECT count(*) FILTER (WHERE state='matched' AND cost_minor IS NOT NULL)::integer AS matched_count,
               COALESCE(sum(cost_minor) FILTER (WHERE state='matched'), 0)::bigint AS matched_minor,
               count(*) FILTER (WHERE state='pending')::integer AS pending_count
        FROM provider_usage
        WHERE reported_at >= date_trunc('month', now()) AND reported_at <= now()
    )
    SELECT jsonb_build_object(
        'mrr_minor', COALESCE((SELECT SUM(p.price_minor) FROM tenant_subscriptions s
                               JOIN billing_plans p ON p.id=s.plan_id
                               WHERE s.status='active'), 0),
        'paying_tenants', (SELECT count(*) FROM tenant_subscriptions WHERE status='active'),
        'paying_tenant_denominator', (SELECT count(*) FROM tenant_subscriptions),
        'trial_tenants', (SELECT count(*) FROM tenant_subscriptions WHERE status='trial'),
        'past_due_tenants', (SELECT count(*) FROM tenant_subscriptions WHERE status='past_due'),
        'cancelled_tenants', (SELECT count(*) FROM tenant_subscriptions WHERE status='cancelled'),
        'subscription_tenants', (SELECT count(*) FROM tenant_subscriptions),
        'locations', (SELECT count(*) FROM locations),
        'churned_this_month', (SELECT count(*) FROM tenant_subscriptions
                               WHERE status='cancelled'
                                 AND updated_at >= date_trunc('month', now())
                                 AND updated_at <= now()),
        'logo_churn_rate_pct', NULL,
        'logo_churn_denominator', NULL,
        'logo_churn_available', false,
        'upgrades_this_month', (SELECT count(*) FROM tenant_audit_events
                                WHERE action='billing.plan_changed'
                                  AND occurred_at >= date_trunc('month', now())
                                  AND occurred_at <= now()),
        'channel_cost_month_minor', COALESCE((SELECT SUM(cost_minor) FROM message_costs
                                              WHERE recorded_at >= date_trunc('month', now())
                                                AND recorded_at <= now()), 0),
        'channel_cost_messages_month', (SELECT count(*) FROM message_costs
                                        WHERE recorded_at >= date_trunc('month', now())
                                          AND recorded_at <= now()),
        'estimated_usage_month_minor', (SELECT estimated_minor FROM usage),
        'estimated_usage_messages_month', (SELECT messages FROM usage),
        'provider_matched_cost_month_minor', (SELECT matched_minor FROM provider_cost),
        'provider_matched_cost_messages_month', (SELECT matched_count FROM provider_cost),
        'provider_usage_pending_month', (SELECT pending_count FROM provider_cost),
        'support_open', (SELECT open_cases FROM support),
        'support_case_denominator', (SELECT total_cases FROM support),
        'support_open_pct', CASE WHEN (SELECT total_cases FROM support)=0 THEN NULL
                            ELSE round(100.0 * (SELECT open_cases FROM support)
                                       / (SELECT total_cases FROM support), 1) END,
        'onboarding_complete_count', (SELECT complete_count FROM onboarding),
        'onboarding_tenant_denominator', (SELECT tenant_count FROM onboarding),
        'onboarding_complete_pct', CASE WHEN (SELECT tenant_count FROM onboarding)=0 THEN NULL
                            ELSE round(100.0 * (SELECT complete_count FROM onboarding)
                                       / (SELECT tenant_count FROM onboarding), 1) END,
        'open_incidents', (SELECT count(*) FROM provider_incidents WHERE status <> 'resolved')
    )
$$;

REVOKE ALL ON FUNCTION company_compute_metrics() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company_compute_metrics() TO salon_app;

-- The authenticated full-refresh path removes obsolete keys for the active
-- month after recomputing from source facts. Keep deletion scoped to this
-- aggregate snapshot table; salon_app receives no broader table cleanup rights.
GRANT DELETE ON company_metric_snapshots TO salon_app;
