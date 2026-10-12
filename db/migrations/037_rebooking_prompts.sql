-- TASK-037 / FR-051, FR-052: rebooking prompts, attribution and snooze.
--
--   rebooking_prompts - one row per prompt sent for a cycle: the due-day
--                       prompt and at most follow-ups to the cycle cap of two
--                       total sends (FR-052). The worker counts these for
--                       cooldown (seven days between) and the cap.
--
-- rebooking_cycles gains outcome columns (all nullable until set):
--   booked_appointment_id    - the appointment that closed the cycle.
--   attributed_communication_id - the prompt it came from (NULL = organic).
--   snoozed_until            - staff/client deferral; prompting resumes after.
--
-- communications gains attribution_token: the tracked token a prompt carries
-- and a booking link returns. appointments gains the same column so the
-- confirming transaction can match token to cycle atomically.

CREATE TABLE rebooking_prompts (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    cycle_id uuid NOT NULL,
    -- due_prompt  = the on/after-due-date nudge
    -- follow_up   = the single seven-days-later nudge
    kind text NOT NULL CHECK (kind IN ('due_prompt', 'follow_up')),
    communication_id uuid NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, cycle_id) REFERENCES rebooking_cycles(tenant_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, communication_id)
        REFERENCES communications(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE rebooking_cycles
    ADD COLUMN booked_appointment_id uuid,
    ADD COLUMN attributed_communication_id uuid,
    ADD COLUMN snoozed_until date;

ALTER TABLE communications ADD COLUMN attribution_token text;
CREATE UNIQUE INDEX communications_attribution_uniq
    ON communications (tenant_id, attribution_token)
    WHERE attribution_token IS NOT NULL;

ALTER TABLE appointments ADD COLUMN attribution_token text;

-- Prompts are about a cycle, not an appointment: they carry the cycle as
-- their source, and the dispatcher version-checks the cycle row instead of
-- an appointment row (an unrelated reschedule must never stale a prompt).
ALTER TABLE communications DROP CONSTRAINT communications_source_type_check;
ALTER TABLE communications ADD CONSTRAINT communications_source_type_check
    CHECK (source_type IN ('appointment', 'location_broadcast', 'rebooking_cycle'));

ALTER TABLE rebooking_prompts ENABLE ROW LEVEL SECURITY;
ALTER TABLE rebooking_prompts FORCE ROW LEVEL SECURITY;
CREATE POLICY rebooking_prompts_tenant_scope ON rebooking_prompts
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT ON rebooking_prompts TO salon_app;

CREATE INDEX rebooking_prompts_tenant_cycle_idx ON rebooking_prompts (tenant_id, cycle_id);
