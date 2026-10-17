-- Keep direct import/fixture inserts in the same deterministic event order
-- as application writes. The lock key matches lock_contact_channel().
CREATE FUNCTION assign_consent_event_sequence() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.event_sequence IS NULL THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(
            NEW.tenant_id::text || ':' || NEW.channel || ':' ||
            lower(btrim(NEW.contact_value_snapshot)), 0));
        SELECT COALESCE(MAX(event_sequence), 0) + 1
          INTO NEW.event_sequence
          FROM consents
         WHERE tenant_id = NEW.tenant_id
           AND client_id = NEW.client_id
           AND contact_id = NEW.contact_id
           AND channel = NEW.channel
           AND purpose = NEW.purpose;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER consents_assign_event_sequence
BEFORE INSERT ON consents
FOR EACH ROW EXECUTE FUNCTION assign_consent_event_sequence();
