-- TASK-045 / FR-063, FR-064: company health snapshots and projections.
--
--   company_metric_snapshots - one row per metric per month: the computed
--                              value plus when and from what it was computed.
--                              Rebuilds are full-refresh upserts (idempotent):
--                              redelivered webhooks converge to the same row,
--                              corrections and deletions propagate on the
--                              next rebuild. Months that were never stored
--                              read back as unavailable — never as silent
--                              old numbers (FR-064).
--
-- Company-global like the rest of the company surface: role-gated reads,
-- aggregates only, no client data anywhere in these numbers.

CREATE TABLE company_metric_snapshots (
    metric text NOT NULL CHECK (length(metric) BETWEEN 1 AND 80),
    period text NOT NULL CHECK (period ~ '^[0-9]{4}-[0-9]{2}$'),
    value_num numeric,
    value_json jsonb NOT NULL DEFAULT '{}',
    computed_at timestamptz NOT NULL DEFAULT now(),
    source text NOT NULL DEFAULT 'live',
    PRIMARY KEY (metric, period)
);

GRANT SELECT, INSERT, UPDATE ON company_metric_snapshots TO salon_app;
