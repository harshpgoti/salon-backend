-- TASK-053 / FR-077, FR-078: request tracing and operational metrics.
--
--   trace_events - one row per span of a request's journey: public request,
--                  booking transaction, outbox record, worker claim/send,
--                  provider callback. The detail JSON holds identifiers,
--                  states and counts only — contact values, message bodies,
--                  tokens, secrets and URLs are blocked by the sanitizer
--                  before insert (AC-2), so telemetry can never leak them.
--                  Case diagnostics read the timeline by trace id (AC-1).
--
-- communications gains the trace id so every hop correlates to the request
-- that caused it.

CREATE TABLE trace_events (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    trace_id uuid NOT NULL,
    -- public_request | booking_commit | outbox_record | worker_claim |
    -- worker_send | provider_callback | suppression_skip | retry_schedule
    span text NOT NULL CHECK (span IN (
        'public_request', 'booking_commit', 'outbox_record', 'worker_claim',
        'worker_send', 'provider_callback', 'suppression_skip', 'retry_schedule')),
    detail jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

ALTER TABLE communications ADD COLUMN trace_id uuid;

ALTER TABLE trace_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE trace_events FORCE ROW LEVEL SECURITY;
CREATE POLICY trace_events_tenant_scope ON trace_events
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT ON trace_events TO salon_app;

CREATE INDEX trace_events_tenant_trace_idx ON trace_events (tenant_id, trace_id);
