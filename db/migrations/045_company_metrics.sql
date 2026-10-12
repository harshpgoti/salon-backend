-- TASK-045 / FR-063, FR-064: company health snapshots and projections.
--
--   company_metric_snapshots - one row per metric per month: the computed
--                              value plus when and from what it was computed.
--                              Rebuilds are full-refresh upserts (idempotent):
--                              redelivered webhooks converge to the same row,
--                              corrections and deletions propagate on the
--                              next rebuild. Months that were never stored
--                              read back as unavailable — never as silent
--                              old numbers (FR-064).
--
-- Company-global like the rest of the company surface: role-gated reads,
-- aggregates only, no client data anywhere in these numbers.

CREATE TABLE company_metric_snapshots (
    metric text NOT NULL CHECK (length(metric) BETWEEN 1 AND 80),
    period text NOT NULL CHECK (period ~ '^[0-9]{4}-[0-9]{2}$'),
    value_num numeric,
    value_json jsonb NOT NULL DEFAULT '{}',
    computed_at timestamptz NOT NULL DEFAULT now(),
    source text NOT NULL DEFAULT 'live',
    PRIMARY KEY (metric, period)
);

-- Cross-tenant company metric computation (same contained pattern as the
-- registry: SECURITY DEFINER, aggregates only, EXECUTE to salon_app, role
-- checks in the API). All money in CAD minor units; MRR prices the current
-- active base only (no usage, no tax, no proration mathematics here).
CREATE OR REPLACE FUNCTION company_compute_metrics()
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT jsonb_build_object(
        'mrr_minor', COALESCE((SELECT SUM(p.price_minor) FROM tenant_subscriptions s
                               JOIN billing_plans p ON p.id = s.plan_id
                               WHERE s.status = 'active'), 0),
        'paying_tenants', (SELECT count(*) FROM tenant_subscriptions WHERE status = 'active'),
        'trial_tenants', (SELECT count(*) FROM tenant_subscriptions WHERE status = 'trial'),
        'past_due_tenants', (SELECT count(*) FROM tenant_subscriptions WHERE status = 'past_due'),
        'cancelled_tenants', (SELECT count(*) FROM tenant_subscriptions WHERE status = 'cancelled'),
        'locations', (SELECT count(*) FROM locations),
        'churned_this_month', (SELECT count(*) FROM tenant_subscriptions
                               WHERE status = 'cancelled'
                                 AND updated_at >= date_trunc('month', now())),
        'upgrades_this_month', (SELECT count(*) FROM tenant_audit_events
                                WHERE action = 'billing.plan_changed'
                                  AND occurred_at >= date_trunc('month', now())),
        'channel_cost_month_minor', (SELECT COALESCE(SUM(cost_minor), 0) FROM message_costs
                                     WHERE recorded_at >= date_trunc('month', now())),
        'support_open', (SELECT count(*) FROM support_cases
                         WHERE status NOT IN ('resolved', 'closed')),
        'onboarding_complete_pct', (
            SELECT CASE WHEN count(*) = 0 THEN 100 ELSE
                round(100.0 * SUM(CASE WHEN open_steps = 0 THEN 1 ELSE 0 END)
                      / count(*), 1) END
            FROM (SELECT t.id,
                         (SELECT count(*) FROM tenant_setup_steps st
                           WHERE st.tenant_id = t.id AND st.state <> 'complete') AS open_steps
                  FROM tenants t) onboarding),
        'open_incidents', (SELECT count(*) FROM provider_incidents WHERE status <> 'resolved')
    )
$$;

REVOKE ALL ON FUNCTION company_compute_metrics() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company_compute_metrics() TO salon_app;

GRANT SELECT, INSERT, UPDATE ON company_metric_snapshots TO salon_app;
