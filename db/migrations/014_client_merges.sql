-- TASK-013 / FR-015: controlled duplicate merge with an explicit, reversible record.
--
-- One table. A merge is never automatic: a shared phone or email produces a *candidate*
-- (010_client_contacts.sql), and only an operator-approved request writes here. The
-- manifest records what moved and where, so a merge can be inspected and, where safe,
-- reversed rather than being a one-way destructive act.

CREATE TABLE merge_records (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    -- The client that remains, and the one that was folded into it.
    survivor_client_id uuid NOT NULL,
    merged_client_id uuid NOT NULL,
    -- Every reassigned row, by table and id, kept as a manifest rather than a diff log.
    manifest jsonb NOT NULL DEFAULT '{}',
    conflicts jsonb NOT NULL DEFAULT '[]',
    acknowledged_conflicts boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'applied' CHECK (status IN ('applied','reversed')),
    performed_by_issuer text NOT NULL,
    performed_by_subject text NOT NULL,
    performed_at timestamptz NOT NULL DEFAULT now(),
    reversed_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    CHECK (survivor_client_id <> merged_client_id),
    CHECK ((status = 'reversed') = (reversed_at IS NOT NULL))
);

ALTER TABLE merge_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE merge_records FORCE ROW LEVEL SECURITY;
CREATE POLICY merge_records_tenant_scope ON merge_records
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON merge_records TO salon_app;

CREATE INDEX merge_records_tenant_survivor_idx ON merge_records (tenant_id, survivor_client_id);
CREATE INDEX merge_records_tenant_merged_idx ON merge_records (tenant_id, merged_client_id);