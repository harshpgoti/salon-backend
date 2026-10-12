-- TASK-019..TASK-025: the booking core shared by availability, holds, appointments,
-- calendar management, cancellation/reschedule and completion.
--
-- Tables, all tenant-owned with FORCE RLS (data-model.md:30, :74-:80):
--   appointments            - the booked slot with its policy/version snapshot.
--   appointment_services    - the ordered services in one appointment with an immutable
--                             duration/price snapshot (FR-032).
--   booking_holds           - a short-lived reservation that carries no personal detail.
--   reservations            - the exclusive occupancy of one staff member or resource over
--                             a tstzrange; an EXCLUDE constraint makes double-booking a
--                             database fact, not a hope (FR-029, qa-plan QA-04).
--   appointment_transitions - append-only state changes with actor and reason (FR-031).
--
-- The exclusion constraint is the load-bearing part: staff and resource exclusivity are
-- enforced by Postgres, so two concurrent confirmations cannot both win (QA-04). Ranges are
-- half-open [start,end) so back-to-back appointments are adjacent, not overlapping.

CREATE TABLE appointments (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    location_id uuid NOT NULL,
    client_id uuid NOT NULL,
    staff_id uuid NOT NULL,
    -- Absolute instant, plus the location timezone it was booked in (FR-032).
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    local_timezone text NOT NULL,
    status text NOT NULL DEFAULT 'confirmed'
        CHECK (status IN ('pending_approval','confirmed','checked_in','in_service',
                          'completed','cancelled','no_show','rejected')),
    source text NOT NULL CHECK (source IN ('public_booking','staff','walk_in')),
    -- The whole-slot busy range including buffers, so occupancy and the client-facing
    -- time can differ without either being lost.
    occupied_range tstzrange NOT NULL,
    policy_version integer NOT NULL DEFAULT 1,
    cancellation_reason text,
    -- The management token hash for the client's own cancel/reschedule link (FR-033).
    management_token_hash text,
    -- The management link is only accepted while now() < expires_at (FR-033). Set to the
    -- appointment's own end, so a past appointment's link cannot be replayed to manage it.
    management_token_expires_at timestamptz,
    -- Idempotency key for a manual booking command (FR-030/034, api-contracts.md:11).
    -- A replayed create returns the original booking rather than a second one.
    idempotency_key text CHECK (idempotency_key IS NULL OR length(idempotency_key) BETWEEN 8 AND 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    CHECK (ends_at > starts_at),
    CHECK (NOT isempty(occupied_range)),
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE appointment_services (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    appointment_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence >= 1),
    service_id uuid NOT NULL,
    -- The service's version at booking, and its values frozen as booked (FR-032).
    service_version integer NOT NULL,
    service_name text NOT NULL,
    staff_id uuid NOT NULL,
    price_minor integer NOT NULL CHECK (price_minor >= 0),
    currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency = 'CAD'),
    duration_minutes integer NOT NULL CHECK (duration_minutes >= 5),
    pre_buffer_minutes integer NOT NULL DEFAULT 0,
    post_buffer_minutes integer NOT NULL DEFAULT 0,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, appointment_id, sequence),
    CHECK (ends_at > starts_at),
    FOREIGN KEY (tenant_id, appointment_id) REFERENCES appointments(tenant_id, id) ON DELETE CASCADE,
    -- The snapshot already carries the name/duration/price, so losing the source service
    -- must not lose the appointment. These cascade only with the tenant's own teardown.
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE booking_holds (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    location_id uuid NOT NULL,
    staff_id uuid NOT NULL,
    service_id uuid NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    occupied_range tstzrange NOT NULL,
    local_timezone text NOT NULL,
    -- The caller's owner token, hashed. No name, phone or email is stored on a hold
    -- (FR-029): a hold that expires must leave nothing personal behind.
    owner_token_hash text NOT NULL CHECK (length(owner_token_hash) = 64),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 64),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','converted','expired')),
    -- What this hold became once confirmed, so a replayed confirm is a receipt lookup
    -- rather than an inference from timestamps.
    appointment_id uuid,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key),
    CHECK (ends_at > starts_at),
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, appointment_id) REFERENCES appointments(tenant_id, id) ON DELETE SET NULL
);

