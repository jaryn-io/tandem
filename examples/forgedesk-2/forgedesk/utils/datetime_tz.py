"""Timezone and datetime utilities for Europe/Rome operations."""

import datetime
from typing import Optional, Tuple, Union
from zoneinfo import ZoneInfo

from forgedesk.config import APP_TIMEZONE, APP_TIMEZONE_NAME


def now_rome() -> datetime.datetime:
    """Return the current datetime localized in Europe/Rome."""
    return datetime.datetime.now(APP_TIMEZONE)


def now_rome_iso() -> str:
    """Return current Europe/Rome timestamp in ISO 8601 format."""
    return now_rome().isoformat()


def today_rome_str() -> str:
    """Return today's date in Europe/Rome formatted as YYYY-MM-DD."""
    return now_rome().strftime("%Y-%m-%d")


def parse_datetime(val: Union[str, datetime.datetime]) -> datetime.datetime:
    """Parse string or naive/aware datetime into Europe/Rome localized datetime."""
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=APP_TIMEZONE)
        return val.astimezone(APP_TIMEZONE)

    val_str = str(val).strip()
    if not val_str:
        raise ValueError("Cannot parse empty datetime string")

    # Handle standard ISO formats
    try:
        dt = datetime.datetime.fromisoformat(val_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=APP_TIMEZONE)
        return dt.astimezone(APP_TIMEZONE)
    except ValueError:
        pass

    # Try common fallback patterns
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(val_str, fmt)
            return dt.replace(tzinfo=APP_TIMEZONE)
        except ValueError:
            continue

    raise ValueError(f"Invalid datetime format for Europe/Rome: {val_str}")


def format_iso(val: Union[str, datetime.datetime]) -> str:
    """Format any valid datetime or string into canonical Europe/Rome ISO 8601."""
    dt = parse_datetime(val)
    return dt.isoformat()


def format_display(val: Optional[Union[str, datetime.datetime]], include_tz: bool = True) -> str:
    """Format datetime for human readable user interface display."""
    if not val:
        return "—"
    try:
        dt = parse_datetime(val)
        tz_suffix = f" {dt.strftime('%Z')}" if include_tz else ""
        return dt.strftime("%Y-%m-%d %H:%M") + tz_suffix
    except Exception:
        return str(val)


def parse_date_only(val: str) -> datetime.date:
    """Parse date string YYYY-MM-DD."""
    val_str = str(val).strip()
    return datetime.datetime.strptime(val_str, "%Y-%m-%d").date()


def is_date_valid_in_range(target_dt: datetime.datetime, issue_date_str: str, expiry_date_str: Optional[str]) -> bool:
    """Check if qualification or certificate is valid at a given datetime."""
    target_date = target_dt.date() if isinstance(target_dt, datetime.datetime) else parse_datetime(target_dt).date()
    issue_date = parse_date_only(issue_date_str)
    if target_date < issue_date:
        return False
    if expiry_date_str:
        expiry_date = parse_date_only(expiry_date_str)
        if target_date > expiry_date:
            return False
    return True


def intervals_overlap(
    start1: Union[str, datetime.datetime],
    end1: Union[str, datetime.datetime],
    start2: Union[str, datetime.datetime],
    end2: Union[str, datetime.datetime],
) -> bool:
    """Determine whether two time intervals overlap (half-open [start, end))."""
    s1 = parse_datetime(start1)
    e1 = parse_datetime(end1)
    s2 = parse_datetime(start2)
    e2 = parse_datetime(end2)
    return max(s1, s2) < min(e1, e2)


def is_within_operating_hours(
    start_dt: Union[str, datetime.datetime],
    end_dt: Union[str, datetime.datetime],
    op_start_time_str: str = "08:00",
    op_end_time_str: str = "22:00",
) -> Tuple[bool, str]:
    """Check if interval falls entirely within machine daily operating hours."""
    s = parse_datetime(start_dt)
    e = parse_datetime(end_dt)

    if s >= e:
        return False, "Reservation start time must be before end time."

    if s.date() != e.date():
        return False, "Reservations spanning across multiple calendar days are not permitted."

    # Parse HH:MM operating bounds
    try:
        sh, sm = map(int, op_start_time_str.split(":"))
        eh, em = map(int, op_end_time_str.split(":"))
    except Exception:
        sh, sm = 8, 0
        eh, em = 22, 0

    day_open = s.replace(hour=sh, minute=sm, second=0, microsecond=0)
    day_close = s.replace(hour=eh, minute=em, second=0, microsecond=0)

    if s < day_open or e > day_close:
        return False, f"Interval must be within operating hours ({op_start_time_str} - {op_end_time_str} Europe/Rome)."

    return True, "OK"


def calculate_peak_offpeak_minutes(
    start_dt: Union[str, datetime.datetime],
    end_dt: Union[str, datetime.datetime],
    peak_start_str: Optional[str] = "17:00",
    peak_end_str: Optional[str] = "21:00",
) -> Tuple[int, int]:
    """Calculate exact duration in minutes split into peak and off-peak periods."""
    s = parse_datetime(start_dt)
    e = parse_datetime(end_dt)
    if s >= e:
        return 0, 0

    total_minutes = int((e - s).total_seconds() / 60)
    if not peak_start_str or not peak_end_str:
        return 0, total_minutes

    try:
        ps_h, ps_m = map(int, peak_start_str.split(":"))
        pe_h, pe_m = map(int, peak_end_str.split(":"))
    except Exception:
        return 0, total_minutes

    peak_start = s.replace(hour=ps_h, minute=ps_m, second=0, microsecond=0)
    peak_end = s.replace(hour=pe_h, minute=pe_m, second=0, microsecond=0)

    overlap_start = max(s, peak_start)
    overlap_end = min(e, peak_end)

    if overlap_start < overlap_end:
        peak_minutes = int((overlap_end - overlap_start).total_seconds() / 60)
    else:
        peak_minutes = 0

    offpeak_minutes = max(0, total_minutes - peak_minutes)
    return peak_minutes, offpeak_minutes
