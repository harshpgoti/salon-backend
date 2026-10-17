"""Local wall-clock parsing and localization shared by scheduling modules."""

from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo


def clock_minutes(value: str, *, allow_end_of_day: bool = False) -> int:
    """Parse HH:MM; 24:00 is permitted only for an interval's end."""
    if allow_end_of_day and value == "24:00":
        return 1440
    if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None:
        raise ValueError("invalid local clock")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def local_instant(day: date, minutes: int, zone: ZoneInfo) -> datetime | None:
    """Resolve a local minute, rejecting gaps and choosing the first repeated instant."""
    if not isinstance(minutes, int) or not 0 <= minutes <= 1440:
        return None
    try:
        naive = datetime.combine(day, time.min) + timedelta(minutes=minutes)
        candidate = naive.replace(tzinfo=zone, fold=0)
        returned = candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        return candidate if returned == naive else None
    except (ValueError, OverflowError):
        return None
