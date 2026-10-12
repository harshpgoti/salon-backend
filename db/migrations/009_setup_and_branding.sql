-- FR-007 setup checklist and FR-008 tenant settings.
--
-- Branding, cancellation terms and notification preferences are validated extensible
-- configuration (data-model.md:117), so they live in one JSONB column. The shape is
-- constrained here as well as in the API: `salon_app` has INSERT/UPDATE on `tenants`,
-- so "validated" would otherwise be a property of one process rather than of the column,
-- and an unvalidated colour would be republished verbatim to the public booking page.
ALTER TABLE tenants ADD COLUMN settings jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE tenants ADD COLUMN settings_version integer NOT NULL DEFAULT 1 CHECK (settings_version >= 1);

ALTER TABLE tenants ADD CONSTRAINT tenants_settings_shape CHECK (
    jsonb_typeof(settings) = 'object'
    -- Every key is guarded by its jsonb_typeof first: JSON null yields SQL NULL from
    -- ->>, and NULL ~ regex is NULL, which a CHECK accepts. A bare regex test would
    -- therefore let a null colour through.
    AND (NOT settings ? 'brand_name'
        OR (jsonb_typeof(settings -> 'brand_name') = 'string'
            AND length(settings ->> 'brand_name') BETWEEN 1 AND 120))
    AND (NOT settings ? 'primary_colour'
        OR (jsonb_typeof(settings -> 'primary_colour') = 'string'
            AND (settings ->> 'primary_colour') ~ '^#[0-9a-f]{6}$'))
    AND (NOT settings ? 'accent_colour'
        OR (jsonb_typeof(settings -> 'accent_colour') = 'string'
            AND (settings ->> 'accent_colour') ~ '^#[0-9a-f]{6}$'))
    AND (NOT settings ? 'cancellation_policy'
        OR (jsonb_typeof(settings -> 'cancellation_policy') = 'string'
            AND length(settings ->> 'cancellation_policy') BETWEEN 1 AND 2000))
    AND (NOT settings ? 'notification_language'
        OR (jsonb_typeof(settings -> 'notification_language') = 'string'
            AND (settings ->> 'notification_language') IN ('en-CA', 'fr-CA')))
    AND (NOT settings ? 'appointment_reminders' OR jsonb_typeof(settings -> 'appointment_reminders') = 'boolean')
    AND (NOT settings ? 'marketing_messages' OR jsonb_typeof(settings -> 'marketing_messages') = 'boolean')
);

-- Only the steps whose underlying records do not exist yet are stored. `location` is
-- satisfied by TASK-002 and `qualified_staff` by any non-owner membership from TASK-007,
-- so both are derived at read time rather than faked here. `hours` and `service` become
-- derived when TASK-015 and TASK-017 land their tables.
CREATE TABLE tenant_setup_steps (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    step text NOT NULL CHECK (step IN ('business_identity','hours','service','public_link',
                                       'communication_setup','import')),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','complete','skipped')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, step)
);

ALTER TABLE tenant_setup_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_setup_steps FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_step_tenant_scope ON tenant_setup_steps
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_setup_steps TO salon_app;

-- tenants and locations are FORCE RLS scoped to app.tenant_id, so an unauthenticated
-- public read cannot resolve a booking slug through an ordinary query. This function
-- resolves the slug and nothing else: booking_slug is globally unique and is already a
-- public opaque identifier (requirements.md:32, api-contracts.md:42). Every read after it
-- goes through normal RLS with the tenant context set.
--
-- Two things here are load-bearing and must not be "tidied":
--
-- 1. The parameter is deliberately not called `slug`: locations also has a per-tenant
--    `slug` column, and a bare name in a SQL function resolves to the column, not the
--    argument, so the original silently matched nothing.
-- 2. `public` must stay ahead of `pg_temp`, and the definer must hold BYPASSRLS.
--    `locations` is FORCE RLS, which applies to the table owner too, so a definer that is
--    merely the owner still gets zero rows - the public page would 404 for every tenant
--    with no error anywhere. Production provisions its runtime role separately
--    (000_app_role.sql), so whoever owns this function there must carry BYPASSRLS.

CREATE FUNCTION public_booking_slug_lookup(p_booking_slug text)
    RETURNS TABLE(tenant_id uuid, location_id uuid)
    LANGUAGE sql SECURITY DEFINER STABLE SET search_path = public, pg_temp AS $$
    SELECT l.tenant_id, l.id FROM locations l WHERE l.booking_slug = p_booking_slug LIMIT 1;
$$;

-- Check the function's owner, not the role running this migration: the two can differ,
-- and `ALTER FUNCTION ... OWNER TO` after the fact would otherwise reintroduce the silent
-- 404 with nothing to catch it.
DO $$
DECLARE
    fn_owner text;
BEGIN
    SELECT r.rolname INTO fn_owner
      FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner
     WHERE p.oid = 'public_booking_slug_lookup(text)'::regprocedure;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = fn_owner AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'public_booking_slug_lookup owner % cannot see FORCE RLS tables; give it BYPASSRLS or the public booking page 404s for every tenant', fn_owner;
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION public_booking_slug_lookup(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public_booking_slug_lookup(text) TO salon_app;

CREATE INDEX setup_steps_pending_idx ON tenant_setup_steps (tenant_id) WHERE state = 'pending';