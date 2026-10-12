CREATE TABLE tenant_files (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    object_key text NOT NULL,
    purpose text NOT NULL,
    state text NOT NULL CHECK (state IN ('pending','available','deleting','deleted')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, object_key)
);
CREATE TABLE tenant_jobs (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    kind text NOT NULL,
    state text NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE tenant_job_attempts (
    tenant_id uuid NOT NULL,
    job_id uuid NOT NULL,
    attempt_no integer NOT NULL CHECK (attempt_no > 0),
    state text NOT NULL CHECK (state IN ('running','succeeded','failed')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    PRIMARY KEY (tenant_id, job_id, attempt_no),
    FOREIGN KEY (tenant_id, job_id) REFERENCES tenant_jobs(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE tenant_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_files FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_file_scope ON tenant_files
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE tenant_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_job_scope ON tenant_jobs
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE tenant_job_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_job_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_job_attempt_scope ON tenant_job_attempts
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_files, tenant_jobs, tenant_job_attempts TO salon_app;
CREATE INDEX tenant_jobs_due_idx ON tenant_jobs (tenant_id, state, created_at);
CREATE INDEX tenant_files_state_idx ON tenant_files (tenant_id, state, created_at);
