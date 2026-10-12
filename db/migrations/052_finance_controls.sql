-- TASK-052 / FR-073, FR-074: finance adjustments and operations controls.
--
-- Company-global tables (like company_staff): no tenant RLS, every route
-- enforces a company role instead.
--
--   finance_adjustments - refunds, credits and discounts with a four-eyes
--                         rule: requester and approver must differ. Execution
--                         is recorded with a synthetic provider reference
--                         (sandbox; DQ-006).
--   company_tax_config  - single-row tax behaviour config. Rates start NULL:
--                         counsel/finance set them; the product never assumes.
--   provider_incidents  - provider-side incidents with lifecycle.
--   company_feature_flags - per-tenant (or global, NULL tenant) kill switches
--                         and rollout gates. Flags nobody reads are theater:
--                         the rebooking worker enforces
--                         rebooking_prompts_enabled (default on).
--   status_notices      - public incident notices; the only public
--                         (unauthenticated) company surface in this task.

CREATE TABLE finance_adjustments (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    -- refund | credit | discount. Money only ever flows back to the salon.
    kind text NOT NULL CHECK (kind IN ('refund', 'credit', 'discount')),
    amount_minor integer NOT NULL CHECK (amount_minor > 0),
    currency text NOT NULL DEFAULT 'CAD' CHECK (currency = 'CAD'),
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    -- requested -> approved | rejected; approved -> executed.
    status text NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'approved', 'rejected', 'executed')),
    requested_by_issuer text NOT NULL,
    requested_by_subject text NOT NULL,
    approved_by_issuer text,
    approved_by_subject text,
    provider_ref text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE company_tax_config (
    id integer NOT NULL DEFAULT 1 CHECK (id = 1),
    country text NOT NULL DEFAULT 'CA',
    -- NULL until counsel/finance configures: the product assumes no rates.
    gst_hst_rate numeric(5, 4),
    pst_rate numeric(5, 4),
    qst_rate numeric(5, 4),
    notes text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE TABLE provider_incidents (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    provider text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
    detail text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'mitigated', 'resolved')),
    started_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE TABLE company_feature_flags (
    tenant_id uuid,
    flag text NOT NULL CHECK (flag ~ '^[a-z_]{1,64}$'),
    enabled boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, flag),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE status_notices (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
    severity text NOT NULL CHECK (severity IN ('info', 'degraded', 'outage')),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

ALTER TABLE tenant_subscriptions ADD COLUMN status_before_suspension text;

INSERT INTO company_tax_config (id) VALUES (1);

GRANT SELECT, INSERT, UPDATE ON finance_adjustments, company_tax_config,
    provider_incidents, company_feature_flags, status_notices TO salon_app;

CREATE INDEX finance_adjustments_tenant_status_idx
    ON finance_adjustments (tenant_id, status);

-- Cross-tenant finance and ops reads for company roles (same contained
-- pattern as the registry: SECURITY DEFINER, aggregates and identifiers,
-- EXECUTE to salon_app, role checks in the API).
CREATE OR REPLACE FUNCTION company_finance_ledger(p_since timestamptz)
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COALESCE(jsonb_agg(row ORDER BY row.created_at DESC), '[]'::jsonb)
    FROM (
        SELECT e.tenant_id, e.provider_event_id AS reference, e.event_type AS kind,
               (e.payload->>'amount_minor') AS amount_minor, e.occurred_at AS created_at
        FROM billing_events e WHERE e.occurred_at >= p_since
        UNION ALL
        SELECT f.tenant_id, f.id::text AS reference,
               ('adjustment_' || f.kind || '_' || f.status) AS kind,
               f.amount_minor::text AS amount_minor, f.created_at
        FROM finance_adjustments f WHERE f.created_at >= p_since
    ) row
$$;

CREATE OR REPLACE FUNCTION company_reconciliation()
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COALESCE(jsonb_agg(row), '[]'::jsonb)
    FROM (
        SELECT t.id AS tenant_id, t.display_name AS business_name,
               s.status AS subscription_status, e.status AS entitlement_status,
               s.plan_id <> (SELECT plan_id FROM billing_plans p
                             WHERE p.code = e.plan_code ORDER BY version DESC LIMIT 1)
                 AS plan_mismatch,
               (SELECT max(occurred_at) FROM billing_events b
                 WHERE b.tenant_id = t.id) AS last_event_at
        FROM tenants t
        LEFT JOIN tenant_subscriptions s ON s.tenant_id = t.id
        LEFT JOIN tenant_entitlements e ON e.tenant_id = t.id
        -- Trial tenants carry no projection until their first billing event;
        -- that absence is expected, not anomalous. Everything else that
        -- disagrees is.
        WHERE (s.status IS DISTINCT FROM e.status)
          AND NOT (s.status = 'trial' AND e.tenant_id IS NULL)
    ) row
$$;

CREATE OR REPLACE FUNCTION company_ops_queues()
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT jsonb_build_object(
        'retry_queued', (SELECT count(*) FROM dispatch_attempts WHERE state = 'queued'),
        'retry_sending', (SELECT count(*) FROM dispatch_attempts WHERE state = 'sending'),
        'dead_letters_open', (SELECT count(*) FROM dead_letters WHERE resolved_at IS NULL),
        'provider_failed_24h', (SELECT count(*) FROM provider_callbacks
                                WHERE event = 'failed' AND created_at > now() - interval '24 hours'),
        'billing_events_24h', (SELECT count(*) FROM billing_events
                               WHERE created_at > now() - interval '24 hours'),
        'open_incidents', (SELECT count(*) FROM provider_incidents WHERE status <> 'resolved'),
        'active_notices', (SELECT count(*) FROM status_notices WHERE is_active)
    )
$$;

REVOKE ALL ON FUNCTION company_finance_ledger(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company_finance_ledger(timestamptz) TO salon_app;
REVOKE ALL ON FUNCTION company_reconciliation() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company_reconciliation() TO salon_app;
REVOKE ALL ON FUNCTION company_ops_queues() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company_ops_queues() TO salon_app;
