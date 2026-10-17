-- TASK-040 / FR-056: enforce one recorded attribution source per appointment,
-- whether the source was an offer campaign or an automated rebooking prompt.
CREATE TABLE public.appointment_attributions (
    tenant_id uuid NOT NULL,
    appointment_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('campaign', 'rebooking')),
    campaign_id uuid,
    cycle_id uuid,
    communication_id uuid,
    attributed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, appointment_id),
    CHECK ((kind = 'campaign' AND campaign_id IS NOT NULL AND cycle_id IS NULL)
        OR (kind = 'rebooking' AND cycle_id IS NOT NULL AND campaign_id IS NULL)),
    FOREIGN KEY (tenant_id, appointment_id)
        REFERENCES public.appointments(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, campaign_id)
        REFERENCES public.offer_campaigns(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, cycle_id)
        REFERENCES public.rebooking_cycles(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, communication_id)
        REFERENCES public.communications(tenant_id, id)
        ON DELETE SET NULL (communication_id)
);

-- Keep the first observed attribution when old records used separate campaign
-- and rebooking projections before this shared uniqueness ledger existed.
WITH candidates AS (
    SELECT r.tenant_id, r.appointment_id, 'campaign'::text AS kind,
           r.campaign_id, NULL::uuid AS cycle_id, r.communication_id,
           r.created_at AS attributed_at
    FROM public.offer_redemptions r
    WHERE r.appointment_id IS NOT NULL
    UNION ALL
    SELECT c.tenant_id, c.booked_appointment_id, 'rebooking'::text,
           NULL::uuid, c.id, c.attributed_communication_id, c.updated_at
    FROM public.rebooking_cycles c
    WHERE c.booked_appointment_id IS NOT NULL
      AND c.attributed_communication_id IS NOT NULL
), first_touch AS (
    SELECT DISTINCT ON (tenant_id, appointment_id) *
    FROM candidates
    ORDER BY tenant_id, appointment_id, attributed_at, kind
)
INSERT INTO public.appointment_attributions
    (tenant_id, appointment_id, kind, campaign_id, cycle_id,
     communication_id, attributed_at)
SELECT tenant_id, appointment_id, kind, campaign_id, cycle_id,
       communication_id, attributed_at
FROM first_touch
ON CONFLICT (tenant_id, appointment_id) DO NOTHING;

ALTER TABLE public.appointment_attributions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.appointment_attributions FORCE ROW LEVEL SECURITY;
CREATE POLICY appointment_attributions_tenant_scope
    ON public.appointment_attributions
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT ON public.appointment_attributions TO salon_app;

CREATE INDEX appointment_attributions_tenant_campaign_idx
    ON public.appointment_attributions (tenant_id, campaign_id)
    WHERE campaign_id IS NOT NULL;
