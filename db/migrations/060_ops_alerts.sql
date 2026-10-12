-- TASK-060 / FR-078: operational alert state (company-owned, no RLS).
--
--   ops_alerts - one row per rule breach. ALERT_RULES in
--                modules/ops_alerts.py are evaluated against metrics_snapshot
--                (+ queue tables); a breach opens an 'active' row, recovery
--                resolves it. Dedupe is structural: the partial unique index
--                below permits exactly one active alert per (rule, tenant).
--                Global alerts use tenant_id NULL, coalesced to the zero UUID
--                so the dedupe covers them too.
--
-- Like the other company tables (no tenant RLS): reads/writes ride company
-- authorization at the route layer. There are no routes in this task; the
-- module exposes pure functions + SQL only.

CREATE TABLE ops_alerts (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    tenant_id uuid,
    rule text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('minor', 'major', 'critical')),
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'resolved')),
    metric_value numeric,
    fired_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    PRIMARY KEY (id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    CHECK ((state = 'resolved') = (resolved_at IS NOT NULL))
);

CREATE UNIQUE INDEX ops_alerts_one_active_per_rule_tenant
    ON ops_alerts (rule, COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'))
    WHERE state = 'active';

GRANT SELECT, INSERT, UPDATE ON ops_alerts TO salon_app;

CREATE INDEX ops_alerts_active_idx ON ops_alerts (rule, tenant_id)
    WHERE state = 'active';
