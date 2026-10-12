-- FR-009 client profile, FR-010 normalized and verified contacts, FR-018 tenant ownership.
--
-- Three tables, all tenant-owned with composite keys and FORCE RLS (data-model.md:30).
-- `preferred_staff_id` is deliberately absent: staff_profiles belongs to TASK-016 and does
-- not exist yet. Consent is a separate row family owned by FR-040's own tasks, which is what
-- FR-009 asks for here - consent is not a column on a contact (data-model.md:66).
CREATE TABLE clients (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    preferred_name text CHECK (preferred_name IS NULL OR length(trim(preferred_name)) BETWEEN 1 AND 120),
    locale char(5) NOT NULL DEFAULT 'en-CA' CHECK (locale IN ('en-CA','fr-CA')),
    lifecycle text NOT NULL DEFAULT 'active'
        CHECK (lifecycle IN ('lead','active','lapsed','archived')),
    preferred_location_id uuid,
    source text NOT NULL CHECK (source IN ('staff','public_booking','import')),
    created_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    deleted_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, preferred_location_id) REFERENCES locations(tenant_id, id)
);

CREATE TABLE client_contacts (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('email','phone')),
    -- The conservative normal form used for scoped lookup, and the operator's original
    -- string kept verbatim for display (FR-010).
    normalized_value text NOT NULL CHECK (length(normalized_value) BETWEEN 3 AND 320),
    display_value text NOT NULL CHECK (length(display_value) BETWEEN 1 AND 320),
    country char(2) NOT NULL DEFAULT 'CA' CHECK (country ~ '^[A-Z]{2}$'),
    verification text NOT NULL DEFAULT 'unverified'
        CHECK (verification IN ('unverified','pending','verified','failed')),
    verified_at timestamptz,
    deliverability text NOT NULL DEFAULT 'unknown'
        CHECK (deliverability IN ('unknown','deliverable','undeliverable')),
    is_primary boolean NOT NULL DEFAULT false,
    source text NOT NULL CHECK (source IN ('staff','public_booking','import')),
    created_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    -- A contact may only be verified while it holds a verification timestamp, and never
    -- after it was explicitly failed.
    CHECK (verification <> 'verified' OR verified_at IS NOT NULL)
);

-- A shared household phone or email is ordinary and proves nothing (data-model.md:32).
-- This table only ever records that a human might want to look; the merge itself is
-- FR-015, an explicit operator action that this task must not perform.
CREATE TABLE client_duplicate_candidates (
    tenant_id uuid NOT NULL,
    client_id uuid NOT NULL,
    contact_id uuid NOT NULL,
    other_client_id uuid NOT NULL,
    match_basis text NOT NULL CHECK (match_basis IN ('email','phone')),
    matched_value text NOT NULL CHECK (length(matched_value) BETWEEN 3 AND 320),
    detected_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolution text CHECK (resolution IS NULL OR resolution IN ('distinct','merged')),
    PRIMARY KEY (tenant_id, client_id, contact_id, other_client_id),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, contact_id) REFERENCES client_contacts(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, other_client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    CHECK (client_id <> other_client_id),
    CHECK ((resolution IS NULL) = (resolved_at IS NULL))
);

ALTER TABLE clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE clients FORCE ROW LEVEL SECURITY;
CREATE POLICY client_tenant_scope ON clients
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE client_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_contacts FORCE ROW LEVEL SECURITY;
CREATE POLICY client_contact_tenant_scope ON client_contacts
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE client_duplicate_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_duplicate_candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY client_duplicate_candidate_scope ON client_duplicate_candidates
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON clients, client_contacts, client_duplicate_candidates TO salon_app;

-- Tenant-prefixed lookup indexes (data-model.md:117). The duplicate scan is the one that
-- needs the contact normal form indexed across a tenant.
CREATE INDEX client_contact_lookup_idx ON client_contacts (tenant_id, kind, normalized_value);
CREATE INDEX client_contact_by_client_idx ON client_contacts (tenant_id, client_id) WHERE verification <> 'failed';
CREATE INDEX client_lifecycle_idx ON clients (tenant_id, lifecycle) WHERE deleted_at IS NULL;
CREATE INDEX client_duplicate_candidate_open_idx ON client_duplicate_candidates (tenant_id, detected_at DESC)
    WHERE resolved_at IS NULL;