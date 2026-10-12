-- TASK-032 / FR-046, FR-047: quiet hours, channel preferences and safe fallback.
--
--   messaging_settings  - one row per salon: quiet window (local time),
--                         global emergency stop, and an optional per-recipient
--                         daily cap. Absent row means product defaults
--                         (21:00-08:00 quiet, no cap, stop off).
--   client_channel_prefs- per-client per-purpose primary channel plus an
--                         optional fallback. Purpose 'default' applies where
--                         no specific row exists; without any row the product
--                         default (email, no fallback) applies.

CREATE TABLE messaging_settings (
    tenant_id uuid NOT NULL,
    -- Local quiet window: due sends inside it defer to its end instead of
    -- sending (or expiring past usefulness, which the expired gate catches).
    -- NULL means unset (no quiet enforcement); the product default is unset
    -- so sends never surprise, and tests never depend on wall-clock time.
    -- Salons configure it during communication setup.
    quiet_start time,
    quiet_end time,
    -- Global kill switch: while on, nothing dispatches (deferred, visible).
    emergency_stop boolean NOT NULL DEFAULT false,
    -- Optional throttle: at most N dispatched messages per client per day;
    -- excess defers to the next day. NULL means uncapped.
    max_per_recipient_per_day integer CHECK (max_per_recipient_per_day IS NULL
                                             OR max_per_recipient_per_day >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE client_channel_prefs (
    tenant_id uuid NOT NULL,
    client_id uuid NOT NULL,
    -- Purpose this row governs, or 'default' for every purpose without its
    -- own row. Free text on purpose: new purposes need no schema change.
    purpose text NOT NULL CHECK (length(purpose) BETWEEN 1 AND 80),
    primary_channel text NOT NULL CHECK (primary_channel IN ('email', 'sms', 'whatsapp')),
    fallback_channel text CHECK (fallback_channel IN ('email', 'sms', 'whatsapp')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, client_id, purpose),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT channel_prefs_no_self_fallback CHECK (fallback_channel IS NULL
                                                     OR fallback_channel <> primary_channel)
);

ALTER TABLE messaging_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE messaging_settings FORCE ROW LEVEL SECURITY;
CREATE POLICY messaging_settings_tenant_scope ON messaging_settings
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE client_channel_prefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_channel_prefs FORCE ROW LEVEL SECURITY;
CREATE POLICY client_channel_prefs_tenant_scope ON client_channel_prefs
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON messaging_settings, client_channel_prefs TO salon_app;
