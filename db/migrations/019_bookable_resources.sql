-- TASK-018 / FR-021: bookable resources with exclusive capacity-one occupancy.
--
-- Two tables, tenant-owned with FORCE RLS (data-model.md:30):
--   bookable_resources            - a chair, room or piece of equipment at one location.
--   service_resource_requirements - which resources a service needs, and how many.
--
-- Capacity is one for R1 (requirements.md:58): a resource is exclusively occupied for the
-- whole service, including its buffers. Capacity pools and partially-unattended phases are
-- R3 and deliberately not modelled here. The requirement count is constrained to exactly 1
-- so nothing in the schema implies a pool this release cannot honour.

CREATE TABLE bookable_resources (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    -- A resource belongs to exactly one location and can never be shared across locations.
    location_id uuid NOT NULL,
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    kind text NOT NULL CHECK (kind IN ('chair','room','equipment')),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, location_id, name),
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE service_resource_requirements (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL,
    resource_id uuid NOT NULL,
    -- R1 is conservative whole-service occupancy; a pool is R3, so the count is fixed at 1.
    quantity smallint NOT NULL DEFAULT 1 CHECK (quantity = 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, service_id, resource_id),
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, resource_id) REFERENCES bookable_resources(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE bookable_resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE bookable_resources FORCE ROW LEVEL SECURITY;
CREATE POLICY bookable_resources_tenant_scope ON bookable_resources
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE service_resource_requirements ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_resource_requirements FORCE ROW LEVEL SECURITY;
CREATE POLICY srr_tenant_scope ON service_resource_requirements
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON bookable_resources, service_resource_requirements TO salon_app;

CREATE INDEX bookable_resources_location_idx ON bookable_resources (tenant_id, location_id) WHERE active;
CREATE INDEX srr_tenant_service_idx ON service_resource_requirements (tenant_id, service_id);