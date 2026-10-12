-- TASK-014 / FR-016: verified export, correction and deletion of client data.
--
-- Three tables, all tenant-owned with FORCE RLS (data-model.md:30, :64):
--   privacy_requests     - one verified request: type, verification, deadline, status.
--   retention_holds      - a legal/business reason a subject must NOT be deleted yet.
--   deletion_tombstones  - a non-identifying receipt that survives a restore, so a
--                          restored backup cannot resurrect a deleted client or a revoked
--                          consent (operations.md:71, :73).
-- Nothing here stores the exported payload: an export is produced on demand and not kept.

CREATE TABLE privacy_requests (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL,
    request_type text NOT NULL CHECK (request_type IN ('access','export','correction','deletion')),
    -- How the requester's authority to act for this client was established. A deletion
    -- without a recorded verification is not a deletion anyone can defend.
    verification text NOT NULL CHECK (length(trim(verification)) BETWEEN 1 AND 240),
    status text NOT NULL DEFAULT 'received'
        CHECK (status IN ('received','in_progress','completed','refused')),
    refusal_reason text,
    deadline date,
    requested_by_issuer text NOT NULL,
    requested_by_subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    CHECK ((status = 'refused') = (refusal_reason IS NOT NULL))
);

CREATE TABLE retention_holds (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL,
    -- Why the record is held, who set it, and when it may be reconsidered.
    basis text NOT NULL CHECK (length(trim(basis)) BETWEEN 1 AND 240),
    held_by_issuer text NOT NULL,
    held_by_subject text NOT NULL,
    released_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE deletion_tombstones (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    -- The subject hash, not the id: a tombstone must not itself identify anyone, but it
    -- must still match the deleted subject when replayed against a restored backup.
    subject_hash text NOT NULL CHECK (length(subject_hash) BETWEEN 16 AND 128),
    resource_type text NOT NULL,
    -- When the deletion happened, so a restore can replay tombstones newer than its snapshot.
    deleted_at timestamptz NOT NULL DEFAULT now(),
    replay_after_restore boolean NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id, id)
);

ALTER TABLE privacy_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE privacy_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY privacy_requests_tenant_scope ON privacy_requests
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE retention_holds ENABLE ROW LEVEL SECURITY;
ALTER TABLE retention_holds FORCE ROW LEVEL SECURITY;
CREATE POLICY retention_holds_tenant_scope ON retention_holds
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE deletion_tombstones ENABLE ROW LEVEL SECURITY;
ALTER TABLE deletion_tombstones FORCE ROW LEVEL SECURITY;
CREATE POLICY deletion_tombstones_tenant_scope ON deletion_tombstones
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON privacy_requests, retention_holds, deletion_tombstones TO salon_app;

CREATE INDEX privacy_requests_tenant_client_idx ON privacy_requests (tenant_id, client_id);
CREATE INDEX retention_holds_active_idx ON retention_holds (tenant_id, client_id) WHERE released_at IS NULL;
CREATE INDEX deletion_tombstones_subject_idx ON deletion_tombstones (tenant_id, subject_hash);