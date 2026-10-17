-- Keep a durable, attributable record of the decision to release a hold.
-- This migration performs a table-wide RLS backfill. It must run as the separately
-- controlled migration role with BYPASSRLS (or as superuser), never as salon_app.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles
                   WHERE rolname=current_user AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'migration 071 requires the controlled BYPASSRLS migration role';
    END IF;
END $$;

ALTER TABLE retention_holds
    ADD COLUMN IF NOT EXISTS released_by_issuer text,
    ADD COLUMN IF NOT EXISTS released_by_subject text,
    ADD COLUMN IF NOT EXISTS release_reason text;

ALTER TABLE privacy_requests ALTER COLUMN client_id DROP NOT NULL;
ALTER TABLE privacy_requests ALTER COLUMN verification DROP NOT NULL;
ALTER TABLE privacy_requests ADD COLUMN IF NOT EXISTS subject_hash text;
ALTER TABLE privacy_requests ADD COLUMN IF NOT EXISTS deletion_idempotency_key text;

CREATE TABLE IF NOT EXISTS privacy_file_purge_jobs (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    privacy_request_id uuid NOT NULL,
    storage_key text NOT NULL CHECK (storage_key ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, privacy_request_id, storage_key),
    FOREIGN KEY (tenant_id, privacy_request_id)
        REFERENCES privacy_requests(tenant_id, id) ON DELETE CASCADE,
    CHECK ((status='completed') = (completed_at IS NOT NULL))
);
ALTER TABLE privacy_file_purge_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE privacy_file_purge_jobs FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
    CREATE POLICY privacy_file_purge_jobs_tenant_scope ON privacy_file_purge_jobs
        USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
GRANT SELECT, INSERT, UPDATE ON privacy_file_purge_jobs TO salon_app;
CREATE INDEX IF NOT EXISTS privacy_file_purge_pending_idx
    ON privacy_file_purge_jobs (tenant_id, privacy_request_id, created_at)
    WHERE status='pending';

-- Older releases recorded only released_at. Preserve those rows and make the
-- missing historical attribution explicit before enforcing the new audit shape.
UPDATE retention_holds SET
    released_by_issuer=COALESCE(NULLIF(trim(released_by_issuer), ''), 'legacy:unknown'),
    released_by_subject=COALESCE(NULLIF(trim(released_by_subject), ''), 'legacy:unknown'),
    release_reason=COALESCE(NULLIF(trim(release_reason), ''),
        'Historical release; actor and reason were not recorded before migration 071')
WHERE released_at IS NOT NULL;

DO $$ BEGIN
    ALTER TABLE retention_holds ADD CONSTRAINT retention_hold_release_audit_check
    CHECK ((released_at IS NULL AND released_by_issuer IS NULL AND released_by_subject IS NULL
            AND release_reason IS NULL)
        OR (released_at IS NOT NULL AND released_by_issuer IS NOT NULL
            AND released_by_subject IS NOT NULL AND length(trim(release_reason)) BETWEEN 1 AND 240));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
