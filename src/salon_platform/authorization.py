"""Shared tenant-role and assigned-location authorization rules."""
from fastapi import HTTPException


def require_assigned_location(role: str, assigned: list, location_id) -> None:
    """Owners may span a tenant; every other role needs explicit location assignment."""
    if role != "owner" and location_id not in assigned:
        raise HTTPException(status_code=404, detail={"code": "location_not_found"})
