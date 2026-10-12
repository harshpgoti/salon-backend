-- TASK-038 / FR-053: audience segments over consenting clients.
--
--   client_tags    - free-form tags per client (lowercased, unique per
--                    client). Written through the segment seam with the same
--                    profile-write permission as other client edits.
--   client_segments- named audiences: a versioned JSON filter AST over
--                    history/service/tags/location/spend. The AST is a fixed
--                    operator set (and/or/not, service, location, tag,
--                    min/max spend, inactive days) — anything else is rejected
--                    at write time, so an invalid AST can never silently widen
--                    to all clients. Audiences resolve at read time (never
--                    stored per-client), with marketing consent and
--                    suppression applied then, so consent changes take effect
--                    without rebuilding anything.

CREATE TABLE client_tags (
    tenant_id uuid NOT NULL,
    client_id uuid NOT NULL,
    tag text NOT NULL CHECK (tag ~ '^[a-z0-9][a-z0-9 _-]{0,39}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, client_id, tag),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE client_segments (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    definition jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, name),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

ALTER TABLE client_tags ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_tags FORCE ROW LEVEL SECURITY;
CREATE POLICY client_tags_tenant_scope ON client_tags
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE client_segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_segments FORCE ROW LEVEL SECURITY;
CREATE POLICY client_segments_tenant_scope ON client_segments
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON client_tags, client_segments TO salon_app;

CREATE INDEX client_tags_tenant_tag_idx ON client_tags (tenant_id, tag);
