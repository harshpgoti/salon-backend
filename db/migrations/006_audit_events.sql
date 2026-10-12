-- Deliberately no tenant FK: immutable audit evidence survives tenant erasure; access still requires a live scoped membership.
CREATE TABLE tenant_audit_events (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    actor_issuer text NOT NULL,
    actor_subject text NOT NULL,
    action text NOT NULL CHECK (length(action) BETWEEN 1 AND 100),
    resource_type text NOT NULL CHECK (length(resource_type) BETWEEN 1 AND 80),
    resource_id uuid NOT NULL,
    reason text NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 240),
    request_id uuid NOT NULL,
    changed_fields text[] NOT NULL DEFAULT '{}',
    occurred_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    CHECK (cardinality(changed_fields) <= 32),
    CHECK (array_position(changed_fields, '') IS NULL)
);
ALTER TABLE tenant_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_audit_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_audit_scope ON tenant_audit_events
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT ON tenant_audit_events TO salon_app;
CREATE INDEX tenant_audit_timeline_idx ON tenant_audit_events (tenant_id, occurred_at DESC, id);

CREATE FUNCTION deny_tenant_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'tenant audit events are immutable' USING ERRCODE = '42501';
END;
$$;
CREATE TRIGGER tenant_audit_immutable
    BEFORE UPDATE OR DELETE ON tenant_audit_events
    FOR EACH ROW EXECUTE FUNCTION deny_tenant_audit_mutation();
