-- TASK-039 / FR-054, FR-055, FR-057: budget-bounded offer campaigns.
--
--   offer_campaigns - draft -> scheduled -> sending -> completed / partial /
--                     cancelled / failed. Approval freezes the audience
--                     snapshot plus the template, budget and schedule it was
--                     approved with; any later edit to those fields reopens
--                     the draft (re-approval required). Sends stop at the
--                     approved budget; increases need a new approval.
--   campaign_sends  - one row per snapshot client: queued, sent, or skipped
--                     with its reason. Unique per campaign+client so a worker
--                     rerun never double-sends.
--
-- communications learn the 'campaign' source for attribution-linked sends.

CREATE TABLE offer_campaigns (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    segment_id uuid NOT NULL,
    channel text NOT NULL CHECK (channel IN ('email', 'sms', 'whatsapp')),
    -- Offer terms shown to the client; code is the redemption hook (TASK-040).
    offer_title text NOT NULL CHECK (length(offer_title) BETWEEN 1 AND 160),
    offer_terms text NOT NULL DEFAULT '',
    offer_code text,
    offer_expires_at timestamptz,
    template_subject text,
    template_body text NOT NULL CHECK (length(template_body) BETWEEN 1 AND 2000),
    -- WhatsApp campaigns must name an approved template (validated at
    -- approval); other channels leave this empty.
    whatsapp_template_id uuid,
    -- Approved CAD minor-unit ceiling for this campaign's sends.
    budget_minor integer NOT NULL CHECK (budget_minor >= 0),
    scheduled_at timestamptz,
    -- draft      = editable; nothing sends
    -- scheduled  = approved and waiting for its time
    -- sending    = worker is draining the queue
    -- completed  = every snapshot client sent
    -- partial    = queue drained with skips (suppressed, capped, expired)
    -- cancelled  = owner stopped future sends; sent ones stay sent
    -- failed     = worker error before drain finished
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'scheduled', 'sending', 'completed',
                          'partial', 'cancelled', 'failed')),
    approved_by_issuer text,
    approved_by_subject text,
    approved_at timestamptz,
    -- Frozen at approval: the eligible client ids plus the locked
    -- template/budget/schedule. Dispatch revalidates each client live.
    audience_snapshot jsonb NOT NULL DEFAULT '[]',
    snapshot_template_body text,
    snapshot_budget_minor integer,
    snapshot_scheduled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, segment_id) REFERENCES client_segments(tenant_id, id)
        ON DELETE RESTRICT
);

CREATE TABLE campaign_sends (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    campaign_id uuid NOT NULL,
    client_id uuid NOT NULL,
    communication_id uuid,
    -- queued -> sent | skipped_<reason>. One row per client, ever.
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'sent', 'skipped_consent', 'skipped_suppressed',
                         'skipped_unreachable', 'skipped_budget', 'skipped_frequency',
                         'skipped_cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, campaign_id, client_id),
    FOREIGN KEY (tenant_id, campaign_id) REFERENCES offer_campaigns(tenant_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, communication_id)
        REFERENCES communications(tenant_id, id) ON DELETE SET NULL
);

ALTER TABLE communications DROP CONSTRAINT communications_source_type_check;
ALTER TABLE communications ADD CONSTRAINT communications_source_type_check
    CHECK (source_type IN ('appointment', 'location_broadcast', 'rebooking_cycle',
                           'campaign'));

ALTER TABLE offer_campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE offer_campaigns FORCE ROW LEVEL SECURITY;
CREATE POLICY offer_campaigns_tenant_scope ON offer_campaigns
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE campaign_sends ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign_sends FORCE ROW LEVEL SECURITY;
CREATE POLICY campaign_sends_tenant_scope ON campaign_sends
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON offer_campaigns, campaign_sends TO salon_app;

CREATE INDEX campaign_sends_tenant_campaign_idx
    ON campaign_sends (tenant_id, campaign_id) WHERE state = 'queued';
