"""Communication channels module (FR-039–FR-048)."""

from modules.communications.callbacks import (
    CallbackModuleError,
    claim_due_retries,
    ingest_callback,
    queue_retry,
    reclaim_stale_sending,
    requeue_dead_letter,
    sign_callback_body,
    verify_callback_signature,
)
from modules.communications.consent import (
    check_consented,
    check_suppressed,
    normalize_contact_value,
    record_consent,
    record_suppression,
)
from modules.communications.outbox import (
    dispatch_claimed_retries,
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
    "CallbackModuleError",
    "SenderModuleError",
    "check_consented",
    "check_suppressed",
    "claim_due_retries",
    "dispatch_claimed_retries",
    "dispatch_due_communications",
    "get_verified_sender",
    "handle_inbound_sms",
    "ingest_callback",
    "normalize_contact_value",
    "queue_retry",
    "reclaim_stale_sending",
    "record_booking_communication",
    "record_consent",
    "record_message_cost",
    "record_suppression",
    "register_sender",
    "requeue_dead_letter",
    "schedule_booking_reminders",
    "sign_callback_body",
    "suspend_sender",
    "verify_callback_signature",
    "verify_sender",
]
