-- TASK-025: manual salon receipt ledger (FR-036).
--
-- These are *recorded* salon payments — cash/card the receptionist logs at checkout — not
-- verified card settlement and not SaaS subscription revenue (data-model.md:84). The
-- ledger is append-only: a refund or adjustment is a NEW compensating entry, never an
-- edit of a posted one, so the history of what was recorded stays intact. Tenant-owned
-- with composite keys and FORCE RLS (data-model.md:30).

CREATE TABLE salon_receipt_entries (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    appointment_id uuid,
    client_id uuid NOT NULL,
    -- Signed minor units: a payment is positive, a refund/adjustment may be negative.
    amount_minor integer NOT NULL CHECK (amount_minor <> 0),
    currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency = 'CAD'),
    method text NOT NULL CHECK (method IN ('cash','card','other')),
    kind text NOT NULL CHECK (kind IN ('payment','refund','adjustment')),
    external_reference text,
    note text CHECK (note IS NULL OR length(note) <= 500),
    -- Caller-supplied idempotency key so a retried post returns the same entry.
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 64),
    posted_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, appointment_id) REFERENCES appointments(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    -- A payment is always a positive amount; refunds/adjustments carry their own sign.
    CHECK (kind <> 'payment' OR amount_minor > 0)
);

ALTER TABLE salon_receipt_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE salon_receipt_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY salon_receipt_entries_tenant_scope ON salon_receipt_entries
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT ON salon_receipt_entries TO salon_app;

CREATE INDEX salon_receipt_entries_tenant_appointment_idx
    ON salon_receipt_entries (tenant_id, appointment_id);
CREATE INDEX salon_receipt_entries_tenant_client_idx
    ON salon_receipt_entries (tenant_id, client_id);
