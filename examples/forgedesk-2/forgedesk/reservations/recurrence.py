"""Recurrence Engine and Generator for Europe/Rome Makerspace Reservations.

Handles:
- Parsing, normalization, and validation of recurrence rules (daily, weekly, biweekly, monthly).
- Calendar date progression across Europe/Rome Daylight Saving Time (DST) changes (CET <-> CEST).
- Preservation of local wall-clock operating hours regardless of UTC offset shifts.
- Horizon bounds checking (max occurrences, max horizon span).
- Formatting of human-readable recurrence summaries.
"""

import datetime
import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from zoneinfo import ZoneInfo

from forgedesk.config import APP_TIMEZONE, APP_TIMEZONE_NAME
from forgedesk.utils.datetime_tz import (
    format_display,
    format_iso,
    parse_date_only,
    parse_datetime,
)

logger = logging.getLogger("forgedesk.reservations.recurrence")

MAX_RECURRENCE_COUNT = 52  # Max 52 occurrences per series (1 year of weekly bookings)
MAX_RECURRENCE_DAYS_HORIZON = 366  # Max 1 year ahead

WEEKDAY_CODES = {
    "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6,
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
    "MONDAY": 0, "TUESDAY": 1, "WEDNESDAY": 2, "THURSDAY": 3, "FRIDAY": 4, "SATURDAY": 5, "SUNDAY": 6,
}

WEEKDAY_SHORT_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class RecurrenceRuleError(ValueError):
    """Exception raised when a recurrence rule is malformed or violates safety limits."""
    pass


def parse_time_string(val: Union[str, datetime.time]) -> datetime.time:
    """Parse HH:MM string or datetime.time into a validated time object."""
    if isinstance(val, datetime.time):
        return val.replace(second=0, microsecond=0)
    val_str = str(val).strip()
    if "T" in val_str:
        val_str = val_str.split("T")[1]
    if ":" not in val_str:
        raise RecurrenceRuleError(f"Invalid time format: '{val}'. Expected HH:MM.")
    parts = val_str.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError()
        return datetime.time(hour=h, minute=m, second=0, microsecond=0)
    except Exception:
        raise RecurrenceRuleError(f"Invalid time format: '{val}'. Expected HH:MM between 00:00 and 23:59.")


def normalize_weekdays(weekdays_input: Any) -> List[str]:
    """Normalize various weekday representations into a sorted list of unique 2-letter weekday codes (MO..SU)."""
    if not weekdays_input:
        return []

    if isinstance(weekdays_input, (str, int)):
        weekdays_input = [weekdays_input]

    result_set: Set[int] = set()
    for item in weekdays_input:
        if isinstance(item, int):
            if 0 <= item <= 6:
                result_set.add(item)
            else:
                raise RecurrenceRuleError(f"Invalid integer weekday index: {item} (must be 0=Monday to 6=Sunday).")
        elif isinstance(item, str):
            clean = item.strip().upper()
            if clean.isdigit():
                idx = int(clean)
                if 0 <= idx <= 6:
                    result_set.add(idx)
                    continue
            if clean in WEEKDAY_CODES:
                result_set.add(WEEKDAY_CODES[clean])
            else:
                raise RecurrenceRuleError(f"Unknown weekday name or code: '{item}'. Use MO, TU, WE, TH, FR, SA, SU.")
        else:
            raise RecurrenceRuleError(f"Unsupported weekday representation: {type(item)}")

    return [WEEKDAY_SHORT_CODES[i] for i in sorted(list(result_set))]


