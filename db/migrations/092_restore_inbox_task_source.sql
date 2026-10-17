-- Migration 083 rebuilt this source constraint without the inbox_task source
-- introduced in 077. Restore the existing reply source while preserving every
-- source currently accepted by the latest constraint.
ALTER TABLE communications DROP CONSTRAINT communications_source_type_check;
ALTER TABLE communications ADD CONSTRAINT communications_source_type_check
    CHECK (source_type IN ('appointment', 'location_broadcast', 'rebooking_cycle',
                           'campaign', 'campaign_test', 'inbox_task'));
