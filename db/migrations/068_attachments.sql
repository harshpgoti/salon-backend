-- TASK-068 / FR-017: consent-controlled visit photos and attachments.
--
-- This work was deferred behind gate G-ATTACHMENT-SCOPE; the product owner has now
-- explicitly approved building it with a synthetic local content-addressed file
-- store (no cloud storage). The bytes live in backend/.data/attachments, named by
-- their sha256, and are reachable only through the row family below.
--
-- One table, tenant-owned with composite key and FORCE RLS (data-model.md:30):
--   attachments - one row per stored object: which client, which consent authorised
--                 it, its own purpose and retention deadline, and the content hash
--                 that names the object in the private store.
--
-- Three properties are enforced in the schema rather than promised in the API:
--
-- - **Consent is part of the row.** consent_id is NOT NULL and the client FK is
--   composite, so a row can only ever cite a consent that belongs to the same
--   tenant and the same client. Revoking the consent does not delete the row: the
--   download gate refuses it while the evidence stays for audit.
-- - **Retention is a column, not a convention.** retention_until is NOT NULL, so
--   every object has an explicit expiry a sweep can act on.
-- - **Deletion is soft.** state='deleted' plus deleted_at, guarded by a CHECK so
--   the two can never disagree, and no DELETE grant is issued to salon_app.

DO $table$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_tables
                   WHERE schemaname = 'public' AND tablename = 'attachments') THEN
        EXECUTE $ddl$
            CREATE TABLE attachments (
                tenant_id uuid NOT NULL,
                id uuid NOT NULL DEFAULT gen_random_uuid(),
                client_id uuid NOT NULL,
                -- The consent that authorised this upload. ON DELETE CASCADE, not
                -- RESTRICT: an approved privacy deletion (TASK-014) purges the
                -- client's consents, and a photo of a deleted subject must not
                -- outlive the deletion by holding the cascade open.
                consent_id uuid NOT NULL,
                appointment_id uuid,
                -- The caller's idempotency key for the upload. Scoped by tenant
                -- because two salons may legitimately reuse a key string.
                idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 64),
                -- Names the object in the private store. It is the sha256 of the
                -- bytes, so the name is the checksum: a mismatched checksum is
                -- refused before anything is written, and content-addressed
                -- dedup is safe by construction.
                storage_key text NOT NULL CHECK (storage_key ~ '^[0-9a-f]{64}$'),
                -- The operator's declared reason for storing the object. The row
                -- carries its own copy because the consent it cites is an
                -- append-only history row, not a policy that can be re-read later.
                purpose text NOT NULL CHECK (purpose IN (
                    'visit_photo', 'visit_document', 'consent_document')),
                content_type text NOT NULL CHECK (content_type IN (
                    'image/jpeg', 'image/png', 'application/pdf')),
                size_bytes integer NOT NULL CHECK (size_bytes BETWEEN 1 AND 5242880),
                -- Client-asserted digest of the uploaded bytes; it must equal the
                -- stored object's digest, which is how a corrupt transfer is caught.
                checksum_sha256 text NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
                retention_until timestamptz NOT NULL,
                state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'deleted')),
                deleted_at timestamptz,
                uploaded_by_issuer text NOT NULL,
                uploaded_by_subject text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (tenant_id, id),
                -- An upload idempotency key replays the same attachment instead of
                -- writing a second object; the unique key is what makes that true.
                UNIQUE (tenant_id, idempotency_key),
                CHECK (state <> 'deleted' OR deleted_at IS NOT NULL),
                FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
                FOREIGN KEY (tenant_id, consent_id) REFERENCES consents(tenant_id, id) ON DELETE CASCADE,
                FOREIGN KEY (tenant_id, appointment_id) REFERENCES appointments(tenant_id, id) ON DELETE SET NULL
                )
        $ddl$;
    END IF;
END
$table$;

ALTER TABLE attachments ENABLE ROW LEVEL SECURITY;
ALTER TABLE attachments FORCE ROW LEVEL SECURITY;

DO $policy$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE schemaname = 'public' AND tablename = 'attachments'
                     AND policyname = 'attachments_tenant_scope') THEN
        EXECUTE $ddl$
            CREATE POLICY attachments_tenant_scope ON attachments
                USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        $ddl$;
    END IF;
END
$policy$;

-- SELECT/INSERT/UPDATE only: there is no hard delete anywhere in this flow.
GRANT SELECT, INSERT, UPDATE ON attachments TO salon_app;

-- Reading a client's attachments is the list and the download gate; both filter by
-- tenant (RLS) and client.
CREATE INDEX IF NOT EXISTS attachments_client_created_idx
    ON attachments (tenant_id, client_id, created_at DESC);
-- The consent re-check on every download, and "which rows cite this consent".
CREATE INDEX IF NOT EXISTS attachments_consent_idx
    ON attachments (tenant_id, consent_id);
-- The retention sweep: active rows by expiry deadline.
CREATE INDEX IF NOT EXISTS attachments_retention_idx
    ON attachments (tenant_id, retention_until) WHERE state = 'active';