def parse_recurrence_rule(rule_input: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Parse, validate, and canonicalize a recurrence rule dict or JSON string."""
    if isinstance(rule_input, str):
        rule_input = rule_input.strip()
        if not rule_input:
            raise RecurrenceRuleError("Empty recurrence rule provided.")
        try:
            rule_dict = json.loads(rule_input)
        except Exception:
            # Fallback simple string parser e.g. "weekly" or "freq=weekly;count=4"
            rule_dict = {"frequency": rule_input.lower()}
    elif isinstance(rule_input, dict):
        rule_dict = dict(rule_input)
    else:
        raise RecurrenceRuleError(f"Recurrence rule must be dict or JSON string, got {type(rule_input)}.")

    freq = str(rule_dict.get("frequency") or rule_dict.get("freq") or "weekly").strip().lower()
    if freq in ("bi-weekly", "bi_weekly", "every_2_weeks"):
        freq = "biweekly"

    if freq not in ("daily", "weekly", "biweekly", "monthly"):
        raise RecurrenceRuleError(f"Unsupported recurrence frequency: '{freq}'. Supported: daily, weekly, biweekly, monthly.")

    interval_val = rule_dict.get("interval") if "interval" in rule_dict else 1
    try:
        interval = int(interval_val)
        if interval < 1:
            raise RecurrenceRuleError(f"Interval must be at least 1, got {interval}.")
    except RecurrenceRuleError:
        raise
    except Exception as e:
        raise RecurrenceRuleError(f"Invalid interval: {interval_val}") from e

    if freq == "biweekly":
        interval = max(interval, 2)

    # Weekdays
    raw_weekdays = rule_dict.get("weekdays") if "weekdays" in rule_dict else (rule_dict.get("byweekday") if "byweekday" in rule_dict else rule_dict.get("days"))
    weekdays = normalize_weekdays(raw_weekdays)

    # Count / Occurrences
    count_val = rule_dict.get("count") if "count" in rule_dict else rule_dict.get("occurrences")
    count: Optional[int] = None
    if count_val is not None:
        try:
            count = int(count_val)
            if count < 1:
                raise RecurrenceRuleError(f"Recurrence count must be positive integer, got {count}.")
            if count > MAX_RECURRENCE_COUNT:
                raise RecurrenceRuleError(f"Recurrence count exceeds safety limit of {MAX_RECURRENCE_COUNT} occurrences.")
        except RecurrenceRuleError:
            raise
        except Exception as e:
            raise RecurrenceRuleError(f"Invalid count: {count_val}") from e

    # Until date
    until_str = rule_dict.get("until") or rule_dict.get("until_date") or rule_dict.get("end_date")
    until_date: Optional[str] = None
    if until_str:
        try:
            parsed_until = parse_date_only(str(until_str))
            until_date = parsed_until.strftime("%Y-%m-%d")
        except Exception as e:
            raise RecurrenceRuleError(f"Invalid until date format: '{until_str}'. Expected YYYY-MM-DD.") from e

    if count is None and until_date is None:
        count = 4  # Default to 4 occurrences if neither count nor until is specified

    canonical: Dict[str, Any] = {
        "frequency": freq,
        "interval": interval,
        "weekdays": weekdays,
        "count": count,
        "until": until_date,
    }
    return canonical


def format_recurrence_summary(rule: Dict[str, Any]) -> str:
    """Generate a clean human-readable summary of a recurrence rule."""
    freq = rule.get("frequency", "weekly")
    interval = rule.get("interval", 1)
    weekdays = rule.get("weekdays", [])
    count = rule.get("count")
    until = rule.get("until")

    wd_str = ""
    if weekdays:
        names = []
        for w in weekdays:
            if isinstance(w, str) and w.upper() in WEEKDAY_CODES:
                names.append(WEEKDAY_SHORT[WEEKDAY_CODES[w.upper()]])
            elif isinstance(w, int) and 0 <= w <= 6:
                names.append(WEEKDAY_SHORT[w])
        if names:
            wd_str = f" on {', '.join(names)}"

    if freq == "daily":
        freq_str = "Daily" if interval == 1 else f"Every {interval} days"
    elif freq == "weekly":
        freq_str = "Weekly" if interval == 1 else f"Every {interval} weeks"
    elif freq == "biweekly":
        freq_str = "Bi-weekly"
    elif freq == "monthly":
        freq_str = "Monthly" if interval == 1 else f"Every {interval} months"
    else:
        freq_str = freq.title()

    limit_str = ""
    if count:
        limit_str = f" ({count} occurrences)"
    elif until:
        limit_str = f" (until {until})"

    return f"{freq_str}{wd_str}{limit_str}"


def _get_days_in_month(year: int, month: int) -> int:
    """Return total number of days in a given calendar month."""
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 31
    if month in (4, 6, 9, 11):
        return 30
    if month == 2:
        is_leap = (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
        return 29 if is_leap else 28
    return 30


def generate_occurrence_datetimes(
    start_date: Union[str, datetime.date],
    start_time_of_day: Union[str, datetime.time],
    end_time_of_day: Union[str, datetime.time],
    recurrence_rule: Union[str, Dict[str, Any]],
) -> List[Tuple[datetime.datetime, datetime.datetime]]:
    """Generate localized Europe/Rome datetime interval pairs for a recurring reservation.
    
    CRITICAL TIMEZONE & DST INVARIANT:
    All occurrences preserve the exact Europe/Rome local wall-clock time (e.g. 10:00 to 12:00)
    regardless of whether the date falls in Central European Time (CET, UTC+1) or Central European
    Summer Time (CEST, UTC+2).
    
    Returns:
        List of (occurrence_start_dt, occurrence_end_dt) localized in Europe/Rome.
    """
    rule = parse_recurrence_rule(recurrence_rule)
    if isinstance(start_date, str):
        initial_date = parse_date_only(start_date)
    elif isinstance(start_date, datetime.datetime):
        initial_date = start_date.date()
    else:
        initial_date = start_date

    time_start = parse_time_string(start_time_of_day)
    time_end = parse_time_string(end_time_of_day)

    if time_start >= time_end:
        raise RecurrenceRuleError(f"Start time ({time_start.strftime('%H:%M')}) must be before end time ({time_end.strftime('%H:%M')}).")

    freq = rule["frequency"]
    interval = rule["interval"]
    weekdays = rule["weekdays"]
    max_count = rule.get("count") or MAX_RECURRENCE_COUNT
    until_date_obj: Optional[datetime.date] = parse_date_only(rule["until"]) if rule.get("until") else None

    # Default weekday if none provided for weekly/biweekly
    if freq in ("weekly", "biweekly") and not weekdays:
        weekdays = [WEEKDAY_SHORT_CODES[initial_date.weekday()]]

    wd_integers: List[int] = []
    for w in weekdays:
        if isinstance(w, str) and w.upper() in WEEKDAY_CODES:
            wd_integers.append(WEEKDAY_CODES[w.upper()])
        elif isinstance(w, int) and 0 <= w <= 6:
            wd_integers.append(w)

    matched_dates: List[datetime.date] = []
    horizon_limit = initial_date + datetime.timedelta(days=MAX_RECURRENCE_DAYS_HORIZON)

    if freq == "daily":
        curr_d = initial_date
        while len(matched_dates) < max_count and curr_d <= horizon_limit:
            if until_date_obj and curr_d > until_date_obj:
                break
            matched_dates.append(curr_d)
            curr_d += datetime.timedelta(days=interval)

    elif freq in ("weekly", "biweekly"):
        # We step week by week starting from the Monday of initial_date's week
        monday_of_first_week = initial_date - datetime.timedelta(days=initial_date.weekday())
        week_idx = 0
        step_weeks = interval if freq == "weekly" else max(2, interval)

        while len(matched_dates) < max_count:
            curr_monday = monday_of_first_week + datetime.timedelta(weeks=week_idx * step_weeks)
            if curr_monday > horizon_limit:
                break

            for wd in sorted(wd_integers):
                d = curr_monday + datetime.timedelta(days=wd)
                if d < initial_date:
                    continue
                if until_date_obj and d > until_date_obj:
                    break
                if d > horizon_limit:
                    break
                matched_dates.append(d)
                if len(matched_dates) >= max_count:
                    break

            if until_date_obj and curr_monday > until_date_obj:
                break

            week_idx += 1

    elif freq == "monthly":
        curr_year = initial_date.year
        curr_month = initial_date.month
        target_day = initial_date.day

        while len(matched_dates) < max_count:
            max_days = _get_days_in_month(curr_year, curr_month)
            day_to_use = min(target_day, max_days)
            d = datetime.date(curr_year, curr_month, day_to_use)

            if d >= initial_date:
                if until_date_obj and d > until_date_obj:
                    break
                if d > horizon_limit:
                    break
                matched_dates.append(d)

            # Advance by interval months
            curr_month += interval
            while curr_month > 12:
                curr_month -= 12
                curr_year += 1

            if d > horizon_limit:
                break

    # Build Europe/Rome localized datetime intervals
    result_intervals: List[Tuple[datetime.datetime, datetime.datetime]] = []
    for d in matched_dates:
        # Construct naive datetimes in Europe/Rome local time, then replace tzinfo with APP_TIMEZONE
        dt_start = datetime.datetime.combine(d, time_start).replace(tzinfo=APP_TIMEZONE)
        dt_end = datetime.datetime.combine(d, time_end).replace(tzinfo=APP_TIMEZONE)
        result_intervals.append((dt_start, dt_end))

    return result_intervals
