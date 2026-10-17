-- TASK-065 / FR-038: waitlist entries and expiring slot offers.
--
--   waitlist_entries - one row per client asking to be offered a slot for a
--                      service inside a date window. open until it is withdrawn
--                      or an offered slot is claimed (fulfilled).
--   slot_offers      - one row per offered start time, created by staff from a
--                      slot a cancellation just freed. It carries a hashed
--                      owner token (FR-029 pattern: the token is delivered
--                      out-of-band, only its hash is stored), an expiry, and
--                      what it became.
--
-- Two properties are deliberate:
--
--   1. An offer does NOT hold a reservation. The freed slot stays bookable by
--      anybody until the client claims, and the claim is what must win the
--      `reservations` EXCLUDE constraint (migration 020). That keeps the
--      one-winner property in one place: a claim inserts an appointment plus
--      its reservation through the same constraint as a manual booking or a
--      hold confirmation, so a lost race raises IntegrityError and never
--      double-books.
--   2. Claiming books an appointment and nothing else. No `consents` row and
--      no `communications` row is ever written here (FR-040 / QA-15): being
--      handed a freed slot is not opting in to marketing, and the claim path
--      has no marketing frequency cap to bypass because it sends nothing.
--
-- Re-runnable: every object is created only if absent, and the policies are
-- dropped before being recreated so a partial previous run cannot leave the
-- tables enabled without their tenant scope.

CREATE TABLE IF NOT EXISTS waitlist_entries (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    -- The salon the entry was made for. Resolved at create time from the named
    -- staff (their location), else the client's preferred location, else the
    -- tenant's oldest location - an offer must be able to name a location.
    location_id uuid NOT NULL,
    client_id uuid NOT NULL,
    service_id uuid NOT NULL,
    -- Optional preference: NULL means "any qualified staff at this location",
    -- which the offer resolves to one concrete staff member at offer time.
    staff_id uuid,
    -- Inclusive local dates the client is asking about.
    date_from date NOT NULL,
    date_to date NOT NULL,
    -- open       = waiting for a slot
    -- withdrawn  = staff or client withdrew it; any open offer is retracted
    -- fulfilled  = one of its offers was claimed and booked
    state text NOT NULL DEFAULT 'open' CHECK (state IN ('open','withdrawn','fulfilled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    CHECK (date_to >= date_from),
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS slot_offers (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    entry_id uuid NOT NULL,
    -- Snapshot of what is being offered, so a later edit to the service or the
    -- staff member's availability cannot change what the client was promised.
    location_id uuid NOT NULL,
    staff_id uuid NOT NULL,
    service_id uuid NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    -- The whole busy block including buffers, matching how every other booking
    -- occupies a staff member's time.
    occupied_range tstzrange NOT NULL,
    local_timezone text NOT NULL,
    -- The bearer credential for this offer, hashed. A wrong token and a missing
    -- offer are indistinguishable 404s (FR-028), so the hash is the only lookup
    -- key and the token itself is never stored.
    owner_token_hash text NOT NULL CHECK (length(owner_token_hash) = 64),
    -- Idempotency key for the staff create command. A replay returns the offer
    -- already created rather than offering the slot twice (api-contracts.md:11).
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 64),
    -- The claiming command's key, recorded once the offer is claimed so a
    -- retried claim replays the same appointment instead of racing a second one.
    claim_idempotency_key text,
    -- offered   = live, awaiting its holder
    -- claimed   = booked; appointment_id is what it became
    -- retracted = staff withdrew the entry or the slot was lost
    -- expired   = expires_at passed without a claim (swept by state transition)
    state text NOT NULL DEFAULT 'offered'
        CHECK (state IN ('offered','claimed','retracted','expired')),
    -- What this offer became, so a replayed claim is a receipt lookup.
    appointment_id uuid,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key),
    -- One appointment per claim command, ever: two racing claims carrying the
    -- same key cannot resolve to two bookings.
    UNIQUE (tenant_id, claim_idempotency_key),
    CHECK (ends_at > starts_at),
    CHECK (NOT isempty(occupied_range)),
    FOREIGN KEY (tenant_id, entry_id) REFERENCES waitlist_entries(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, appointment_id) REFERENCES appointments(tenant_id, id) ON DELETE SET NULL
);

ALTER TABLE waitlist_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE waitlist_entries FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS waitlist_entries_tenant_scope ON waitlist_entries;
CREATE POLICY waitlist_entries_tenant_scope ON waitlist_entries
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE slot_offers ENABLE ROW LEVEL SECURITY;
ALTER TABLE slot_offers FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS slot_offers_tenant_scope ON slot_offers;
CREATE POLICY slot_offers_tenant_scope ON slot_offers
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON waitlist_entries, slot_offers TO salon_app;

CREATE INDEX IF NOT EXISTS waitlist_entries_tenant_state_idx
    ON waitlist_entries (tenant_id, state, date_from) WHERE state = 'open';

-- The sweep path: find this tenant's still-live offers that have lapsed.
CREATE INDEX IF NOT EXISTS slot_offers_tenant_expiry_idx
    ON slot_offers (tenant_id, expires_at) WHERE state = 'offered';

-- Withdrawing an entry retracts every offer still open for it.
CREATE INDEX IF NOT EXISTS slot_offers_tenant_entry_idx
    ON slot_offers (tenant_id, entry_id) WHERE state = 'offered';
