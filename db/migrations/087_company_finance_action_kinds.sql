-- TASK-052: invoice and dispute review actions use the same four-eyes queue.
ALTER TABLE company_action_approvals
    DROP CONSTRAINT company_action_approvals_action_check;
ALTER TABLE company_action_approvals
    ADD CONSTRAINT company_action_approvals_action_check
    CHECK (action IN ('feature_flag','tenant_suspend','tenant_reactivate',
                      'accounting_export','invoice_action','dispute_action'));
