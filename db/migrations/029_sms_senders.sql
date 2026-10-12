-- TASK-029 / FR-044: SMS sender registration/verification and delivery workflow.
--
-- Three tables, all tenant-owned with FORCE RLS:
--   sms_senders     - one row per salon sender identity (E.164 number or short
--                     code). The sender identity is globally unique so a number
--                     can never silently serve two salons (no cross-salon
--                     pooling). Status moves pending -> verified -> suspended;
--                     only verified senders route SMS.
--   inbound_messages- every inbound SMS the (recording) provider delivers:
--                     STOP unsubscribes, HELP answers with help text, anything
--                     else is logged for staff follow-up (TASK-034 inbox).
--   message_costs   - one ledger row per dispatched communication: channel,
--                     segment count and CAD minor-unit cost, so spend is visible
--                     before the next campaign (FR-044/FR-047).
--
-- Inbound STOP may arrive for a number the salon never stored as a client
-- contact, so suppressions.client_id becomes nullable: the block is keyed by
-- contact value, the client link stays for audit when it resolves.

CREATE TABLE sms_senders (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    label text NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
    -- E.164 (+14165550132) or short code (12345). Globally unique: one number,
    -- one salon, never a shared pool.
    sender_identity text NOT NULL,
    channel text NOT NULL DEFAULT 'sms' CHECK (channel = 'sms'),
    -- pending   = registered, verification code issued, not yet proven
    -- verified  = code confirmed; eligible to route SMS
    -- suspended = owner/company suspended; must not route
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'verified', 'suspended')),
    verification_reference text NOT NULL,
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (sender_identity),
    -- Globally unique surrogate so inbound history can key the sender alone
    -- and survive its deletion (sender_id nulls, tenant row stays).
    UNIQUE (id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE inbound_messages (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    sender_id uuid,
    from_value text NOT NULL,
    channel text NOT NULL DEFAULT 'sms' CHECK (channel = 'sms'),
    keyword text NOT NULL CHECK (keyword IN ('stop', 'help', 'other')),
    -- suppressed = STOP created/ refreshed a suppression
    -- help_sent  = HELP answered with help text, no suppression
    -- logged     = anything else, kept for the staff inbox
    -- unresolved = sender identity unknown; no tenant action possible
    action text NOT NULL
        CHECK (action IN ('suppressed', 'help_sent', 'logged', 'unresolved')),
    received_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (sender_id) REFERENCES sms_senders(id) ON DELETE SET NULL
);

CREATE TABLE message_costs (
    tenant_id uuid NOT NULL,
    communication_id uuid NOT NULL,
    channel text NOT NULL CHECK (channel IN ('email', 'sms', 'whatsapp')),
    segments integer NOT NULL DEFAULT 1 CHECK (segments >= 1),
    cost_minor integer NOT NULL DEFAULT 0 CHECK (cost_minor >= 0),
    currency text NOT NULL DEFAULT 'CAD' CHECK (currency = 'CAD'),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, communication_id),
    FOREIGN KEY (tenant_id, communication_id)
        REFERENCES communications(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE suppressions ALTER COLUMN client_id DROP NOT NULL;

ALTER TABLE sms_senders ENABLE ROW LEVEL SECURITY;
ALTER TABLE sms_senders FORCE ROW LEVEL SECURITY;
CREATE POLICY sms_senders_tenant_scope ON sms_senders
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE inbound_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE inbound_messages FORCE ROW LEVEL SECURITY;
CREATE POLICY inbound_messages_tenant_scope ON inbound_messages
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE message_costs ENABLE ROW LEVEL SECURITY;
ALTER TABLE message_costs FORCE ROW LEVEL SECURITY;
CREATE POLICY message_costs_tenant_scope ON message_costs
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON sms_senders, inbound_messages, message_costs TO salon_app;

CREATE INDEX sms_senders_tenant_status_idx ON sms_senders (tenant_id, status);
CREATE INDEX inbound_messages_tenant_from_idx ON inbound_messages (tenant_id, from_value);
