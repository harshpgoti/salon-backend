-- Give consent changes a durable order even when multiple events share a
-- transaction timestamp. PostgreSQL now() is stable within a transaction, so
-- timestamps plus ctid cannot reliably distinguish grant and revocation.
ALTER TABLE consents ADD COLUMN event_sequence bigint;

WITH ordered AS (
    SELECT tenant_id, id,
           row_number() OVER (
               PARTITION BY tenant_id, client_id, contact_id, channel, purpose
               ORDER BY granted_at, created_at, ctid
           ) AS event_sequence
    FROM consents
)
UPDATE consents c
SET event_sequence = ordered.event_sequence
FROM ordered
WHERE ordered.tenant_id = c.tenant_id AND ordered.id = c.id;

ALTER TABLE consents ALTER COLUMN event_sequence SET NOT NULL;
CREATE UNIQUE INDEX consents_event_sequence_idx
    ON consents (tenant_id, client_id, contact_id, channel, purpose, event_sequence);
