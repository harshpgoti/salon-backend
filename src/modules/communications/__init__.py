"""Communication channels module (FR-039–FR-048)."""

from apps.api.src.modules.communications.outbox import (
    dispatch_due_communications,
    record_booking_communication,
    schedule_booking_reminders,
)
from apps.api.src.modules.communications.consent import (
    check_suppressed,
    record_consent,
    record_suppression,
)

__all__ = [
    "check_suppressed",
    "dispatch_due_communications",
    "record_booking_communication",
    "record_consent",
    "record_suppression",
    "schedule_booking_reminders",
]