-- TASK-051 / FR-071: preserve safe request and worker-job correlates on cases.
ALTER TABLE support_cases
    ADD COLUMN request_id uuid,
    ADD COLUMN job_id uuid;

ALTER TABLE support_cases
    ADD CONSTRAINT support_cases_tenant_job_fk
    FOREIGN KEY (tenant_id, job_id)
    REFERENCES tenant_jobs (tenant_id, id)
    ON DELETE RESTRICT;

CREATE INDEX support_cases_tenant_request_idx
    ON support_cases (tenant_id, request_id) WHERE request_id IS NOT NULL;

CREATE OR REPLACE FUNCTION company_case_list()
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COALESCE(jsonb_agg(row ORDER BY row.created_at DESC), '[]'::jsonb)
    FROM (
        SELECT c.id AS id, c.tenant_id AS tenant_id, t.display_name AS tenant_name,
               c.title, c.severity, c.status,
               c.assignee_staff_id AS assignee, c.request_id, c.appointment_id,
               c.job_id, c.communication_id, c.trace_id, c.internal_notes,
               c.created_at
        FROM support_cases c
        JOIN tenants t ON t.id = c.tenant_id
    ) row
$$;

