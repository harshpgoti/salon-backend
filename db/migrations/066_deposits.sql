-- TASK-066 / FR-038: validated, provider-hosted customer deposits.
--
-- Deferred behind gate G-DEPOSIT-SCOPE; now built with the product owner's explicit
-- approval. The provider is SYNTHETIC and in-app (see src/modules/booking/deposits.py):
-- there are no real Stripe/card calls and whether a payment succeeds, fails or is
-- disputed is a request field the synthetic provider "honors". No card data exists here
-- or anywhere.
--
-- A customer deposit is money the SALON collects up-front to secure a booking. It is the
-- salon's own merchant money and lives in its own append-only deposit ledger. It must
-- NEVER appear in the company SaaS ledger (company_finance_ledger reads billing_events /
-- finance_adjustments, which this migration neither writes nor references): deposit money
-- is not subscription revenue, and a test proves the two never mix. Signed minor units in
-- CAD, tenant-owned with composite keys and FORCE RLS (data-model.md:30, mirroring the
-- manual receipt ledger in 022). Refunds and releases are compensating entries, never edits
-- of a posted row, so the full history of what moved and when stays intact.
--
--   deposits               - one intent per (tenant, idempotency_key); the pay token is stored
--                            as a sha256 hash only (FR-029 owner-token pattern), state machine
--                            pending -> held -> captured|refunded, with released/disputed exits.
--   deposit_ledger_entries - the append-only SIGNED money ledger. `hold` is positive (money
--                            held for the booking); `refund`/`release`/`dispute` are negative
--                            (money returned or contested). Sign convention is enforced below.
--                            sum(amount_minor) is the salon's reconcilable deposit float.
--   deposit_transitions    - append-only deposit state history (like appointment_transitions),
--                            carrying from_state/to_state/actor/reason. No money lives here, so
--                            closing a never-paid intent is recorded without corrupting the float.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = current_schema() AND table_name = 'deposits') THEN
        CREATE TABLE deposits (
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            appointment_id uuid NOT NULL,
            -- Signed minor units in CAD, positive: a deposit is money coming in.
            amount_minor integer NOT NULL CHECK (amount_minor > 0),
            currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency = 'CAD'),
            -- pending  = intent created, awaiting the provider callback
            -- held     = provider paid; funds are held for the booking
            -- captured = held funds finalized as the salon's (settlement worker writes this;
            --            not reachable through the TASK-066 API)
            -- released = released back to the customer (expired hold / failed booking recovery)
            -- disputed = a dispute is contesting the held funds
            -- refunded = funds returned to the customer
            state text NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','held','captured','released','disputed','refunded')),
            -- The caller's pay token, hashed. No card data and no plaintext token is stored
            -- (FR-029): the holder is identified by producing the token again.
            pay_token_hash text NOT NULL CHECK (length(pay_token_hash) = 64),
            -- The pay window for a *pending* intent: created_at + hold_minutes.
            hold_minutes integer NOT NULL DEFAULT 1440 CHECK (hold_minutes > 0),
            hold_expires_at timestamptz NOT NULL,
            -- Set only once held: the window the held funds stay holdable before the sweep
            -- releases them (held past this instant is released by /deposits/sweep).
            held_expires_at timestamptz,
            -- Opaque synthetic provider reference; never card data.
            provider_ref text,
            -- Caller-supplied idempotency key so a retried create returns the same intent.
            idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 64),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
            PRIMARY KEY (tenant_id, id),
            UNIQUE (tenant_id, idempotency_key),
            FOREIGN KEY (tenant_id, appointment_id) REFERENCES appointments(tenant_id, id) ON DELETE CASCADE
        );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = current_schema() AND table_name = 'deposit_ledger_entries') THEN
        CREATE TABLE deposit_ledger_entries (
            tenant_id uuid NOT NULL,
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            deposit_id uuid NOT NULL,
            -- hold / capture are money-in; refund / release / dispute are compensating money-out.
            kind text NOT NULL CHECK (kind IN ('hold','capture','refund','release','dispute')),
            -- Signed minor units, never zero: a hold is positive, a compensating entry is negative.
            amount_minor integer NOT NULL CHECK (amount_minor <> 0),
            currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency = 'CAD'),
            note text CHECK (note IS NULL OR length(note) <= 500),
            -- Synthetic provider reference for this movement; never card data.
            provider_ref text,
            entry_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
            PRIMARY KEY (tenant_id, id),
            FOREIGN KEY (tenant_id, deposit_id) REFERENCES deposits(tenant_id, id) ON DELETE CASCADE,
            -- `hold` only ever adds money.
            CHECK (kind <> 'hold' OR amount_minor > 0),
            -- `refund`/`release`/`dispute` only ever take compensating money out.
            CHECK (kind NOT IN ('refund','release','dispute') OR amount_minor < 0)
        );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = current_schema() AND table_name = 'deposit_transitions') THEN
        CREATE TABLE deposit_transitions (
            tenant_id uuid NOT NULL,
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            deposit_id uuid NOT NULL,
            from_state text,
            to_state text NOT NULL,
            actor_issuer text NOT NULL,
            actor_subject text NOT NULL,
            reason text CHECK (reason IS NULL OR length(reason) <= 240),
            occurred_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            FOREIGN KEY (tenant_id, deposit_id) REFERENCES deposits(tenant_id, id) ON DELETE CASCADE
        );
    END IF;
END $$;

-- RLS: every table is scoped to app.tenant_id with FORCE so even the owner role is
-- contained (same contained pattern as 020/022). The deposit ledger is queried only
-- within its own tenant.
ALTER TABLE deposits ENABLE ROW LEVEL SECURITY;
ALTER TABLE deposits FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS deposits_tenant_scope ON deposits;
CREATE POLICY deposits_tenant_scope ON deposits
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE deposit_ledger_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE deposit_ledger_entries FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS deposit_ledger_entries_tenant_scope ON deposit_ledger_entries;
CREATE POLICY deposit_ledger_entries_tenant_scope ON deposit_ledger_entries
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE deposit_transitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE deposit_transitions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS deposit_transitions_tenant_scope ON deposit_transitions;
CREATE POLICY deposit_transitions_tenant_scope ON deposit_transitions
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON deposits, deposit_ledger_entries, deposit_transitions TO salon_app;

-- Sweep scans a tenant's open deposits by window; reads join a deposit to its ledger.
CREATE INDEX IF NOT EXISTS deposits_tenant_state_hold_idx
    ON deposits (tenant_id, state, hold_expires_at);
CREATE INDEX IF NOT EXISTS deposits_tenant_appointment_idx
    ON deposits (tenant_id, appointment_id);
CREATE INDEX IF NOT EXISTS deposit_ledger_entries_tenant_deposit_idx
    ON deposit_ledger_entries (tenant_id, deposit_id, entry_at);
CREATE INDEX IF NOT EXISTS deposit_transitions_tenant_deposit_idx
    ON deposit_transitions (tenant_id, deposit_id, occurred_at);
