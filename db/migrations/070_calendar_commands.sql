-- Request-bound successful calendar receipts, retained with their appointment.
CREATE TABLE calendar_commands (
    tenant_id uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 64),
    appointment_id uuid NOT NULL,
    request_hash text NOT NULL CHECK (length(request_hash) = 64),
    response jsonb NOT NULL CHECK (jsonb_typeof(response) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, appointment_id) REFERENCES appointments(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX calendar_commands_appointment_idx ON calendar_commands (tenant_id, appointment_id);
ALTER TABLE calendar_commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_commands FORCE ROW LEVEL SECURITY;
CREATE POLICY calendar_commands_tenant_scope ON calendar_commands
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT ON calendar_commands TO salon_app;
