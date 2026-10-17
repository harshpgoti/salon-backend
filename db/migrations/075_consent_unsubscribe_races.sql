-- TASK-028: keep a marketing grant tied to the address the client actually
-- authorized, and make one-click email opt-out a tenant-safe capability.
ALTER TABLE consents ADD COLUMN contact_value_snapshot text;
UPDATE consents c
   SET contact_value_snapshot=lower(btrim(cc.normalized_value))
  FROM client_contacts cc
 WHERE cc.tenant_id=c.tenant_id AND cc.id=c.contact_id;
ALTER TABLE consents ALTER COLUMN contact_value_snapshot SET NOT NULL;

-- Existing grants predate an immutable destination snapshot. Preserve the
-- evidence row, but require fresh proof before sending to a destination.
UPDATE consents
   SET expires_at=LEAST(COALESCE(expires_at, now()), now()),
       version=version+1, updated_at=now()
 WHERE purpose='marketing' AND status='granted';

CREATE TABLE email_unsubscribe_capabilities (
    tenant_id uuid NOT NULL,
    token_hash char(64) NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    client_id uuid NOT NULL,
    contact_value text NOT NULL CHECK (length(contact_value) BETWEEN 3 AND 320),
    channel text NOT NULL DEFAULT 'email' CHECK (channel='email'),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, token_hash),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE
);
ALTER TABLE email_unsubscribe_capabilities ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_unsubscribe_capabilities FORCE ROW LEVEL SECURITY;
CREATE POLICY email_unsubscribe_tenant_scope ON email_unsubscribe_capabilities
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, DELETE ON email_unsubscribe_capabilities TO salon_app;
CREATE INDEX email_unsubscribe_expiry_idx
    ON email_unsubscribe_capabilities (tenant_id, expires_at);

-- The opaque URL token is a bearer capability. Keep cross-tenant resolution
-- and suppression application in one function; callers never receive contact
-- details. The matching advisory lock is shared with in-process dispatch.
CREATE FUNCTION apply_email_unsubscribe(p_token_hash text)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE target record;
BEGIN
    IF p_token_hash !~ '^[0-9a-f]{64}$' THEN
        RETURN false;
    END IF;
    SELECT tenant_id, client_id, contact_value, channel
      INTO target
      FROM public.email_unsubscribe_capabilities
     WHERE token_hash=p_token_hash AND expires_at > now()
     ORDER BY created_at DESC
     LIMIT 1
     FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        target.tenant_id::text || ':' || target.channel || ':' || lower(btrim(target.contact_value)), 0));
    INSERT INTO public.suppressions
        (tenant_id, id, client_id, contact_value, channel, reason, source)
    VALUES
        (target.tenant_id, gen_random_uuid(), target.client_id,
         lower(btrim(target.contact_value)), target.channel, 'unsubscribe', 'client_self')
    ON CONFLICT (tenant_id, contact_value, channel)
    DO UPDATE SET reason='unsubscribe', source='client_self',
                  suppressed_at=now(), version=public.suppressions.version+1,
                  updated_at=now();
    RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION apply_email_unsubscribe(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION apply_email_unsubscribe(text) TO salon_app;
COMMENT ON FUNCTION apply_email_unsubscribe(text) IS
    'Applies a valid opaque email opt-out capability without exposing tenant contact data.';
