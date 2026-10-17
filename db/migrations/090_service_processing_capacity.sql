-- TASK-067 / FR-021, FR-032: versioned service phases and capacity-backed resource pools.

ALTER TABLE bookable_resources
    ADD COLUMN capacity smallint NOT NULL DEFAULT 1
        CHECK (capacity BETWEEN 1 AND 100);

ALTER TABLE service_resource_requirements
    DROP CONSTRAINT service_resource_requirements_quantity_check;
ALTER TABLE service_resource_requirements
    ADD CONSTRAINT service_resource_requirements_quantity_check
        CHECK (quantity BETWEEN 1 AND 100);

ALTER TABLE reservations
    ADD COLUMN resource_unit smallint NOT NULL DEFAULT 1
        CHECK (resource_unit BETWEEN 1 AND 100);
ALTER TABLE reservations DROP CONSTRAINT reservations_no_resource_overlap;
ALTER TABLE reservations ADD CONSTRAINT reservations_no_resource_overlap
    EXCLUDE USING gist
        (tenant_id WITH =, resource_id WITH =, resource_unit WITH =, occupied_range WITH &&)
    WHERE (state = 'active' AND resource_id IS NOT NULL);

CREATE TABLE service_processing_phases (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL,
    sequence smallint NOT NULL CHECK (sequence BETWEEN 1 AND 12),
    duration_minutes smallint NOT NULL CHECK (duration_minutes BETWEEN 1 AND 1440),
    mode text NOT NULL CHECK (mode IN ('attended','gap')),
    staff_count smallint NOT NULL CHECK (staff_count BETWEEN 0 AND 4),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, service_id, sequence),
    CHECK ((mode = 'attended' AND staff_count BETWEEN 1 AND 4)
        OR (mode = 'gap' AND staff_count = 0)),
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE service_processing_phases ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_processing_phases FORCE ROW LEVEL SECURITY;
CREATE POLICY service_processing_phases_tenant_scope ON service_processing_phases
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON service_processing_phases TO salon_app;
CREATE INDEX service_processing_phases_order_idx
    ON service_processing_phases (tenant_id, service_id, sequence);
