-- TASK-031 / FR-045: WhatsApp ISV onboarding and approved template sends.
--
-- Sandbox only (DQ-006): no real vendor is called. The tables model the ISV
-- process so the product rules can be enforced and tested:
--   whatsapp_senders   - one business sender per salon (global phone UNIQUE,
--                        no pooling). Status draft -> submitted -> approved
--                        (or rejected/suspended). Approval carries the
--                        quality rating and messaging limit the ISV reports.
--   whatsapp_templates - named templates with category, locale and {{n}}
--                        parameters. Only approved templates may start a
--                        conversation outside the 24h window.
--
-- The 24h customer-service window derives from inbound/outbound timestamps,
-- so inbound_messages.channel admits whatsapp alongside sms now.

CREATE TABLE whatsapp_senders (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    business_name text NOT NULL CHECK (length(business_name) BETWEEN 1 AND 120),
    phone_number text NOT NULL,
    -- draft     = registered locally, not yet sent to the ISV
    -- submitted = sent to the ISV sandbox, awaiting decision
    -- approved  = ISV approved; eligible to send (quality permitting)
    -- rejected  = ISV refused; visible with its reason
    -- suspended = owner/company suspended; must not route
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'submitted', 'approved', 'rejected', 'suspended')),
    isv_reference text,
    quality_rating text CHECK (quality_rating IN ('green', 'yellow', 'red')),
    messaging_limit integer CHECK (messaging_limit IS NULL OR messaging_limit > 0),
    decision_note text,
    approved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (phone_number),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE whatsapp_templates (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (name ~ '^[a-z0-9_]{1,64}$'),
    -- Meta categories: utility (appointments), marketing (offers),
    -- authentication (codes). Marketing additionally needs consent (FR-040).
    category text NOT NULL CHECK (category IN ('utility', 'marketing', 'authentication')),
    locale text NOT NULL CHECK (locale ~ '^[a-z]{2}(_[A-Z]{2})?$'),
    -- draft -> submitted -> approved (or rejected). Only approved templates
    -- may open a conversation outside the 24h window.
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'submitted', 'approved', 'rejected')),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 1024),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, name, locale),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

ALTER TABLE inbound_messages DROP CONSTRAINT inbound_messages_channel_check;
ALTER TABLE inbound_messages ADD CONSTRAINT inbound_messages_channel_check
    CHECK (channel IN ('sms', 'whatsapp'));

ALTER TABLE whatsapp_senders ENABLE ROW LEVEL SECURITY;
ALTER TABLE whatsapp_senders FORCE ROW LEVEL SECURITY;
CREATE POLICY whatsapp_senders_tenant_scope ON whatsapp_senders
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE whatsapp_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE whatsapp_templates FORCE ROW LEVEL SECURITY;
CREATE POLICY whatsapp_templates_tenant_scope ON whatsapp_templates
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON whatsapp_senders, whatsapp_templates TO salon_app;

CREATE INDEX whatsapp_senders_tenant_status_idx ON whatsapp_senders (tenant_id, status);
CREATE INDEX whatsapp_templates_tenant_name_idx ON whatsapp_templates (tenant_id, name, locale);
CREATE INDEX inbound_messages_wa_window_idx ON inbound_messages (tenant_id, from_value, received_at)
    WHERE channel = 'whatsapp';
