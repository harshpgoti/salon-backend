ALTER TABLE identity_users ADD COLUMN email text;
ALTER TABLE identity_users ADD COLUMN email_verified boolean NOT NULL DEFAULT false;
ALTER TABLE identity_users ADD COLUMN auth_time timestamptz;
ALTER TABLE identity_users ADD CONSTRAINT identity_verified_email_present CHECK (NOT email_verified OR email IS NOT NULL);

CREATE TABLE tenant_invitations (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    email text NOT NULL CHECK (length(email) BETWEEN 3 AND 254),
    role text NOT NULL CHECK (role IN ('manager','receptionist','stylist','analyst')),
    invited_by_issuer text NOT NULL,
    invited_by_subject text NOT NULL,
    state text NOT NULL CHECK (state IN ('pending','accepted','revoked','expired')),
    accepted_by_user_id uuid REFERENCES identity_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    accepted_at timestamptz,
    revoked_at timestamptz,
    PRIMARY KEY (tenant_id,id),
    CHECK (expires_at > created_at)
);
CREATE UNIQUE INDEX pending_invitation_email_unique ON tenant_invitations (tenant_id,email) WHERE state='pending';
CREATE TABLE invitation_locations (
    tenant_id uuid NOT NULL,
    invitation_id uuid NOT NULL,
    location_id uuid NOT NULL,
    PRIMARY KEY (tenant_id,invitation_id,location_id),
    FOREIGN KEY (tenant_id,invitation_id) REFERENCES tenant_invitations(tenant_id,id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id,location_id) REFERENCES locations(tenant_id,id) ON DELETE CASCADE
);
ALTER TABLE tenant_invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_invitations FORCE ROW LEVEL SECURITY;
CREATE POLICY invitation_tenant_scope ON tenant_invitations
    USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
ALTER TABLE invitation_locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE invitation_locations FORCE ROW LEVEL SECURITY;
CREATE POLICY invitation_location_tenant_scope ON invitation_locations
    USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);

CREATE TABLE ownership_transfers (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    from_user_id uuid NOT NULL,
    target_email text NOT NULL CHECK (length(target_email) BETWEEN 3 AND 254),
    accepted_by_user_id uuid REFERENCES identity_users(id),
    state text NOT NULL CHECK (state IN ('pending','accepted','revoked','expired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    accepted_at timestamptz,
    PRIMARY KEY (tenant_id,id),
    FOREIGN KEY (tenant_id,from_user_id) REFERENCES memberships(tenant_id,user_id),
    CHECK (expires_at > created_at)
);
CREATE UNIQUE INDEX one_pending_owner_transfer ON ownership_transfers (tenant_id) WHERE state='pending';
ALTER TABLE ownership_transfers ENABLE ROW LEVEL SECURITY;
ALTER TABLE ownership_transfers FORCE ROW LEVEL SECURITY;
CREATE POLICY ownership_transfer_tenant_scope ON ownership_transfers
    USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_invitations, invitation_locations, ownership_transfers TO salon_app;
GRANT UPDATE ON identity_users TO salon_app;
CREATE INDEX tenant_membership_list_idx ON memberships (tenant_id, role, created_at);
