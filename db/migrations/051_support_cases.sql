-- TASK-051 / FR-071, FR-072: support cases and tenant-approved access.
--
-- Company-internal by design (like company_staff): no tenant RLS, every
-- route enforces a company role, and rows carry references — never client
-- PII. The default diagnostic view is masked (counts and IDs); case notes
-- are redacted on write.
--
--   support_cases        - one row per issue: tenant link, severity, status,
--                          assignee (company staff), redacted internal notes.
--   support_access_grants- temporary, tenant-approved authority to inspect
--                          one tenant for one case. Thirty minutes from
--                          approval, revocable by either side, read-only by
--                          default (scope names what may be seen, never write).
--   support_grant_uses   - every diagnostic read under a grant, so each use
--                          is audited and visible to the tenant.

CREATE TABLE support_cases (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
    severity text NOT NULL DEFAULT 'medium'
        CHECK (severity IN ('low', 'medium', 'high', 'urgent')),
    -- open -> triaged -> assigned -> resolved -> closed (forward only).
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'triaged', 'assigned', 'resolved', 'closed')),
    assignee_staff_id uuid,
    -- Safe correlates only: appointment / communication / trace identifiers.
    appointment_id uuid,
    communication_id uuid,
    trace_id text,
    internal_notes text NOT NULL DEFAULT '',
    opened_by_issuer text NOT NULL,
    opened_by_subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (assignee_staff_id) REFERENCES company_staff(id) ON DELETE SET NULL
);

CREATE TABLE support_access_grants (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    -- What may be inspected. Write is never granted by this table.
    scope text NOT NULL DEFAULT 'diagnostic'
        CHECK (scope IN ('diagnostic', 'billing')),
    -- requested -> approved | rejected; approved -> expired | revoked.
    status text NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'approved', 'rejected', 'expired', 'revoked')),
    requested_by_issuer text NOT NULL,
    requested_by_subject text NOT NULL,
    justification text NOT NULL CHECK (length(justification) BETWEEN 1 AND 500),
    approved_by_issuer text,
    approved_by_subject text,
    approved_at timestamptz,
    -- Thirty minutes from approval (FR-072 proposed expiry).
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (id),
    FOREIGN KEY (case_id) REFERENCES support_cases(id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE support_grant_uses (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    grant_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    actor_issuer text NOT NULL,
    actor_subject text NOT NULL,
    action text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    FOREIGN KEY (grant_id) REFERENCES support_access_grants(id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

GRANT SELECT, INSERT, UPDATE ON support_cases, support_access_grants, support_grant_uses
    TO salon_app;

CREATE INDEX support_cases_tenant_status_idx ON support_cases (tenant_id, status);
CREATE INDEX support_grants_tenant_status_idx
    ON support_access_grants (tenant_id, status) WHERE status = 'approved';

-- Company support reads tenant data through these contained SECURITY DEFINER
-- functions (same pattern as the registry, TASK-050): no per-table company
-- holes in tenant RLS, aggregates and identifiers only, EXECUTE granted to
-- salon_app with the company role check staying in the API layer.
CREATE OR REPLACE FUNCTION company_tenant_name(p_tenant uuid)
RETURNS text
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$ SELECT display_name FROM tenants WHERE id = p_tenant $$;

CREATE OR REPLACE FUNCTION company_case_list()
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COALESCE(jsonb_agg(row ORDER BY row.created_at DESC), '[]'::jsonb)
    FROM (
        SELECT c.id AS id, c.tenant_id AS tenant_id, t.display_name AS tenant_name,
               c.title, c.severity, c.status,
               c.assignee_staff_id AS assignee, c.appointment_id, c.communication_id,
               c.trace_id, c.internal_notes, c.created_at
        FROM support_cases c
        JOIN tenants t ON t.id = c.tenant_id
    ) row
$$;

CREATE OR REPLACE FUNCTION company_tenant_diagnostics(p_tenant uuid)
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT to_jsonb(row) FROM (
        SELECT t.id AS tenant_id, t.display_name AS business_name,
               s.status AS subscription_status, p.code AS plan_code,
               (SELECT count(*) FROM locations l WHERE l.tenant_id = t.id) AS locations,
               (SELECT count(*) FROM appointments a
                 WHERE a.tenant_id = t.id AND a.status = 'pending_approval') AS pending_approvals,
               (SELECT count(*) FROM communications c
                 WHERE c.tenant_id = t.id AND c.state = 'failed') AS failed_communications,
               (SELECT count(*) FROM dead_letters d
                 WHERE d.tenant_id = t.id AND d.resolved_at IS NULL) AS open_dead_letters,
               (SELECT count(*) FROM follow_up_tasks f
                 WHERE f.tenant_id = t.id AND f.status = 'open') AS open_follow_up_tasks,
               (SELECT COALESCE(jsonb_agg(x ORDER BY x DESC), '[]'::jsonb) FROM (
                    SELECT c.id FROM communications c
                    WHERE c.tenant_id = t.id AND c.state = 'failed'
                    ORDER BY c.created_at DESC LIMIT 10) x) AS recent_failed_ids
        FROM tenants t
        LEFT JOIN tenant_subscriptions s ON s.tenant_id = t.id
        LEFT JOIN billing_plans p ON p.id = s.plan_id
        WHERE t.id = p_tenant
    ) row
$$;

REVOKE ALL ON FUNCTION company_tenant_name(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company_tenant_name(uuid) TO salon_app;
REVOKE ALL ON FUNCTION company_case_list() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company_case_list() TO salon_app;
REVOKE ALL ON FUNCTION company_tenant_diagnostics(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company_tenant_diagnostics(uuid) TO salon_app;
