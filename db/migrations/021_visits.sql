-- TASK-024: completed-service facts (FR-031/FR-034).
--
-- A `visits` row is the structured, immutable record that a service actually happened.
-- It is the reused fact rebooking (FR-049) and analytics (FR-042) read, rather than each
-- subsystem re-deriving "completed" from appointment status. `visit_services` snapshots
-- each completed service line so a later service edit never rewrites history.
--
-- Both tables are tenant-owned with composite keys and FORCE RLS (data-model.md:30,:83).

CREATE TABLE visits (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    appointment_id uuid NOT NULL,
    client_id uuid NOT NULL,
    staff_id uuid NOT NULL,
    location_id uuid NOT NULL,
    completed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    -- One visit per appointment: a replayed completion is a lookup, not a second visit.
    UNIQUE (tenant_id, appointment_id),
    FOREIGN KEY (tenant_id, appointment_id) REFERENCES appointments(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE visit_services (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    visit_id uuid NOT NULL,
    appointment_service_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence >= 1),
    service_id uuid NOT NULL,
    -- Frozen at completion so a later catalogue edit never rewrites what was delivered.
    service_version integer NOT NULL,
    service_name text NOT NULL,
    staff_id uuid NOT NULL,
    price_minor integer NOT NULL CHECK (price_minor >= 0),
    currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency = 'CAD'),
    duration_minutes integer NOT NULL CHECK (duration_minutes >= 5),
    pre_buffer_minutes integer NOT NULL DEFAULT 0,
    post_buffer_minutes integer NOT NULL DEFAULT 0,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, visit_id, sequence),
    FOREIGN KEY (tenant_id, visit_id) REFERENCES visits(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, appointment_service_id) REFERENCES appointment_services(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE visits ENABLE ROW LEVEL SECURITY;
ALTER TABLE visits FORCE ROW LEVEL SECURITY;
CREATE POLICY visits_tenant_scope ON visits
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE visit_services ENABLE ROW LEVEL SECURITY;
ALTER TABLE visit_services FORCE ROW LEVEL SECURITY;
CREATE POLICY visit_services_tenant_scope ON visit_services
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON visits, visit_services TO salon_app;

CREATE INDEX visits_tenant_client_completed_idx ON visits (tenant_id, client_id, completed_at);
CREATE INDEX visit_services_tenant_service_idx ON visit_services (tenant_id, service_id);
