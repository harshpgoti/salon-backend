-- TASK-033 / FR-026, FR-042: location-broadcast sourcing.
--
-- Broadcasts announce a location (not one appointment), so communications
-- learn a second source type. The appointment version gate stays
-- appointment-specific: broadcasts skip it and rely on consent,
-- suppression, quiet hours and budget like every other send.

ALTER TABLE communications DROP CONSTRAINT communications_source_type_check;
ALTER TABLE communications ADD CONSTRAINT communications_source_type_check
    CHECK (source_type IN ('appointment', 'location_broadcast'));
