-- TASK-037 / FR-051, FR-052: discover due rebooking tenants without exposing
-- client or appointment data, then read the rows under forced tenant RLS.
CREATE OR REPLACE FUNCTION due_rebooking_tenants(p_now timestamptz, p_limit integer)
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
      SELECT c.tenant_id
      FROM public.rebooking_cycles AS c
      JOIN public.appointments AS a
      ON a.tenant_id=c.tenant_id AND a.id=c.source_appointment_id
    JOIN public.rebooking_rules AS r
      ON r.tenant_id=c.tenant_id AND r.service_id=c.service_id AND r.is_active
    LEFT JOIN LATERAL (
        SELECT count(*) AS prompt_count, max(sent_at) AS last_sent_at
        FROM public.rebooking_prompts AS p
        WHERE p.tenant_id=c.tenant_id AND p.cycle_id=c.id
    ) AS history ON true
    WHERE c.status='due'
      AND c.due_date <= (p_now AT TIME ZONE a.local_timezone)::date
      AND (c.snoozed_until IS NULL OR
           c.snoozed_until <= (p_now AT TIME ZONE a.local_timezone)::date)
      AND r.send_time_local <= (p_now AT TIME ZONE a.local_timezone)::time
      AND NOT EXISTS (
          SELECT 1
          FROM public.appointments AS upcoming
          JOIN public.appointment_services AS service_line
            ON service_line.tenant_id=upcoming.tenant_id
           AND service_line.appointment_id=upcoming.id
          WHERE upcoming.tenant_id=c.tenant_id
            AND upcoming.client_id=c.client_id
            AND service_line.service_id=c.service_id
            AND upcoming.starts_at > p_now
            AND upcoming.status IN ('pending_approval', 'confirmed',
                                    'checked_in', 'in_service')
      )
      AND history.prompt_count < 1 + r.max_follow_ups
      AND (history.prompt_count=0 OR
           history.last_sent_at + interval '7 days' <= p_now)
      GROUP BY c.tenant_id
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

REVOKE ALL ON FUNCTION due_rebooking_tenants(timestamptz, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION due_rebooking_tenants(timestamptz, integer) TO salon_app;

COMMENT ON FUNCTION due_rebooking_tenants(timestamptz, integer) IS
    'Returns bounded tenant IDs only; rebooking content is read after forced RLS scope.';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
        WHERE p.oid='due_rebooking_tenants(timestamptz,integer)'::regprocedure
          AND (r.rolsuper OR r.rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'due_rebooking_tenants owner must bypass forced RLS for tenant-ID discovery';
    END IF;
END;
$$;

-- The existing bounded body column is reused for the tracked booking URL.
-- Inbox reply idempotency and ownership invariants remain unchanged.
ALTER TABLE communications DROP CONSTRAINT communications_inbox_reply_fields_check;
ALTER TABLE communications ADD CONSTRAINT communications_inbox_reply_fields_check
    CHECK (
      (source_type = 'inbox_task' AND purpose = 'inbox_reply'
       AND channel IN ('sms', 'whatsapp')
       AND reply_body IS NOT NULL AND length(reply_body) BETWEEN 1 AND 2000
       AND reply_idempotency_key IS NOT NULL
       AND reply_request_hash IS NOT NULL
       AND reply_request_hash ~ '^[0-9a-f]{64}$' AND reply_actor_id IS NOT NULL
       AND template_id IS NULL)
      OR
      (source_type = 'rebooking_cycle' AND purpose = 'marketing'
       AND reply_body IS NOT NULL AND length(reply_body) BETWEEN 1 AND 2000
       AND reply_idempotency_key IS NULL AND reply_request_hash IS NULL
       AND reply_actor_id IS NULL AND template_id IS NULL)
      OR
      (source_type NOT IN ('inbox_task', 'rebooking_cycle') AND reply_body IS NULL
       AND reply_idempotency_key IS NULL
       AND reply_request_hash IS NULL AND reply_actor_id IS NULL)
    );
