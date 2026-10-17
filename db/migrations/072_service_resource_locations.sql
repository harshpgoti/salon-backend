-- TASK-018 / FR-021: service requirements are configured per physical location.
-- A service may be offered at multiple locations, but a chair/room/equipment
-- requirement can only refer to resources at the selected location.

ALTER TABLE bookable_resources
    ADD CONSTRAINT bookable_resources_tenant_id_location_unique
    UNIQUE (tenant_id, id, location_id);

ALTER TABLE service_resource_requirements
    ADD COLUMN location_id uuid;

UPDATE service_resource_requirements q
SET location_id=r.location_id
FROM bookable_resources r
WHERE r.tenant_id=q.tenant_id AND r.id=q.resource_id;

ALTER TABLE service_resource_requirements
    ALTER COLUMN location_id SET NOT NULL,
    ADD CONSTRAINT srr_location_owned
        FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE,
    ADD CONSTRAINT srr_resource_at_location
        FOREIGN KEY (tenant_id, resource_id, location_id)
        REFERENCES bookable_resources(tenant_id, id, location_id) ON DELETE CASCADE;

CREATE INDEX srr_tenant_service_location_idx
    ON service_resource_requirements (tenant_id, service_id, location_id);
