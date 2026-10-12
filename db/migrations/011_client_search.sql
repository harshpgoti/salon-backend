-- TASK-010: indexes for client search and timeline
-- No new tables; these are read-side indexes over existing structured sources.

-- clients: filters and stable ordering for paginated search
CREATE INDEX clients_tenant_lifecycle_idx ON clients (tenant_id, lifecycle) WHERE deleted_at IS NULL;
CREATE INDEX clients_tenant_location_idx ON clients (tenant_id, preferred_location_id) WHERE deleted_at IS NULL;
CREATE INDEX clients_tenant_name_idx ON clients (tenant_id, lower(name)) WHERE deleted_at IS NULL;

-- client_contacts: reverse lookup for contact-value search within tenant
CREATE INDEX client_contacts_tenant_normalized_idx ON client_contacts (tenant_id, normalized_value);

-- tenant_audit_events already has (tenant_id, occurred_at DESC, id) from migration 006