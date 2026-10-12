-- TASK-016 / FR-020: staff profiles, qualifications, shifts, breaks, leave and locations.
--
-- Five tables, tenant-owned with FORCE RLS (data-model.md:30):
--   staff_profiles          - one staff member's bookable profile.
--   staff_service_qualifications - which services this staff member may perform.
--   staff_locations         - which locations this staff member works at.
--   staff_shifts            - a weekly shift with break intervals, in the location timezone.
--   staff_leave             - approved time off that removes availability.
--
-- A deactivation is a date, not a delete: `deactivated_on` lets scheduling stop offering
-- the staff member from that day while history and future-appointment resolution still
-- find them (FR-020). `staff_profiles.user_ref` is optional, because a staff member may be
-- scheduled before they have a login.

CREATE TABLE staff_profiles (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    display_name text NOT NULL CHECK (length(trim(display_name)) BETWEEN 1 AND 120),
    -- The membership identity this profile belongs to, when it has one.
    user_ref uuid,
    is_bookable boolean NOT NULL DEFAULT true,
    deactivated_on date,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE staff_service_qualifications (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    staff_id uuid NOT NULL,
    service_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, staff_id, service_id),
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE staff_locations (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    staff_id uuid NOT NULL,
    location_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, staff_id, location_id),
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE staff_shifts (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    staff_id uuid NOT NULL,
    location_id uuid NOT NULL,
    -- 0 = Monday .. 6 = Sunday, matching ISO-8601.
    weekday smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    -- Local wall-clock times in the location's IANA timezone.
    starts_at time NOT NULL,
    ends_at time NOT NULL,
    -- Break intervals within the shift, as a JSON array of {start,end} local times.
    breaks jsonb NOT NULL DEFAULT '[]',
    effective_from date NOT NULL DEFAULT CURRENT_DATE,
    effective_to date,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    -- A shift must end after it begins; an overnight shift is two shifts, not a negative one.
    CHECK (ends_at > starts_at),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE staff_leave (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    staff_id uuid NOT NULL,
    -- Inclusive local dates. A single-day leave has starts_on = ends_on.
    starts_on date NOT NULL,
    ends_on date NOT NULL,
    reason text CHECK (reason IS NULL OR length(reason) <= 240),
    approved boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    CHECK (ends_on >= starts_on),
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE staff_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE staff_profiles FORCE ROW LEVEL SECURITY;
CREATE POLICY staff_profiles_tenant_scope ON staff_profiles
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE staff_service_qualifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE staff_service_qualifications FORCE ROW LEVEL SECURITY;
CREATE POLICY ssq_tenant_scope ON staff_service_qualifications
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE staff_locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE staff_locations FORCE ROW LEVEL SECURITY;
CREATE POLICY staff_locations_tenant_scope ON staff_locations
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE staff_shifts ENABLE ROW LEVEL SECURITY;
ALTER TABLE staff_shifts FORCE ROW LEVEL SECURITY;
CREATE POLICY staff_shifts_tenant_scope ON staff_shifts
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE staff_leave ENABLE ROW LEVEL SECURITY;
ALTER TABLE staff_leave FORCE ROW LEVEL SECURITY;
CREATE POLICY staff_leave_tenant_scope ON staff_leave
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON staff_profiles, staff_service_qualifications, staff_locations, staff_shifts, staff_leave TO salon_app;

CREATE INDEX staff_shifts_tenant_staff_idx ON staff_shifts (tenant_id, staff_id, weekday);
CREATE INDEX staff_leave_tenant_staff_idx ON staff_leave (tenant_id, staff_id, starts_on);
CREATE INDEX staff_qualifications_tenant_service_idx ON staff_service_qualifications (tenant_id, service_id);