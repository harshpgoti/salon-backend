-- TASK-032 / FR-046: keep generated fallback messages linked to their source
-- communication while preserving the original booking/campaign/cycle source.
ALTER TABLE communications
    ADD COLUMN fallback_of uuid,
    ADD CONSTRAINT communications_fallback_parent_fk
        FOREIGN KEY (tenant_id, fallback_of)
        REFERENCES communications (tenant_id, id) ON DELETE CASCADE,
    ADD CONSTRAINT communications_fallback_not_self
        CHECK (fallback_of IS NULL OR fallback_of <> id);

CREATE INDEX communications_fallback_parent_idx
    ON communications (tenant_id, fallback_of)
    WHERE fallback_of IS NOT NULL;
