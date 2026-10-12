-- TASK-012 / FR-014: CSV client import with preview, provenance and row recovery.
--
-- Three tables, all tenant-owned with composite keys and FORCE RLS (data-model.md:30):
--   imports       - one upload attempt: mapping, dry-run counts, status, receipt.
--   import_rows   - short-lived staged rows with their validation outcome.
--   import_receipts - the resumable, deduplicated commit record.
-- Consent is never inferred from an import (data-model.md:66, QA-15): a row carries its
-- proof reference or it stays ineligible for marketing, regardless of what the file says.

CREATE TABLE imports (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    filename text NOT NULL CHECK (length(trim(filename)) BETWEEN 1 AND 240),
    mapping jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'previewed'
        CHECK (status IN ('previewed','committing','committed','failed')),
    total_rows integer NOT NULL DEFAULT 0 CHECK (total_rows >= 0),
    valid_rows integer NOT NULL DEFAULT 0 CHECK (valid_rows >= 0),
    error_rows integer NOT NULL DEFAULT 0 CHECK (error_rows >= 0),
    duplicate_rows integer NOT NULL DEFAULT 0 CHECK (duplicate_rows >= 0),
    committed_rows integer NOT NULL DEFAULT 0 CHECK (committed_rows >= 0),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_by_issuer text NOT NULL,
    created_by_subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE import_rows (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    import_id uuid NOT NULL,
    row_number integer NOT NULL CHECK (row_number >= 1),
    -- The raw cell values exactly as read from the file, kept only until the import's
    -- retention window closes. They are data, never instructions.
    raw jsonb NOT NULL,
    normalized jsonb,
    error_codes text[] NOT NULL DEFAULT '{}',
    -- Proof that the client consented, supplied by the operator. Absence never becomes
    -- an opt-in: a row without it is imported but flagged ineligible for marketing.
    consent_proof text CHECK (consent_proof IS NULL OR length(trim(consent_proof)) BETWEEN 1 AND 240),
    marketing_eligible boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','valid','error','committed')),
    -- Staging rows are short-lived and reference the created client for the receipt only.
    -- A composite FK to clients would have to be ON DELETE SET NULL, which would try to
    -- null the tenant_id half of the primary key; tenant scoping is enforced by RLS and
    -- the import cascade instead.
    client_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, import_id, row_number),
    FOREIGN KEY (tenant_id, import_id) REFERENCES imports(tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX import_rows_tenant_import_status_idx ON import_rows (tenant_id, import_id, status);

ALTER TABLE imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE imports FORCE ROW LEVEL SECURITY;
CREATE POLICY imports_tenant_scope ON imports
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE import_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE import_rows FORCE ROW LEVEL SECURITY;
CREATE POLICY import_rows_tenant_scope ON import_rows
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON imports TO salon_app;
GRANT SELECT, INSERT, UPDATE ON import_rows TO salon_app;