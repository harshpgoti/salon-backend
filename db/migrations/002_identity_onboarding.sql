ALTER TABLE tenants ADD COLUMN default_currency char(3) NOT NULL DEFAULT 'CAD';
ALTER TABLE locations ADD COLUMN slug text;
ALTER TABLE locations ADD COLUMN booking_slug text;
UPDATE locations SET slug = 'main-' || left(replace(id::text, '-', ''), 8), booking_slug = 'salon-' || left(replace(tenant_id::text, '-', ''), 12);
ALTER TABLE locations ALTER COLUMN slug SET NOT NULL;
ALTER TABLE locations ALTER COLUMN booking_slug SET NOT NULL;
ALTER TABLE locations ADD CONSTRAINT locations_tenant_slug_unique UNIQUE (tenant_id, slug);
ALTER TABLE locations ADD CONSTRAINT locations_booking_slug_unique UNIQUE (booking_slug);

-- Identity subjects are opaque issuer-scoped references; credentials stay at the OIDC provider.
CREATE TABLE identity_users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    issuer text NOT NULL,
    subject text NOT NULL,
    tenant_id uuid REFERENCES tenants(id),
    onboarding_fingerprint text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (issuer, subject),
    CHECK ((tenant_id IS NULL) = (onboarding_fingerprint IS NULL))
);

CREATE TABLE memberships (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES identity_users(id),
    role text NOT NULL CHECK (role IN ('owner','manager','receptionist','stylist','analyst')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);
ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY membership_tenant_scope ON memberships
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON identity_users TO salon_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON memberships TO salon_app;
GRANT SELECT, INSERT, UPDATE ON tenants TO salon_app;
GRANT SELECT, INSERT, UPDATE ON locations TO salon_app;
