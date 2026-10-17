-- TASK-052: durable, idempotent four-eyes approvals for finance/ops actions.
CREATE TABLE company_action_approvals (
    id uuid NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    action text NOT NULL CHECK (action IN ('feature_flag','tenant_suspend','tenant_reactivate','accounting_export')),
    tenant_id uuid REFERENCES tenants(id) ON DELETE SET NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    requested_by_issuer text NOT NULL,
    requested_by_subject text NOT NULL,
    status text NOT NULL DEFAULT 'requested' CHECK (status IN ('requested','approved','rejected','completed')),
    decided_by_issuer text,
    decided_by_subject text,
    decision_key text,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (requested_by_issuer, requested_by_subject, idempotency_key)
);
CREATE INDEX company_action_approvals_status_idx ON company_action_approvals(status, created_at);
ALTER TABLE finance_adjustments ADD COLUMN request_key text;
ALTER TABLE finance_adjustments ADD COLUMN decision_key text;
CREATE UNIQUE INDEX finance_adjustments_request_key_idx
    ON finance_adjustments(requested_by_issuer, requested_by_subject, request_key)
    WHERE request_key IS NOT NULL;
GRANT SELECT, INSERT, UPDATE ON company_action_approvals TO salon_app;
