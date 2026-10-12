-- TASK-030 / FR-043, FR-047: signed callbacks, retries and dead letters.
--
--   provider_webhook_secrets - one HMAC secret per tenant/channel. Callbacks
--                              authenticate by signature, never by staff session.
--   provider_callbacks       - every provider delivery event, deduplicated by
--                              (communication, provider event id) so duplicate
--                              or out-of-order redelivery converges safely.
--   dispatch_attempts        - one row per send attempt with its not-before
--                              backoff timestamp; the worker claims due rows
--                              with SELECT ... FOR UPDATE SKIP LOCKED.
--   dead_letters             - communications that exhausted retries or arrived
--                              for an unknown reference, with their reason.
--
-- communications.state gains retry_scheduled (waiting for backoff) and sending
-- (claimed by exactly one worker). A crashed worker's 'sending' rows are
-- reclaimed only after a staleness timeout, so a retry never blindly sends
-- twice while the first attempt may still be in flight.

CREATE TABLE provider_webhook_secrets (
    tenant_id uuid NOT NULL,
    channel text NOT NULL CHECK (channel IN ('email', 'sms', 'whatsapp')),
    secret text NOT NULL CHECK (length(secret) >= 32),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, channel),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE provider_callbacks (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    communication_id uuid,
    provider_ref text,
    -- accepted = provider took the message (NOT delivery); delivered/read =
    -- provider-supplied evidence; failed = provider-side failure evidence.
    event text NOT NULL CHECK (event IN ('accepted', 'delivered', 'read', 'failed')),
    provider_event_id text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, communication_id, provider_event_id),
    FOREIGN KEY (tenant_id, communication_id)
        REFERENCES communications(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE dispatch_attempts (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    communication_id uuid NOT NULL,
    attempt integer NOT NULL CHECK (attempt >= 1),
    -- queued          = scheduled, not yet due or unclaimed
    -- sending         = claimed by one worker (see reclaim rule below)
    -- succeeded       = adapter accepted without raising
    -- retry_scheduled = adapter raised; a later attempt row is queued
    -- superseded      = a newer attempt replaced this queued one; never claimed
    -- exhausted       = attempt ceiling hit; moved to dead letters
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'sending', 'succeeded', 'retry_scheduled', 'superseded',
                         'exhausted')),
    not_before timestamptz NOT NULL DEFAULT now(),
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, communication_id, attempt),
    FOREIGN KEY (tenant_id, communication_id)
        REFERENCES communications(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE dead_letters (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    communication_id uuid,
    provider_ref text,
    -- exhausted_retries = attempt ceiling hit after backoff
    -- unknown_reference = callback for no known communication
    reason text NOT NULL CHECK (reason IN ('exhausted_retries', 'unknown_reference')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

-- One open dead letter per communication, and one per unknown provider
-- reference: duplicates and redeliveries converge instead of piling up.
-- (Partial uniques: NULL communication/provider refs never collide.)
CREATE UNIQUE INDEX dead_letters_open_comm_uniq ON dead_letters (tenant_id, communication_id)
    WHERE communication_id IS NOT NULL AND resolved_at IS NULL;
CREATE UNIQUE INDEX dead_letters_open_ref_uniq ON dead_letters (tenant_id, provider_ref, reason)
    WHERE communication_id IS NULL AND provider_ref IS NOT NULL AND resolved_at IS NULL;

ALTER TABLE communications DROP CONSTRAINT communications_state_check;
ALTER TABLE communications ADD CONSTRAINT communications_state_check
    CHECK (state IN ('recorded', 'retry_scheduled', 'sending', 'dispatched',
                     'delivered', 'failed', 'skipped_stale', 'skipped_expired',
                     'suppressed'));

ALTER TABLE provider_webhook_secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_webhook_secrets FORCE ROW LEVEL SECURITY;
CREATE POLICY provider_webhook_secrets_tenant_scope ON provider_webhook_secrets
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE provider_callbacks ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_callbacks FORCE ROW LEVEL SECURITY;
CREATE POLICY provider_callbacks_tenant_scope ON provider_callbacks
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE dispatch_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE dispatch_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY dispatch_attempts_tenant_scope ON dispatch_attempts
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE dead_letters ENABLE ROW LEVEL SECURITY;
ALTER TABLE dead_letters FORCE ROW LEVEL SECURITY;
CREATE POLICY dead_letters_tenant_scope ON dead_letters
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON
    provider_webhook_secrets, provider_callbacks, dispatch_attempts, dead_letters
    TO salon_app;

CREATE INDEX provider_callbacks_tenant_comm_idx
    ON provider_callbacks (tenant_id, communication_id);
CREATE INDEX dispatch_attempts_due_idx ON dispatch_attempts (tenant_id, not_before)
    WHERE state = 'queued';
CREATE INDEX dead_letters_tenant_open_idx ON dead_letters (tenant_id)
    WHERE resolved_at IS NULL;
