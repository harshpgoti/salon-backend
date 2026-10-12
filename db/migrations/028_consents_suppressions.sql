-- TASK-028 / FR-040, FR-041: consent evidence and opt-out suppression.
--
-- Two tables, tenant-owned with FORCE RLS:
--   consents     - one row per client contact/channel/purpose grant or revocation,
--                  carrying proof (source, wording version, reference) so a later
--                  audit can show why a message was allowed.
--   suppressions - one row per opted-out contact value/channel. A suppression is
--                  keyed by the normalized contact value (not the client row) so an
--                  unsubscribe/STOP keeps blocking sends even if the client record
--                  is edited, merged or re-imported afterwards.
--
-- AC-1: booking and import flows may record consent with source 'booking' or
-- 'import' for transactional purposes only; they never create a marketing grant
-- silently. Marketing sends require an explicit 'granted' consent row.
--
-- AC-2: the dispatcher re-checks suppressions immediately before each send. A
-- suppression written after a message was recorded (the unsubscribe/STOP race)
-- moves that message to state 'suppressed' instead of dispatching it.

CREATE TABLE consents (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL,
    contact_id uuid NOT NULL,
    channel text NOT NULL CHECK (channel IN ('email', 'sms', 'whatsapp')),
    purpose text NOT NULL,
    source text NOT NULL CHECK (source IN (
        'booking', 'import', 'staff', 'client_self', 'marketing_form')),
    wording_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('granted', 'revoked')),
    proof_reference text,
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    expires_at timestamptz,
    lawful_basis_review text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, contact_id) REFERENCES client_contacts(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE suppressions (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL,
    -- Normalized contact value (lower-cased email, E.164 phone). Keyed by value
    -- so the opt-out survives client edits, merges and re-imports.
    contact_value text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('email', 'sms', 'whatsapp')),
    reason text NOT NULL CHECK (reason IN (
        'unsubscribe', 'stop', 'bounce', 'complaint', 'channel_unavailable')),
    source text NOT NULL DEFAULT 'client_self',
    suppressed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, contact_value, channel),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE consents FORCE ROW LEVEL SECURITY;
CREATE POLICY consents_tenant_scope ON consents
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE suppressions ENABLE ROW LEVEL SECURITY;
ALTER TABLE suppressions FORCE ROW LEVEL SECURITY;
CREATE POLICY suppressions_tenant_scope ON suppressions
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON consents, suppressions TO salon_app;

CREATE INDEX consents_tenant_client_idx ON consents (tenant_id, client_id, channel, purpose);
CREATE INDEX suppressions_tenant_contact_idx ON suppressions (tenant_id, contact_value, channel);
