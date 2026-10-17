-- TASK-026: preserve closure-resolution outcomes across lost HTTP responses.
CREATE TABLE closure_resolution_receipts (
    tenant_id uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 64),
    impact_id uuid NOT NULL,
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, impact_id)
        REFERENCES closure_impacts(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE closure_resolution_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE closure_resolution_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY closure_resolution_receipts_tenant_scope ON closure_resolution_receipts
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT ON closure_resolution_receipts TO salon_app;
