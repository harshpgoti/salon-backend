-- TASK-036 / FR-050: deterministic rebooking rules with previews.
--
--   rebooking_rules - one row per service: the interval/channel/send-time/
--                     follow-up policy the owner configures, versioned
--                     in place (expected_version) like services. is_active
--                     selects the rules TASK-037's prompt worker reads.
--                     segment_id is a forward reference to TASK-038's audience
--                     segments: accepted only as NULL until segments land, so
--                     no invalid audience reference can slip in meanwhile.

CREATE TABLE rebooking_rules (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL,
    interval_days integer NOT NULL CHECK (interval_days BETWEEN 1 AND 730),
    channel text NOT NULL CHECK (channel IN ('email', 'sms', 'whatsapp')),
    -- Local send time for prompts, resolved in each location's timezone.
    send_time_local time NOT NULL DEFAULT '10:00',
    -- FR-052 caps a cycle at two prompts; the rule cannot promise more.
    max_follow_ups integer NOT NULL DEFAULT 1 CHECK (max_follow_ups BETWEEN 0 AND 2),
    segment_id uuid,
    is_active boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, service_id),
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT rebooking_rules_no_segment_yet CHECK (segment_id IS NULL)
);

ALTER TABLE rebooking_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE rebooking_rules FORCE ROW LEVEL SECURITY;
CREATE POLICY rebooking_rules_tenant_scope ON rebooking_rules
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON rebooking_rules TO salon_app;
