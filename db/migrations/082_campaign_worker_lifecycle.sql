-- TASK-039 / FR-054, FR-055, FR-057: resumable, pauseable offer delivery.
ALTER TABLE public.offer_campaigns
    DROP CONSTRAINT offer_campaigns_status_check;
ALTER TABLE public.offer_campaigns
    ADD CONSTRAINT offer_campaigns_status_check
    CHECK (status IN ('draft', 'scheduled', 'sending', 'paused', 'completed',
                      'partial', 'cancelled', 'failed'));

ALTER TABLE public.offer_campaigns
    ADD COLUMN paused_from_status text,
    ADD COLUMN snapshot_template_subject text,
    ADD COLUMN snapshot_whatsapp_template_id uuid,
    ADD COLUMN snapshot_whatsapp_template_version integer,
    ADD COLUMN snapshot_whatsapp_template_name text,
    ADD COLUMN snapshot_whatsapp_template_category text,
    ADD COLUMN snapshot_whatsapp_template_locale text,
    ADD COLUMN snapshot_whatsapp_template_body text;

ALTER TABLE public.offer_campaigns
    ADD CONSTRAINT offer_campaigns_paused_from_status_check
    CHECK (paused_from_status IS NULL OR
           paused_from_status IN ('scheduled', 'sending', 'completed', 'partial')),
    ADD CONSTRAINT offer_campaigns_pause_state_check
    CHECK ((status = 'paused') = (paused_from_status IS NOT NULL));

ALTER TABLE public.communications
    ADD COLUMN template_subject_snapshot text,
    ADD COLUMN template_body_snapshot text;

ALTER TABLE public.campaign_sends
    DROP CONSTRAINT campaign_sends_state_check;
ALTER TABLE public.campaign_sends
    ADD CONSTRAINT campaign_sends_state_check
    CHECK (state IN ('queued', 'recorded', 'sent', 'skipped_consent',
                     'skipped_suppressed', 'skipped_unreachable', 'skipped_budget',
                     'skipped_frequency', 'skipped_cancelled'));

CREATE INDEX offer_campaigns_due_worker_idx
    ON public.offer_campaigns (snapshot_scheduled_at, tenant_id)
    WHERE status IN ('scheduled', 'sending');

-- Bounded tenant-ID-only discovery. Workers read campaign content only after
-- establishing forced tenant RLS context. Five-second page rotation prevents
-- due campaigns whose remaining clients are skipped from starving later tenants.
CREATE FUNCTION public.due_campaign_tenants(p_now timestamptz, p_limit integer)
RETURNS TABLE(tenant_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH limits AS (
        SELECT GREATEST(1, LEAST(COALESCE(p_limit, 50), 500))::bigint AS page_size,
               floor(extract(epoch FROM p_now) / 5)::bigint AS poll_tick
    ), due AS (
        SELECT DISTINCT c.tenant_id
        FROM public.offer_campaigns AS c
        WHERE c.status IN ('scheduled', 'sending')
          AND c.snapshot_scheduled_at IS NOT NULL
          AND c.snapshot_scheduled_at <= p_now
    ), numbered AS (
        SELECT tenant_id,
               row_number() OVER (ORDER BY tenant_id)::bigint AS position,
               count(*) OVER ()::bigint AS total
        FROM due
    )
    SELECT numbered.tenant_id
    FROM numbered CROSS JOIN limits
    ORDER BY mod(numbered.position - mod(limits.poll_tick * limits.page_size,
                                         numbered.total) - 1 + numbered.total,
                 numbered.total)
    LIMIT (SELECT page_size FROM limits)
$$;

REVOKE ALL ON FUNCTION public.due_campaign_tenants(timestamptz, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.due_campaign_tenants(timestamptz, integer) TO salon_app;
COMMENT ON FUNCTION public.due_campaign_tenants(timestamptz, integer) IS
    'Returns bounded due campaign tenant IDs only; campaign contents are read under forced RLS.';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
        WHERE p.oid='public.due_campaign_tenants(timestamptz,integer)'::regprocedure
          AND (r.rolsuper OR r.rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'due_campaign_tenants owner must bypass forced RLS for tenant-ID discovery';
    END IF;
END;
$$;
