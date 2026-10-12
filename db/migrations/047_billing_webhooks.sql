-- TASK-047 / FR-067: billing webhook dedupe and entitlement projection.
--
--   billing_provider_secrets - company-global HMAC secret for the test
--                              provider (the Stripe account is the company's,
--                              not the salon's). Global catalog table like
--                              billing_plans: no tenant scope, API-gated.
--   billing_customer_map     - opaque provider customer ref -> tenant. Global
--                              and PII-free (refs and UUIDs only) so the
--                              unsigned webhook route can resolve scope before
--                              verifying the signature.
--   billing_events           - every provider billing event, deduplicated by
--                              provider event id. The audit trail is the
--                              truth; the subscription row is its projection.
--   tenant_entitlements      - idempotent projection of plan limits plus the
--                              marketing flag, rebuilt on every applied event
--                              so out-of-order redelivery converges.
--
-- tenant_subscriptions gains last_provider_event_at: events older than the
-- last applied one never regress state (out-of-order safety).

CREATE TABLE billing_provider_secrets (
    provider text NOT NULL DEFAULT 'stripe_test',
    secret text NOT NULL CHECK (length(secret) >= 32),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider)
);

CREATE TABLE billing_customer_map (
    customer_ref text NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_ref),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE billing_events (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    provider_event_id text NOT NULL,
    event_type text NOT NULL CHECK (event_type IN (
        'checkout.session.completed', 'invoice.paid', 'invoice.payment_failed',
        'customer.subscription.updated', 'charge.refunded', 'dispute.created',
        'payment.requires_action')),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (provider_event_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE tenant_entitlements (
    tenant_id uuid NOT NULL,
    plan_code text NOT NULL,
    status text NOT NULL,
    max_locations integer NOT NULL,
    max_staff integer NOT NULL,
    included_sms_segments integer NOT NULL,
    included_emails integer NOT NULL,
    marketing_allowed boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

ALTER TABLE tenant_subscriptions ADD COLUMN last_provider_event_at timestamptz;

INSERT INTO billing_provider_secrets (provider, secret)
    VALUES ('stripe_test', 'billing-webhook-secret-0123456789abcdef0123456789');

ALTER TABLE billing_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_events FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_events_tenant_scope ON billing_events
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE tenant_entitlements ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_entitlements FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_entitlements_tenant_scope ON tenant_entitlements
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT ON billing_provider_secrets, billing_customer_map TO salon_app;
GRANT SELECT, INSERT ON billing_events TO salon_app;
GRANT SELECT, INSERT, UPDATE ON tenant_entitlements TO salon_app;

CREATE INDEX billing_events_tenant_type_idx ON billing_events (tenant_id, event_type);
