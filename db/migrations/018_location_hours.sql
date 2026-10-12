-- TASK-017 / FR-022, FR-024: weekly hours and dated exceptions, versioned.
--
-- Two tables, tenant-owned with FORCE RLS (data-model.md:30):
--   location_hours       - a weekly opening interval, multiple per weekday.
--   location_exceptions  - a dated override (holiday, temporary closure, special hours).
--
-- Every interval is local wall-clock in the location's IANA timezone, which `locations`
-- already stores. An interval must end after it begins within the same day: an overnight
-- window is expressed as two intervals, so no rule ever has to be interpreted across a
-- DST transition (qa-plan.md QA-09). Editing hours writes a new version rather than
-- overwriting the old one in place, so a past calendar rule stays reproducible (FR-024).

CREATE TABLE location_hours (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    location_id uuid NOT NULL,
    -- 0 = Monday .. 6 = Sunday, matching ISO-8601.
    weekday smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    opens_at time NOT NULL,
    closes_at time NOT NULL,
    effective_from date NOT NULL DEFAULT CURRENT_DATE,
    effective_to date,
    -- A superseded row keeps its values; `superseded_at` marks it replaced.
    superseded_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    CHECK (closes_at > opens_at),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE location_exceptions (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    location_id uuid NOT NULL,
    -- A single local date on which normal weekly hours do not apply.
    exception_date date NOT NULL,
    -- 'closed' has no interval; 'custom' must carry one.
    kind text NOT NULL CHECK (kind IN ('closed','custom')),
    opens_at time,
    closes_at time,
    label text CHECK (label IS NULL OR length(label) <= 120),
    -- Whether this change has been published to clients, with the date it takes effect.
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, location_id, exception_date),
    -- A closed day carries no hours; a custom day must carry a valid interval.
    CHECK (
        (kind = 'closed' AND opens_at IS NULL AND closes_at IS NULL)
        OR (kind = 'custom' AND opens_at IS NOT NULL AND closes_at IS NOT NULL AND closes_at > opens_at)
    ),
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE location_hours ENABLE ROW LEVEL SECURITY;
ALTER TABLE location_hours FORCE ROW LEVEL SECURITY;
CREATE POLICY location_hours_tenant_scope ON location_hours
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE location_exceptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE location_exceptions FORCE ROW LEVEL SECURITY;
CREATE POLICY location_exceptions_tenant_scope ON location_exceptions
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON location_hours, location_exceptions TO salon_app;

CREATE INDEX location_hours_active_idx ON location_hours (tenant_id, location_id, weekday)
    WHERE superseded_at IS NULL;
CREATE INDEX location_exceptions_tenant_date_idx ON location_exceptions (tenant_id, location_id, exception_date);