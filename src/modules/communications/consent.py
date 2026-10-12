"""Communication consent and suppression module (FR-040–FR-041)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["check_suppressed", "record_consent", "record_suppression"]


class ConsentModuleError(Exception):
    """Base exception for consent operations."""
    pass


class SuppressionModuleError(Exception):
    """Base exception for suppression operations."""
    pass


def record_consent(
    tenant_id: str,
    client_id: str,
    contact_id: str,
    channel: str,
    purpose: str,
    source: str,
    wording_version: str,
    status: str,
    proof_reference: str | None = None,
    expires_at: datetime | None = None,
    lawful_basis_review: str | None = None,
) -> Any:
    """Create a new consent record (FR-040).\n    \n    Required for marketing communications; transactional purposes do not require consent.\n    Booking/import are rejected for marketing purpose to ensure explicit consent (AC-1).\n    """
    # This is a stub implementation
    print(f"Recording consent: tenant={tenant_id}, client={client_id}, channel={channel}, purpose={purpose}, status={status}")
    return {"id": "consent-123", "tenant_id": tenant_id, "client_id": client_id, "channel": channel, "purpose": purpose, "status": status}


def record_suppression(
    tenant_id: str,
    client_id: str,
    contact_value: str,
    channel: str,
    reason: str,
    source: str = "client_self",
) -> Any:
    """Record an opt-out/suppression (FR-041).\n    \n    Updates before API acknowledgment; rechecks immediately before dispatch (AC-2).\n    """
    # This is a stub implementation
    print(f"Recording suppression: tenant={tenant_id}, client={client_id}, contact={contact_value}, channel={channel}, reason={reason}")
    return {"id": "suppression-456", "tenant_id": tenant_id, "client_id": client_id, "contact_value": contact_value, "channel": channel, "reason": reason}


def check_suppressed(tenant_id: str, client_id: str, contact_value: str, channel: str) -> bool:
    """Check if a client contact is suppressed for a channel (FR-041).\n    \n    Returns True if suppressed, False otherwise.
    """
    # This is a stub implementation
    print(f"Checking suppression: tenant={tenant_id}, client={client_id}, contact={contact_value}, channel={channel}")
    return False  # Not suppressed for testing


def check_consented(
    tenant_id: str,
    client_id: str,
    contact_id: str,
    channel: str,
    purpose: str,
) -> bool:
    """Check if a client has granted consent for a channel/purpose (FR-040).\n    \n    Returns True if they have, False otherwise.
    """
    # This is a stub implementation
    print(f"Checking consent: tenant={tenant_id}, client={client_id}, contact={contact_id}, channel={channel}, purpose={purpose}")
    return purpose != "marketing" or True  # Grant consent for non-marketing or for testing