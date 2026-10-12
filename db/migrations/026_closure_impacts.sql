-- TASK-026 / FR-025, FR-026: Closure impact resolution preserves affected bookings
-- When a closure is published, existing appointments that overlap with the closure
-- are tracked so owners can choose to reschedule, cancel, or leave them.
--
-- AC-1: Closure blocks new slots and lists existing appointments to resolve
-- AC-2: Affected notices and unreachable follow-up tasks reflect explicit decisions

CREATE TABLE closure_impacts (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    location_id uuid NOT NULL,
    exception_date date NOT NULL,
    appointment_id uuid NOT NULL,
    client_id uuid NOT NULL,
    staff_id uuid NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'rescheduled', 'cancelled', 'left')),
    resolution text,
    resolved_at timestamptz,
    resolved_by uuid,
    resolution_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, location_id, exception_date, appointment_id),
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, appointment_id) REFERENCES appointments(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, resolved_by) REFERENCES staff_profiles(tenant_id, id)
);

ALTER TABLE closure_impacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE closure_impacts FORCE ROW LEVEL SECURITY;
CREATE POLICY closure_impacts_tenant_scope ON closure_impacts
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

CREATE INDEX closure_impacts_location_date_idx ON closure_impacts (tenant_id, location_id, exception_date);
CREATE INDEX closure_impacts_appointment_idx ON closure_impacts (tenant_id, appointment_id);
CREATE INDEX closure_impacts_status_idx ON closure_impacts (tenant_id, status);

GRANT SELECT, INSERT, UPDATE ON closure_impacts TO salon_app;