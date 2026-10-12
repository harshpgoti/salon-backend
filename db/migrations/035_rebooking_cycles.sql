-- TASK-035 / FR-049: versioned rebooking cycles from completed services.
--
--   client_rebooking_overrides - per-client per-service interval in days.
--                                Wins over the service default; versioned so a
--                                later change never rewrites history.
--   rebooking_cycles           - one row per completed service line: the fact
--                                "client C is due for service S on date D".
--                                interval_days and due_date are snapshots — a
--                                later rule change affects new completions
--                                only. A newer completion for the same
--                                client+service supersedes the older due row,
--                                so no stale cycle survives. Cancelled and
--                                no-show appointments complete no visit and
--                                therefore create no cycle.
--
-- Statuses booked/snoozed/dismissed are transitioned by TASK-037; this task
-- only ever writes due and superseded.

CREATE TABLE client_rebooking_overrides (
    tenant_id uuid NOT NULL,
    client_id uuid NOT NULL,
    service_id uuid NOT NULL,
    interval_days integer NOT NULL CHECK (interval_days BETWEEN 1 AND 730),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, client_id, service_id),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE rebooking_cycles (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL,
    service_id uuid NOT NULL,
    service_version integer,
    visit_id uuid NOT NULL,
    source_appointment_id uuid NOT NULL,
    -- Snapshot of the rule that created this row (override or service
    -- default at completion time). Later rule edits never touch it.
    interval_days integer NOT NULL CHECK (interval_days BETWEEN 1 AND 730),
    due_date date NOT NULL,
    -- due        = waiting; the actionable row (one per client+service)
    -- superseded = replaced by a newer completion's cycle
    -- booked     = client rebooked (TASK-037 attribution)
    -- snoozed    = staff/client deferred to a later date (TASK-037)
    -- dismissed  = staff closed without booking (TASK-037)
    status text NOT NULL DEFAULT 'due'
        CHECK (status IN ('due', 'superseded', 'booked', 'snoozed', 'dismissed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, visit_id) REFERENCES visits(tenant_id, id) ON DELETE CASCADE
);

-- Exactly one actionable row per client and service.
CREATE UNIQUE INDEX rebooking_cycles_open_uniq
    ON rebooking_cycles (tenant_id, client_id, service_id)
    WHERE status = 'due';

ALTER TABLE client_rebooking_overrides ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_rebooking_overrides FORCE ROW LEVEL SECURITY;
CREATE POLICY client_rebooking_overrides_tenant_scope ON client_rebooking_overrides
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE rebooking_cycles ENABLE ROW LEVEL SECURITY;
ALTER TABLE rebooking_cycles FORCE ROW LEVEL SECURITY;
CREATE POLICY rebooking_cycles_tenant_scope ON rebooking_cycles
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON client_rebooking_overrides, rebooking_cycles TO salon_app;

CREATE INDEX rebooking_cycles_tenant_due_idx ON rebooking_cycles (tenant_id, due_date)
    WHERE status = 'due';
CREATE INDEX rebooking_cycles_tenant_client_idx ON rebooking_cycles (tenant_id, client_id);
