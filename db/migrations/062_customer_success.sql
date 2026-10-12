-- TASK-062 / FR-075: customer success tasks and explainable health.
--
--   company_tasks - business leads (no tenant yet), onboarding assists,
--                   renewal risks and risk follow-ups. Status open/done/
--                   dismissed with who/when. Company-global like the rest of
--                   the company surface: role-gated, never tenant-scoped.
--
-- Health itself is computed, not stored (company.py): a documented 0-100
-- score with per-deduction reasons, from aggregates only — client data is
-- never reused for company sales, by construction (no client column is read).

CREATE TABLE company_tasks (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    tenant_id uuid,
    -- lead (no tenant yet) | onboarding | renewal | risk
    kind text NOT NULL CHECK (kind IN ('lead', 'onboarding', 'renewal', 'risk')),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
    detail text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'done', 'dismissed')),
    assignee_staff_id uuid,
    due_at timestamptz,
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (assignee_staff_id) REFERENCES company_staff(id) ON DELETE SET NULL
);

GRANT SELECT, INSERT, UPDATE ON company_tasks TO salon_app;

CREATE INDEX company_tasks_tenant_status_idx
    ON company_tasks (tenant_id, status) WHERE status = 'open';

-- Success signals for every tenant in one call (same contained pattern as
-- the registry: SECURITY DEFINER, aggregates only, EXECUTE to salon_app,
-- role checks in the API).
CREATE OR REPLACE FUNCTION company_success_signals()
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COALESCE(jsonb_agg(row), '[]'::jsonb)
    FROM (
        SELECT t.id AS tenant_id, t.display_name AS business_name,
               s.status AS subscription_status, s.trial_ends_at,
               (SELECT count(*) FROM tenant_setup_steps st
                 WHERE st.tenant_id = t.id AND st.state <> 'complete') AS setup_open,
               (SELECT count(*) FROM support_cases c
                 WHERE c.tenant_id = t.id AND c.severity IN ('high', 'urgent')
                   AND c.status NOT IN ('resolved', 'closed')) AS urgent_cases,
               (SELECT count(*) FROM communications m
                 WHERE m.tenant_id = t.id AND m.state = 'failed') AS failed_messages
        FROM tenants t
        LEFT JOIN tenant_subscriptions s ON s.tenant_id = t.id
    ) row
$$;

REVOKE ALL ON FUNCTION company_success_signals() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company_success_signals() TO salon_app;
