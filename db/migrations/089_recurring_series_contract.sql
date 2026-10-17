-- TASK-064: idempotent series creation and one appointment per series occurrence.
ALTER TABLE recurring_series ADD COLUMN IF NOT EXISTS idempotency_key text;
ALTER TABLE recurring_series ADD COLUMN IF NOT EXISTS request_hash text;
CREATE UNIQUE INDEX IF NOT EXISTS recurring_series_idempotency_idx
    ON recurring_series (tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS appointments_series_occurrence_idx
    ON appointments (tenant_id,series_id,occurrence_index)
    WHERE series_id IS NOT NULL AND occurrence_index IS NOT NULL;
CREATE TABLE IF NOT EXISTS recurring_series_changes (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    series_id uuid NOT NULL,
    appointment_id uuid NOT NULL,
    actor_issuer text NOT NULL,
    actor_subject text NOT NULL,
    previous_starts_at timestamptz NOT NULL,
    next_starts_at timestamptz NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,id),
    FOREIGN KEY (tenant_id,series_id) REFERENCES recurring_series(tenant_id,id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id,appointment_id) REFERENCES appointments(tenant_id,id) ON DELETE CASCADE
);
ALTER TABLE recurring_series_changes ENABLE ROW LEVEL SECURITY;
ALTER TABLE recurring_series_changes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS recurring_series_changes_tenant_scope ON recurring_series_changes;
CREATE POLICY recurring_series_changes_tenant_scope ON recurring_series_changes
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT,INSERT ON recurring_series_changes TO salon_app;
GRANT SELECT,INSERT,UPDATE ON recurring_series TO salon_app;
