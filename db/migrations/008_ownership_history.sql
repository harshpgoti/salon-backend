-- A membership can be deleted, an identity cannot. `ownership_transfers.from_user_id`
-- previously referenced memberships(tenant_id, user_id) with no ON DELETE action, so
-- once any transfer existed for a person, deactivating them raised a foreign key
-- violation. AC-1 requires deactivation to actually revoke access, and the tenant's
-- ownership history must survive the initiator leaving, so the reference is retargeted
-- at identity_users - the same table `accepted_by_user_id` already points at.
--
-- This drops a tenant-scoped composite foreign key, so the initiator's tenant is no
-- longer enforced by the schema. It is enforced in the application instead: every
-- transfer read and write is filtered by `tenant_id` under the tenant RLS policy, and
-- `accept_ownership_transfer` rejects a transfer whose initiator is not an owner of the
-- tenant being written. `identity_users` carries no RLS of its own, so this foreign key
-- is not RLS-restricted.
ALTER TABLE ownership_transfers DROP CONSTRAINT ownership_transfers_tenant_id_from_user_id_fkey;
ALTER TABLE ownership_transfers
    ADD CONSTRAINT ownership_transfers_from_user_fkey
    FOREIGN KEY (from_user_id) REFERENCES identity_users(id);
CREATE INDEX ownership_transfers_initiator_idx ON ownership_transfers (tenant_id, from_user_id);