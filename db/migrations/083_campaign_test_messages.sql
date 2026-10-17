-- Keep explicitly requested test sends separate from approved campaign revenue.
ALTER TABLE public.communications DROP CONSTRAINT communications_source_type_check;
ALTER TABLE public.communications ADD CONSTRAINT communications_source_type_check
    CHECK (source_type IN ('appointment', 'location_broadcast', 'rebooking_cycle',
                           'campaign', 'campaign_test'));