-- One row per exclusive owner of an appointment or hold. The EXCLUDE constraint is what
-- makes "exactly one winner" true under concurrency (FR-029, QA-04): two overlapping
-- active rows for the same staff member or resource cannot both exist.
CREATE TABLE reservations (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    appointment_id uuid,
    hold_id uuid,
    -- Exactly one of these identifies the exclusive owner of the range.
    staff_id uuid,
    resource_id uuid,
    occupied_range tstzrange NOT NULL,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','released','expired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    CHECK ((appointment_id IS NOT NULL) <> (hold_id IS NOT NULL)),
    CHECK ((staff_id IS NOT NULL) <> (resource_id IS NOT NULL)),
    CHECK (NOT isempty(occupied_range)),
    FOREIGN KEY (tenant_id, appointment_id) REFERENCES appointments(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, hold_id) REFERENCES booking_holds(tenant_id, id) ON DELETE CASCADE
);

-- The concurrency guarantee. Restricted to active rows, so releasing an appointment frees
-- its range. `btree_gist` gives the equality operator on uuid that EXCLUDE needs here.
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE reservations ADD CONSTRAINT reservations_no_staff_overlap
    EXCLUDE USING gist (tenant_id WITH =, staff_id WITH =, occupied_range WITH &&)
    WHERE (state = 'active' AND staff_id IS NOT NULL);
ALTER TABLE reservations ADD CONSTRAINT reservations_no_resource_overlap
    EXCLUDE USING gist (tenant_id WITH =, resource_id WITH =, occupied_range WITH &&)
    WHERE (state = 'active' AND resource_id IS NOT NULL);

CREATE TABLE appointment_transitions (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    appointment_id uuid NOT NULL,
    from_status text,
    to_status text NOT NULL,
    actor_issuer text NOT NULL,
    actor_subject text NOT NULL,
    reason text CHECK (reason IS NULL OR length(reason) <= 240),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, appointment_id) REFERENCES appointments(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE appointments ENABLE ROW LEVEL SECURITY;
ALTER TABLE appointments FORCE ROW LEVEL SECURITY;
CREATE POLICY appointments_tenant_scope ON appointments
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE appointment_services ENABLE ROW LEVEL SECURITY;
ALTER TABLE appointment_services FORCE ROW LEVEL SECURITY;
CREATE POLICY appointment_services_tenant_scope ON appointment_services
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE booking_holds ENABLE ROW LEVEL SECURITY;
ALTER TABLE booking_holds FORCE ROW LEVEL SECURITY;
CREATE POLICY booking_holds_tenant_scope ON booking_holds
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE reservations FORCE ROW LEVEL SECURITY;
CREATE POLICY reservations_tenant_scope ON reservations
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE appointment_transitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE appointment_transitions FORCE ROW LEVEL SECURITY;
CREATE POLICY appointment_transitions_tenant_scope ON appointment_transitions
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON appointments, appointment_services, booking_holds,
    reservations, appointment_transitions TO salon_app;

CREATE INDEX appointments_tenant_start_idx ON appointments (tenant_id, starts_at);
CREATE INDEX appointments_tenant_staff_start_idx ON appointments (tenant_id, staff_id, starts_at);
-- A replayed manual-booking command (same tenant + idempotency key) resolves to one row.
CREATE UNIQUE INDEX appointments_tenant_idempotency_key ON appointments (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX reservations_tenant_range_idx ON reservations USING gist (tenant_id, occupied_range);
CREATE INDEX booking_holds_active_idx ON booking_holds (tenant_id, expires_at) WHERE state = 'active';