-- TASK-049 / FR-069: atomic usage allowances and messaging budgets.
--
--   messaging_budgets   - one row per salon: the tenant-approved spend ceiling
--                         (NULL = no cap) for the billing period. The row is
--                         locked (FOR UPDATE) inside the dispatch transaction,
--                         which is what makes concurrent reservations atomic.
--   usage_reservations  - one row per communication: the estimated cost held
--                         before sending, reconciled to the ledgered actual
--                         afterwards (or voided when a gate blocks the send).
--                         Unique per communication: a replayed dispatch can
--                         never reserve twice.
--   provider_usage      - provider-reported usage lines. Matched lines
--                         reconcile the reservation to provider actuals;
--                         duplicates are ignored; unknown lines stay pending
--                         instead of inventing charges.
--
-- Billing period: calendar month in the tenant's primary (earliest)
-- location timezone — defined once here, read by the resolver.

CREATE TABLE messaging_budgets (
    tenant_id uuid NOT NULL,
    -- NULL means no cap: plan allowances still report, nothing blocks.
    spend_ceiling_minor integer CHECK (spend_ceiling_minor IS NULL OR spend_ceiling_minor >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE usage_reservations (
    tenant_id uuid NOT NULL,
    communication_id uuid NOT NULL,
    estimated_minor integer NOT NULL CHECK (estimated_minor >= 0),
    actual_minor integer,
    -- reserved   = held before the send
    -- reconciled = send completed; actual matches the cost ledger
    -- voided     = a later gate blocked the send; holds nothing
    state text NOT NULL DEFAULT 'reserved'
        CHECK (state IN ('reserved', 'reconciled', 'voided')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, communication_id),
    FOREIGN KEY (tenant_id, communication_id)
        REFERENCES communications(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE provider_usage (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    provider_ref text NOT NULL,
    communication_id uuid,
    segments integer CHECK (segments IS NULL OR segments >= 1),
    cost_minor integer CHECK (cost_minor IS NULL OR cost_minor >= 0),
    -- pending = no reservation matches yet; kept, never billed blindly
    -- matched = reconciled against the reservation exactly once
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'matched')),
    reported_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, provider_ref),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

ALTER TABLE messaging_budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE messaging_budgets FORCE ROW LEVEL SECURITY;
CREATE POLICY messaging_budgets_tenant_scope ON messaging_budgets
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE usage_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_reservations FORCE ROW LEVEL SECURITY;
CREATE POLICY usage_reservations_tenant_scope ON usage_reservations
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE provider_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_usage FORCE ROW LEVEL SECURITY;
CREATE POLICY provider_usage_tenant_scope ON provider_usage
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON messaging_budgets, usage_reservations, provider_usage
    TO salon_app;

CREATE INDEX usage_reservations_tenant_state_idx
    ON usage_reservations (tenant_id, state);
