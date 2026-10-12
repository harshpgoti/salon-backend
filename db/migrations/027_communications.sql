-- TASK-027 / FR-039, FR-042: durable communication outbox for booking events.
--
-- Two tables, tenant-owned with FORCE RLS:
--   message_templates - versioned templates keyed by channel, purpose and locale.
--   communications    - one row per outbound message, referencing the source entity
--                       (appointment) and the entity version it was created for.
--
-- A communication is recorded in the same transaction as the booking change, so a
-- rolled-back booking never leaves an orphan message. The actual provider send is a
-- separate dispatch step (AC-2: provider failure leaves booking intact). The
-- `expected_entity_version` column lets the dispatcher skip stale messages whose
-- appointment has since been rescheduled or cancelled (FR-042).
--
-- `expires_at` marks reminders that are no longer useful once the appointment
-- start has passed; the dispatcher skips them and leaves a reason (FR-042).

CREATE TABLE message_templates (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    channel text NOT NULL CHECK (channel IN ('email', 'sms', 'whatsapp')),
    purpose text NOT NULL CHECK (purpose IN (
        'booking_confirmation', 'booking_rescheduled', 'booking_cancelled',
        'appointment_reminder', 'marketing')),
    locale text NOT NULL DEFAULT 'en-CA',
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    subject text,
    body text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, channel, purpose, locale, version),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE communications (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL,
    -- Snapshot of the resolved contact at record time, so the message body is
    -- reproducible even if the client later edits their contact.
    contact_value text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('email', 'sms', 'whatsapp')),
    purpose text NOT NULL,
    template_id uuid,
    template_version integer,
    -- The source entity this message is about, and the version it was created for.
    source_type text NOT NULL CHECK (source_type IN ('appointment')),
    source_id uuid NOT NULL,
    expected_entity_version integer NOT NULL,
    -- recorded  = row written in the booking transaction, not yet dispatched
    -- dispatched= handed to the provider adapter
    -- delivered = provider confirmed (only when provider supplies evidence)
    -- failed    = provider returned a classified error
    -- skipped_stale  = appointment version changed before dispatch
    -- skipped_expired= reminder window passed before dispatch
    -- suppressed= consent or opt-out blocked the send
    state text NOT NULL DEFAULT 'recorded'
        CHECK (state IN ('recorded', 'dispatched', 'delivered', 'failed',
                         'skipped_stale', 'skipped_expired', 'suppressed')),
    skip_reason text,
    scheduled_at timestamptz,
    expires_at timestamptz,
    dispatched_at timestamptz,
    provider_ref text,
    -- Business dedupe key so a replayed booking command does not double-send.
    dedupe_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, dedupe_key),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE message_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE message_templates FORCE ROW LEVEL SECURITY;
CREATE POLICY message_templates_tenant_scope ON message_templates
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE communications ENABLE ROW LEVEL SECURITY;
ALTER TABLE communications FORCE ROW LEVEL SECURITY;
CREATE POLICY communications_tenant_scope ON communications
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON message_templates, communications TO salon_app;

CREATE INDEX communications_tenant_source_idx ON communications (tenant_id, source_type, source_id);
CREATE INDEX communications_tenant_state_idx ON communications (tenant_id, state);
CREATE INDEX communications_due_idx ON communications (tenant_id, scheduled_at)
    WHERE state = 'recorded' AND scheduled_at IS NOT NULL;