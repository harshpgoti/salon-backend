-- TASK-047 / FR-067: canonical provider facts for the local synthetic adapter.
-- This table models the retrieval result of the test provider; it is not a
-- substitute for live Stripe API access or evidence of a configured account.
CREATE TABLE billing_provider_facts (
    tenant_id uuid NOT NULL,
    subscription_ref text NOT NULL,
    customer_ref text NOT NULL,
    provider_status text NOT NULL,
    plan_code text NOT NULL,
    period_end timestamptz,
    cancel_at_period_end boolean NOT NULL DEFAULT false,
    provider_updated_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, subscription_ref),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

ALTER TABLE billing_provider_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_provider_facts FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_provider_facts_tenant_scope ON billing_provider_facts
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT ON billing_provider_facts TO salon_app;

