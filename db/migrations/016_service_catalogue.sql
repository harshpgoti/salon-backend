-- TASK-015 / FR-019: versioned service catalogue with CAD minor-unit pricing.
--
-- Two tables, tenant-owned with FORCE RLS (data-model.md:30):
--   service_categories - a controlled grouping.
--   services           - a service with duration, buffers, price and rebooking interval.
--
-- Money is stored as an integer count of CAD minor units (cents), never a float: a price
-- is a fact about a transaction and rounding must be a deliberate act, not an accident of
-- binary floating point (requirements.md:56). Duration and buffers are integer minutes.
-- An archived service keeps its rows so appointment history can still name what was sold;
-- archiving sets `archived_at` and never rewrites the past (FR-019).

CREATE TABLE service_categories (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 80),
    display_order integer NOT NULL DEFAULT 0,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, name)
);

CREATE TABLE services (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    category_id uuid,
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    description text CHECK (description IS NULL OR length(description) <= 2000),
    -- Booking math, in whole minutes. A zero duration is not a bookable service.
    duration_minutes integer NOT NULL CHECK (duration_minutes BETWEEN 5 AND 1440),
    pre_buffer_minutes integer NOT NULL DEFAULT 0 CHECK (pre_buffer_minutes BETWEEN 0 AND 240),
    post_buffer_minutes integer NOT NULL DEFAULT 0 CHECK (post_buffer_minutes BETWEEN 0 AND 240),
    -- CAD minor units (cents). Stored as an integer so no rounding is implicit.
    price_minor integer NOT NULL CHECK (price_minor BETWEEN 0 AND 100000000),
    currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency = 'CAD'),
    -- How long until this service is typically rebooked; drives the retention cycle.
    rebooking_interval_days integer CHECK (rebooking_interval_days IS NULL
                                           OR rebooking_interval_days BETWEEN 1 AND 730),
    -- Whether the service needs an exclusive room/chair (TASK-018 fills the join).
    requires_resource boolean NOT NULL DEFAULT false,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, name),
    -- CASCADE, not SET NULL: a tenant's deletion must remove its whole catalogue, and a
    -- category deleted on its own takes its services with it rather than orphaning them.
    FOREIGN KEY (tenant_id, category_id) REFERENCES service_categories(tenant_id, id) ON DELETE CASCADE
);

-- Per-staff overrides (FR-019). A single (tenant, service, staff) row: a staff member has
-- at most one override for a service, which the override simply updates.
CREATE TABLE service_staff_overrides (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL,
    -- staff_profiles belongs to TASK-016, which owns its own vocabulary and constraints.
    staff_ref uuid NOT NULL,
    duration_minutes integer CHECK (duration_minutes IS NULL OR duration_minutes BETWEEN 5 AND 1440),
    price_minor integer CHECK (price_minor IS NULL OR price_minor BETWEEN 0 AND 100000000),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, service_id, staff_ref),
    FOREIGN KEY (tenant_id, service_id) REFERENCES services (tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE service_categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_categories FORCE ROW LEVEL SECURITY;
CREATE POLICY service_categories_tenant_scope ON service_categories
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE services ENABLE ROW LEVEL SECURITY;
ALTER TABLE services FORCE ROW LEVEL SECURITY;
CREATE POLICY services_tenant_scope ON services
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE service_staff_overrides ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_staff_overrides FORCE ROW LEVEL SECURITY;
CREATE POLICY service_staff_overrides_tenant_scope ON service_staff_overrides
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON service_categories, services, service_staff_overrides TO salon_app;

CREATE INDEX services_tenant_category_idx ON services (tenant_id, category_id) WHERE archived_at IS NULL;
CREATE INDEX services_tenant_active_idx ON services (tenant_id) WHERE archived_at IS NULL;