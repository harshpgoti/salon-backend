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
from modules.communications.senders import (
    SenderModuleError,
    get_verified_sender,
    handle_inbound_sms,
    record_message_cost,
    register_sender,
    suspend_sender,
    verify_sender,
)

__all__ = [
    "SenderModuleError",
    "check_consented",
    "check_suppressed",
    "dispatch_due_communications",
    "get_verified_sender",
    "handle_inbound_sms",
    "normalize_contact_value",
    "record_booking_communication",
    "record_consent",
    "record_message_cost",
    "record_suppression",
    "register_sender",
    "schedule_booking_reminders",
    "suspend_sender",
    "verify_sender",
]
