-- TASK-034 / FR-048: minimal inbound inbox and staff follow-up tasks.
--
--   follow_up_tasks - one row per staff-actionable item. Source-linked
--                     (inbound message, closure impact, ...) with a unique key
--                     per source so reprocessing never duplicates a task.
--                     Status open -> done/dismissed with who/when/why.
--
--   inbound_messages gains the raw body: the list API returns only a 140-char
--   triage preview (redaction), while the full text stays in the row for the
--   client callback the task drives.

CREATE TABLE follow_up_tasks (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    -- The client this task is about, when the sender number resolved to one.
    -- Null for unknown senders: staff still sees the number, not a guess.
    client_id uuid,
    -- opt_out | help_request | inbound_reply | closure_follow_up
    kind text NOT NULL CHECK (kind IN ('opt_out', 'help_request', 'inbound_reply',
                                       'closure_follow_up')),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
    detail text NOT NULL DEFAULT '',
    -- What created the task: ('inbound_message', <id>) etc. Unique per source.
    source_type text NOT NULL,
    source_id uuid,
    -- open -> done | dismissed, with who/when/why recorded.
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'done', 'dismissed')),
    resolved_by uuid,
    resolved_at timestamptz,
    resolution_note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, source_type, source_id),
    FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE inbound_messages ADD COLUMN body text NOT NULL DEFAULT '';

ALTER TABLE follow_up_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE follow_up_tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY follow_up_tasks_tenant_scope ON follow_up_tasks
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON follow_up_tasks TO salon_app;

CREATE INDEX follow_up_tasks_tenant_status_idx
    ON follow_up_tasks (tenant_id, status) WHERE status = 'open';
CREATE INDEX follow_up_tasks_tenant_client_idx ON follow_up_tasks (tenant_id, client_id);
