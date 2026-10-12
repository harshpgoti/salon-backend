"""Communication channels module (FR-039–FR-048)."""

from modules.communications.consent import (
    check_consented,
    check_suppressed,
    normalize_contact_value,
    record_consent,
    record_suppression,
)
from modules.communications.outbox import (
    dispatch_due_communications,
    record_booking_communication,
    schedule_booking_reminders,
)

__all__ = [
    "check_consented",
    "check_suppressed",
    "dispatch_due_communications",
    "normalize_contact_value",
    "record_booking_communication",
    "record_consent",
    "record_suppression",
    "schedule_booking_reminders",
]
