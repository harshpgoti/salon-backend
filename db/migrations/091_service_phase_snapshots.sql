-- TASK-067: preserve the staff assignment and service phases selected at booking time.
ALTER TABLE booking_holds
    ADD COLUMN processing_phases jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE appointment_services
    ADD COLUMN processing_phases jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE reservations
    ADD COLUMN phase_sequence smallint
        CHECK (phase_sequence IS NULL OR phase_sequence BETWEEN 1 AND 12);
