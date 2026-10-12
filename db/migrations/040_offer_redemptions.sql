-- TASK-040 / FR-056: offer redemptions and attributed outcomes.
--
--   offer_redemptions - one row per attributed booking: the campaign, client,
--                       appointment and (when given) code that earned it, plus
--                       the send it came from. One appointment attributes at
--                       most once (partial unique): the first matching token
--                       wins, nothing is ever inferred, and revenue is never
--                       double-counted across campaigns. Receipt linkage is a
--                       read-time join (appointment -> redemption -> receipts),
--                       so recorded money stays in its own ledger.
--
-- Attribution window: fourteen days from send to booking (ATTRIBUTION_DAYS).
-- Older tokens do not attribute; the booking stands on its own.

CREATE TABLE offer_redemptions (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    campaign_id uuid NOT NULL,
    client_id uuid NOT NULL,
    appointment_id uuid,
    communication_id uuid,
    code text,
    redeemed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, campaign_id) REFERENCES offer_campaigns(tenant_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE
);

-- One appointment, one attribution. NULL appointments (code-only redemptions
-- with no booking yet) never collide with each other.
CREATE UNIQUE INDEX offer_redemptions_appointment_uniq
    ON offer_redemptions (tenant_id, appointment_id)
    WHERE appointment_id IS NOT NULL;

ALTER TABLE offer_redemptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE offer_redemptions FORCE ROW LEVEL SECURITY;
CREATE POLICY offer_redemptions_tenant_scope ON offer_redemptions
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT ON offer_redemptions TO salon_app;

CREATE INDEX offer_redemptions_tenant_campaign_idx
    ON offer_redemptions (tenant_id, campaign_id);
