-- TASK-034 / FR-048: durable synthetic staff replies on the shared outbox.
-- Body exists only until the recording/approved provider adapter consumes it;
-- sensitive-data and live-provider gates remain closed.
ALTER TABLE communications DROP CONSTRAINT communications_source_type_check;
ALTER TABLE communications ADD CONSTRAINT communications_source_type_check
    CHECK (source_type IN ('appointment', 'location_broadcast', 'rebooking_cycle',
                           'campaign', 'inbox_task'));

ALTER TABLE communications ADD COLUMN reply_body text;
ALTER TABLE communications ADD COLUMN reply_idempotency_key text;
ALTER TABLE communications ADD COLUMN reply_request_hash text;
ALTER TABLE communications ADD COLUMN reply_actor_id uuid;
ALTER TABLE communications ADD CONSTRAINT communications_inbox_reply_fields_check
    CHECK (
      (source_type = 'inbox_task' AND purpose = 'inbox_reply'
       AND channel IN ('sms', 'whatsapp')
       AND reply_body IS NOT NULL AND length(reply_body) BETWEEN 1 AND 2000
       AND reply_idempotency_key IS NOT NULL
       AND reply_request_hash IS NOT NULL
       AND reply_request_hash ~ '^[0-9a-f]{64}$' AND reply_actor_id IS NOT NULL
       AND template_id IS NULL)
      OR
      (source_type <> 'inbox_task' AND reply_body IS NULL
       AND reply_idempotency_key IS NULL
       AND reply_request_hash IS NULL AND reply_actor_id IS NULL)
    );
CREATE UNIQUE INDEX communications_inbox_reply_key_idx
    ON communications (tenant_id, source_id, reply_idempotency_key)
    WHERE source_type='inbox_task';

DELETE FROM follow_up_tasks t USING inbound_messages m
WHERE t.tenant_id=m.tenant_id AND t.source_id=m.id
  AND t.source_type='inbound_message'
  AND (SELECT count(DISTINCT c.client_id) FROM client_contacts c
       WHERE c.tenant_id=m.tenant_id
         AND lower(c.normalized_value)=lower(m.from_value)) <> 1;

UPDATE follow_up_tasks SET detail = CASE kind
    WHEN 'opt_out' THEN 'Client opted out; message content hidden'
    WHEN 'help_request' THEN 'Client asked for help; message content hidden'
    ELSE 'Client replied; message content hidden'
END
WHERE source_type='inbound_message';
