-- Booking consent must survive unrelated salon branding/settings edits. The former
-- settings_version covered every settings write, so it is not a stable policy version.
ALTER TABLE tenants
    ADD COLUMN booking_policy_version integer NOT NULL DEFAULT 1
        CHECK (booking_policy_version >= 1);

-- Existing appointments were created against settings_version; preserve that
-- acceptance snapshot as the initial policy version during the transition.
UPDATE tenants SET booking_policy_version = settings_version;

-- Prior releases stored the broad settings version on an appointment. The old
-- configuration history was not retained, so normalize existing snapshots to
-- this migration baseline; all subsequent booking-policy changes are versioned
-- independently from branding and other salon settings.
UPDATE appointments a
SET policy_version = t.booking_policy_version
FROM tenants t
WHERE t.id = a.tenant_id;
