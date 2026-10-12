-- TASK-011: custom field definitions, field values, visit notes, client preferences
-- Forward references to appointments, services, staff_profiles use ON DELETE SET NULL

-- Custom field definitions (schema for typed fields)
CREATE TABLE client_field_definitions (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    entity_type text NOT NULL CHECK (entity_type IN ('client')),
    key text NOT NULL CHECK (length(key) BETWEEN 1 AND 64),
    label text NOT NULL CHECK (length(label) BETWEEN 1 AND 120),
    type text NOT NULL CHECK (type IN ('text','number','date','boolean','enum')),
    validation jsonb NOT NULL DEFAULT '{}',
    purpose text NOT NULL CHECK (length(purpose) BETWEEN 1 AND 240),
    sensitivity text NOT NULL DEFAULT 'standard' CHECK (sensitivity IN ('standard','private','restricted')),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, entity_type, key)
);
ALTER TABLE client_field_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_field_definitions FORCE ROW LEVEL SECURITY;
CREATE POLICY cfd_tenant_scope ON client_field_definitions
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON client_field_definitions TO salon_app;
CREATE INDEX cfd_tenant_entity_idx ON client_field_definitions (tenant_id, entity_type);

-- Custom field values (typed, validated)
CREATE TABLE client_field_values (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    definition_id uuid NOT NULL,
    entity_type text NOT NULL CHECK (entity_type IN ('client')),
    entity_id uuid NOT NULL,
    value_text text,
    value_number numeric,
    value_date date,
    value_boolean boolean,
    version integer NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, definition_id, entity_type, entity_id),
    FOREIGN KEY (tenant_id, definition_id) REFERENCES client_field_definitions (tenant_id, id) ON DELETE CASCADE
);
ALTER TABLE client_field_values ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_field_values FORCE ROW LEVEL SECURITY;
CREATE POLICY cfv_tenant_scope ON client_field_values
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON client_field_values TO salon_app;
CREATE INDEX cfv_tenant_entity_idx ON client_field_values (tenant_id, entity_type, entity_id);

-- Visit notes (structured, with visibility)
CREATE TABLE visit_notes (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL,
    appointment_id uuid,
    author_issuer text NOT NULL,
    author_subject text NOT NULL,
    class text NOT NULL DEFAULT 'general' CHECK (class IN ('general','clinical','allergy','preference')),
    visibility text NOT NULL DEFAULT 'standard' CHECK (visibility IN ('standard','private')),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 8000),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id) ON DELETE CASCADE
    -- appointment_id FK added in TASK-020 when appointments table exists
);
ALTER TABLE visit_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE visit_notes FORCE ROW LEVEL SECURITY;
CREATE POLICY vn_tenant_scope ON visit_notes
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON visit_notes TO salon_app;
CREATE INDEX vn_tenant_client_idx ON visit_notes (tenant_id, client_id, created_at DESC);

-- Service preferences (colour formula, stylist preference, etc.)
CREATE TABLE client_preferences (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL,
    preference_type text NOT NULL CHECK (preference_type IN (
        'colour_formula', 'stylist_preference', 'product', 'rebooking_interval',
        'product_allergy', 'sensitivity_note', 'communication_preference'
    )),
    service_id uuid,
    staff_id uuid,
    location_id uuid,
    value_json jsonb NOT NULL,
    source text NOT NULL DEFAULT 'staff' CHECK (source IN ('staff','client','import')),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id) ON DELETE CASCADE
    -- service_id, staff_id, location_id FKs added in TASK-015, TASK-016
);
ALTER TABLE client_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_preferences FORCE ROW LEVEL SECURITY;
CREATE POLICY cp_tenant_scope ON client_preferences
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON client_preferences TO salon_app;
CREATE INDEX cp_tenant_client_idx ON client_preferences (tenant_id, client_id, preference_type);