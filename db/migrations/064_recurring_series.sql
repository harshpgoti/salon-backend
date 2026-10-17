-- TASK-064 / FR-037: Explicit recurring appointment series
--
-- Table: recurring_series - one row per series definition.
-- Table: appointments gets series_id/occurrence_index linkage for the created occurrences.
--
-- RLS: every table is FORCE RLS on app.tenant_id (same pattern as 020).

-- Re-runnable: CREATE TABLE IF NOT EXISTS, guarded ALTER with DO blocks.

-- ---------------------------------------------------
-- 1. recurring_series table
-- ---------------------------------------------------
CREATE TABLE IF NOT EXISTS recurring_series (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id uuid,
    staff_id uuid,
    service_id uuid NOT NULL,
    location_id uuid NOT NULL,
    local_time text NOT NULL,
    frequency text NOT NULL DEFAULT 'weekly'
        CHECK (frequency IN ('weekly')),
    interval_weeks integer NOT NULL DEFAULT 1 CHECK (interval_weeks >= 1),
    byweekday smallint[],
    start_date date NOT NULL,
    end_rule text NOT NULL DEFAULT 'count'
        CHECK (end_rule IN ('count','until_date')),
    occurrence_count integer,
    until_date date,
    policy_duration_minutes integer NOT NULL DEFAULT 60,
    policy_price_minor integer NOT NULL DEFAULT 0 CHECK (policy_price_minor >= 0),
    policy_currency char(3) NOT NULL DEFAULT 'CAD' CHECK (policy_currency = 'CAD'),
    policy_pre_buffer_minutes integer NOT NULL DEFAULT 0,
    policy_post_buffer_minutes integer NOT NULL DEFAULT 0,
    policy_service_version integer NOT NULL DEFAULT 1 CHECK (policy_service_version >= 1),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','cancelled')),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_profiles(tenant_id, id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, service_id) REFERENCES services(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, id) ON DELETE CASCADE,
    CHECK (start_date >= CURRENT_DATE - INTERVAL '365 days')
);

-- RLS
ALTER TABLE recurring_series ENABLE ROW LEVEL SECURITY;
ALTER TABLE recurring_series FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS recurring_series_tenant_scope ON recurring_series;
CREATE POLICY recurring_series_tenant_scope ON recurring_series
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON recurring_series TO salon_app;

-- ---------------------------------------------------
-- 2. Add series_id / occurrence_index to appointments
-- ---------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'appointments'
                     AND column_name IN ('series_id','occurrence_index')) THEN
        EXECUTE $ddl$
            ALTER TABLE appointments ADD COLUMN series_id uuid;
            ALTER TABLE appointments ADD COLUMN occurrence_index integer;
            CREATE INDEX IF NOT EXISTS appointments_tenant_series_idx
                ON appointments (tenant_id, series_id, occurrence_index);
            ALTER TABLE appointments
                ADD CONSTRAINT fk_appointments_series
                FOREIGN KEY (tenant_id, series_id)
                REFERENCES recurring_series(tenant_id, id) ON DELETE SET NULL;
        $ddl$;
    END IF;
END $$;

-- Index on the new columns for fast series queries
CREATE INDEX IF NOT EXISTS appointments_series_occ_idx
    ON appointments (tenant_id, series_id, occurrence_index);

GRANT SELECT, INSERT, UPDATE ON appointments TO salon_app;