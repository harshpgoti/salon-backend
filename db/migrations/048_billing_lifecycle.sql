-- TASK-048 / FR-066, FR-068: plan changes, grace and restricted recovery.
--
-- tenant_subscriptions gains:
--   grace_until          - seven-day window after a failed payment. Inside it
--                          the salon keeps full booking operations while
--                          discretionary marketing stays paused; past it the
--                          worker restricts the account (TASK-048 tick).
--   scheduled_plan_id    - downgrades apply at period end, never mid-cycle:
--                          shrinking limits under live locations would strand
--                          data. Upgrades apply immediately.
--   scheduled_applies_at - when the scheduled downgrade takes effect.
--
-- The 'grace' status value from the foundation migration is retired: the
-- past_due window IS the grace period, so a separate value would fork the
-- state machine for no reason. No row ever carried it.

ALTER TABLE tenant_subscriptions
    ADD COLUMN grace_until timestamptz,
    ADD COLUMN scheduled_plan_id uuid REFERENCES billing_plans(id),
    ADD COLUMN scheduled_applies_at timestamptz;

ALTER TABLE tenant_subscriptions DROP CONSTRAINT tenant_subscriptions_status_check;
ALTER TABLE tenant_subscriptions ADD CONSTRAINT tenant_subscriptions_status_check
    CHECK (status IN ('trial', 'active', 'past_due', 'restricted',
                      'cancelled', 'suspended'));
