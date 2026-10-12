CREATE TABLE membership_locations (
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    location_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, location_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES memberships(tenant_id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE
);
ALTER TABLE membership_locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE membership_locations FORCE ROW LEVEL SECURITY;
CREATE POLICY membership_location_tenant_scope ON membership_locations
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON membership_locations TO salon_app;
