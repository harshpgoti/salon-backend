-- TASK-035 / FR-049: allow the tenant API role to remove an invalidated
-- completed-service fact when an owner corrects its appointment state.
-- The visit FK cascades removal to its service snapshots and rebooking cycles.
GRANT DELETE ON visits TO salon_app;
