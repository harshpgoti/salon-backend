-- TASK-046 / FR-065, FR-066: plans, subscriptions and hosted checkout.
--
--   billing_plans            - the company catalogue: Essential, Growth,
--                              Multi-location. Prices and limits are ROWS
--                              (configurable commercial hypotheses, DQ-005),
--                              never hard-coded; versions accumulate, rows are
--                              never edited in place. Global catalog (no
--                              tenant), read through the staff-gated API.
--   tenant_subscriptions     - one row per salon: its plan snapshot, lifecycle
--                              status and provider references. Card data is
--                              NEVER stored here or anywhere (AC-2) — only
--                              opaque provider refs. Status moves only on
--                              signed webhooks (TASK-047), never from the
--                              browser success page.
--   billing_checkout_sessions- hosted checkout sessions from the test adapter:
--                              pending until the provider webhook completes
--                              them; polling the session grants nothing.

CREATE TABLE billing_plans (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    code text NOT NULL CHECK (code IN ('essential', 'growth', 'multi_location')),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    name text NOT NULL,
    price_minor integer NOT NULL CHECK (price_minor >= 0),
    currency text NOT NULL DEFAULT 'CAD' CHECK (currency = 'CAD'),
    -- Included limits: max_locations, max_staff, included_sms_segments,
    -- included_emails. Overage is metered (TASK-049 reads these).
    limits jsonb NOT NULL DEFAULT '{}',
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (code, version)
);

CREATE TABLE tenant_subscriptions (
    tenant_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    -- trial      = onboarding default; converts on first webhook
    -- active     = paid and current
    -- past_due   = payment failed; grace rules apply (TASK-048)
    -- grace      = seven-day recovery window (TASK-048)
    -- restricted = read/export/billing-recovery only (TASK-048)
    -- cancelled  = ends at period end; operations continue till then
    -- suspended  = security suspension, independent of billing (TASK-052)
    status text NOT NULL DEFAULT 'trial'
        CHECK (status IN ('trial', 'active', 'past_due', 'grace', 'restricted',
                          'cancelled', 'suspended')),
    stripe_customer_ref text,
    stripe_subscription_ref text,
    trial_ends_at timestamptz,
    current_period_end timestamptz,
    cancel_at_period_end boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (plan_id) REFERENCES billing_plans(id)
);

CREATE TABLE billing_checkout_sessions (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    plan_id uuid NOT NULL,
    -- pending  = awaiting provider webhook; polling changes nothing
    -- complete = webhook confirmed (TASK-047 flips this, not the browser)
    -- expired  = abandoned past its expiry
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'complete', 'expired')),
    provider_session_ref text NOT NULL,
    checkout_url text NOT NULL,
    expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (plan_id) REFERENCES billing_plans(id)
);

INSERT INTO billing_plans (code, version, name, price_minor, limits) VALUES
    ('essential', 1, 'Essential', 4900,
     '{"max_locations": 1, "max_staff": 5, "included_sms_segments": 200, "included_emails": 2000}'),
    ('growth', 1, 'Growth', 9900,
     '{"max_locations": 1, "max_staff": 15, "included_sms_segments": 1000, "included_emails": 10000}'),
    ('multi_location', 1, 'Multi-location', 19900,
     '{"max_locations": 5, "max_staff": 50, "included_sms_segments": 3000, "included_emails": 30000}');

ALTER TABLE tenant_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_subscriptions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_subscriptions_tenant_scope ON tenant_subscriptions
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE billing_checkout_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_checkout_sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_checkout_sessions_tenant_scope ON billing_checkout_sessions
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- The catalogue is company-global: no tenant scope, staff-gated reads only.
GRANT SELECT ON billing_plans TO salon_app;
GRANT SELECT, INSERT, UPDATE ON tenant_subscriptions, billing_checkout_sessions TO salon_app;
