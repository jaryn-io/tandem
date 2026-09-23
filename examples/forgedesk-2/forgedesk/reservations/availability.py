"""High-Performance Availability and Conflict Detection Engine for ForgeDesk Makerspace.

Handles:
- Machine state enforcement (available, temporarily_unavailable, under_maintenance, retired).
- Multi-capacity interval concurrency algorithms (sweep-line peak overlap calculation).
- Daily operating hours boundary validation in Europe/Rome.
- Overlapping maintenance windows and critical out-of-service incidents.
- Member qualification temporal validity and membership status enforcement.
- Timeline slot generation with capacity utilization metrics.
- Preservation and retrieval of historical reservations on retired machines.
"""

import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

from forgedesk.db.connection import query_all, query_one
from forgedesk.utils.datetime_tz import (
    format_display,
    format_iso,
    intervals_overlap,
    is_within_operating_hours,
    now_rome,
    now_rome_iso,
    parse_date_only,
    parse_datetime,
)

logger = logging.getLogger("forgedesk.reservations.availability")


# -----------------------------------------------------------------------------
# Timing & Eligibility Validation Helpers
# -----------------------------------------------------------------------------

def validate_reservation_timing(
    start_time: Union[str, datetime.datetime],
    end_time: Union[str, datetime.datetime],
    op_start_time_str: str = "08:00",
    op_end_time_str: str = "22:00",
    min_duration_minutes: int = 15,
    max_duration_minutes: int = 720,  # 12 hours max single reservation
) -> Tuple[bool, str, Dict[str, Any]]:
    """Validate that reservation start and end times meet workshop operating rules."""
    try:
        s_dt = parse_datetime(start_time)
        e_dt = parse_datetime(end_time)
    except Exception as e:
        return False, f"Invalid date/time format: {e}", {"error_type": "invalid_format"}

    if s_dt >= e_dt:
        return False, "Reservation start time must be strictly earlier than end time.", {
            "error_type": "invalid_interval",
            "start": format_iso(s_dt),
            "end": format_iso(e_dt),
        }

    duration_minutes = int((e_dt - s_dt).total_seconds() // 60)
    if duration_minutes < min_duration_minutes:
        return (
            False,
            f"Reservation duration must be at least {min_duration_minutes} minutes (requested: {duration_minutes} min).",
            {"error_type": "duration_too_short", "duration_minutes": duration_minutes},
        )

    if duration_minutes > max_duration_minutes:
        return (
            False,
            f"Reservation duration cannot exceed {max_duration_minutes // 60} hours (requested: {duration_minutes} min).",
            {"error_type": "duration_too_long", "duration_minutes": duration_minutes},
        )

    if s_dt.date() != e_dt.date():
        return (
            False,
            "Reservations spanning across multiple calendar days are prohibited. Create separate daily bookings.",
            {"error_type": "cross_day", "start_date": str(s_dt.date()), "end_date": str(e_dt.date())},
        )

    in_hours, hours_msg = is_within_operating_hours(
        s_dt,
        e_dt,
        op_start_time_str=op_start_time_str,
        op_end_time_str=op_end_time_str,
    )
    if not in_hours:
        return False, hours_msg, {
            "error_type": "operating_hours",
            "operating_hours_start": op_start_time_str,
            "operating_hours_end": op_end_time_str,
            "message": hours_msg,
        }

    return True, "Valid timing.", {
        "start_iso": format_iso(s_dt),
        "end_iso": format_iso(e_dt),
        "duration_minutes": duration_minutes,
    }


def validate_member_reservation_eligibility(
    member_id: int,
    machine_id: int,
    start_time: Union[str, datetime.datetime],
    end_time: Union[str, datetime.datetime],
    conn: Optional[Any] = None,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """Validate member's account status and qualification temporal validity for a machine."""
    member = query_one(
        """
        SELECT m.id, m.member_number, m.full_name, m.membership_status, m.membership_expiry,
               u.is_active AS user_active
        FROM members m
        LEFT JOIN users u ON u.id = m.user_id
        WHERE m.id = ?;
        """,
        (int(member_id),),
        conn=conn,
    )
    if not member:
        return False, f"Member ID {member_id} does not exist.", [{"type": "member_not_found"}]

    if member.get("user_active") == 0:
        return False, f"Member '{member['full_name']}' user account is deactivated.", [{"type": "user_deactivated"}]

    if member.get("membership_status") != "active":
        status = member.get("membership_status", "unknown")
        return (
            False,
            f"Member '{member['full_name']}' membership is currently '{status}'. Only active members may reserve machines.",
            [{"type": "membership_status_invalid", "status": status}],
        )

    # Check membership expiry against reservation end date
    target_end_dt = parse_datetime(end_time)
    res_date_str = str(target_end_dt.date())
    membership_expiry = member.get("membership_expiry")
    if membership_expiry and membership_expiry < res_date_str:
        return (
            False,
            f"Membership expires on {membership_expiry}, which is before the reservation date ({res_date_str}). Please renew membership first.",
            [{"type": "membership_expired", "expiry_date": membership_expiry, "reservation_date": res_date_str}],
        )

    # Check machine qualification requirements
    machine = query_one(
        """
        SELECT m.id, m.name, m.code, m.category_id, m.required_qualification_category_id,
               c.name AS category_name,
               qc.name AS required_qualification_category_name
        FROM machines m
        LEFT JOIN machine_categories c ON c.id = m.category_id
        LEFT JOIN machine_categories qc ON qc.id = m.required_qualification_category_id
        WHERE m.id = ?;
        """,
        (int(machine_id),),
        conn=conn,
    )
    if not machine:
        return False, f"Machine ID {machine_id} does not exist.", [{"type": "machine_not_found"}]

    # Determine required category
    required_cat_id = machine.get("required_qualification_category_id") or machine.get("category_id")
    if not required_cat_id:
        return True, "No specific qualification required.", []

    cat_name = machine.get("required_qualification_category_name") or machine.get("category_name") or "Required Category"

    # Query qualification record for (member_id, required_cat_id)
    qual = query_one(
        """
        SELECT id, qualification_name, issue_date, expiry_date
        FROM qualifications
        WHERE member_id = ? AND category_id = ?;
        """,
        (int(member_id), int(required_cat_id)),
        conn=conn,
    )

    if not qual:
        return (
            False,
            f"Member '{member['full_name']}' lacks the mandatory qualification for category '{cat_name}'. An operator must certify the member before reservations are permitted.",
            [{"type": "qualification_missing", "category_id": required_cat_id, "category_name": cat_name}],
        )

    # Temporal qualification checks
    target_start_dt = parse_datetime(start_time)
    start_date_str = str(target_start_dt.date())
    issue_date = qual["issue_date"]
    expiry_date = qual.get("expiry_date")

    if issue_date > start_date_str:
        return (
            False,
            f"Qualification '{qual['qualification_name']}' issue date is {issue_date}, which is after the requested reservation date ({start_date_str}).",
            [{"type": "qualification_pending_start", "issue_date": issue_date, "reservation_date": start_date_str}],
        )

    if expiry_date and expiry_date < res_date_str:
        return (
            False,
            f"Qualification '{qual['qualification_name']}' expires on {expiry_date}, before or during the reservation ({res_date_str}). Re-certification required.",
            [{"type": "qualification_expired", "expiry_date": expiry_date, "reservation_date": res_date_str}],
        )

    return True, "Member qualification verified and valid for reservation period.", []


# -----------------------------------------------------------------------------
# Multi-Capacity Concurrency & Interval Overlap Engine
# -----------------------------------------------------------------------------

def calculate_concurrent_utilization(
    machine_id: int,
    start_time: Union[str, datetime.datetime],
    end_time: Union[str, datetime.datetime],
    exclude_reservation_id: Optional[int] = None,
    conn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Calculate peak concurrent reservation utilization and overlapping bookings in interval [start, end).
    
    Uses a sweep-line event algorithm to determine the exact peak concurrent bookings at any instant
    within the requested time window.
    """
    s_dt = parse_datetime(start_time)
    e_dt = parse_datetime(end_time)

    # Retrieve all active reservations for this machine
    query = """
        SELECT r.id, r.machine_id, r.member_id, r.title, r.start_time, r.end_time, r.status,
               m.full_name AS member_name, m.member_number
        FROM reservations r
        JOIN members m ON m.id = r.member_id
        WHERE r.machine_id = ? AND r.status IN ('pending', 'confirmed', 'checked_in', 'late')
    """
    params: List[Any] = [int(machine_id)]
    if exclude_reservation_id is not None:
        query += " AND r.id != ?"
        params.append(int(exclude_reservation_id))

    active_rows = query_all(query, tuple(params), conn=conn)

    # Filter strictly overlapping reservations: max(s, s_i) < min(e, e_i)
    overlapping_reservations: List[Dict[str, Any]] = []
    events: List[Tuple[datetime.datetime, int, Dict[str, Any]]] = []

    for row in active_rows:
        r_start = parse_datetime(row["start_time"])
        r_end = parse_datetime(row["end_time"])

        if intervals_overlap(s_dt, e_dt, r_start, r_end):
            res_info = dict(row)
            overlapping_reservations.append(res_info)

            # Clamp event bounds to the inspected window [s_dt, e_dt)
            effective_start = max(s_dt, r_start)
            effective_end = min(e_dt, r_end)

            # +1 for start event, -1 for end event
            events.append((effective_start, 1, res_info))
            events.append((effective_end, -1, res_info))

    if not events:
        return {
            "peak_concurrent": 0,
            "overlapping_count": 0,
            "overlapping_reservations": [],
            "peak_interval_start": None,
            "peak_interval_end": None,
        }

    # Sort events chronologically:
    # If timestamps are identical, -1 (release) comes before +1 (acquire) because intervals are half-open [start, end)
    events.sort(key=lambda item: (item[0], item[1]))

    current_count = 0
    peak_concurrent = 0
    peak_start: Optional[datetime.datetime] = None
    peak_end: Optional[datetime.datetime] = None
    last_ts = events[0][0]

    for ts, delta, res in events:
        if current_count > peak_concurrent:
            peak_concurrent = current_count
            peak_start = last_ts
            peak_end = ts

        current_count += delta
        last_ts = ts

    # Check final segment if applicable
    if current_count > peak_concurrent:
        peak_concurrent = current_count
        peak_start = last_ts
        peak_end = e_dt

    return {
        "peak_concurrent": peak_concurrent,
        "overlapping_count": len(overlapping_reservations),
        "overlapping_reservations": overlapping_reservations,
        "peak_interval_start": format_iso(peak_start) if peak_start else None,
        "peak_interval_end": format_iso(peak_end) if peak_end else None,
    }


# -----------------------------------------------------------------------------
# Comprehensive Conflict Discovery
# -----------------------------------------------------------------------------

def find_interval_conflicts(
    machine_id: int,
    start_time: Union[str, datetime.datetime],
    end_time: Union[str, datetime.datetime],
    member_id: Optional[int] = None,
    exclude_reservation_id: Optional[int] = None,
    check_qualifications: bool = True,
    conn: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Scan and return all operational, maintenance, capacity, and qualification conflicts."""
    conflicts: List[Dict[str, Any]] = []

    # 1. Machine Existence & State
    machine = query_one("SELECT * FROM machines WHERE id = ?;", (int(machine_id),), conn=conn)
    if not machine:
        conflicts.append({
            "type": "machine_not_found",
            "message": f"Machine with ID {machine_id} does not exist.",
        })
        return conflicts

    machine_state = machine.get("state", "available")
    if machine_state == "retired":
        conflicts.append({
            "type": "machine_retired",
            "state": "retired",
            "message": f"Machine '{machine['name']}' ({machine['code']}) is retired. Historical reservations remain preserved, but no new bookings may be created.",
        })
        return conflicts

    if machine_state == "under_maintenance":
        conflicts.append({
            "type": "machine_under_maintenance",
            "state": "under_maintenance",
            "message": f"Machine '{machine['name']}' is currently under maintenance.",
        })

    if machine_state == "temporarily_unavailable":
        conflicts.append({
            "type": "machine_temporarily_unavailable",
            "state": "temporarily_unavailable",
            "message": f"Machine '{machine['name']}' is temporarily unavailable.",
        })

    # 2. Critical Incidents taking machine out of service
    incidents = query_all(
        """
        SELECT id, title, severity, status, created_at
        FROM incidents
        WHERE machine_id = ? AND takes_machine_out_of_service = 1 AND status IN ('reported', 'investigating')
        ORDER BY created_at DESC;
        """,
        (int(machine_id),),
        conn=conn,
    )
    for inc in incidents:
        conflicts.append({
            "type": "critical_incident",
            "incident_id": inc["id"],
            "title": inc["title"],
            "severity": inc["severity"],
            "status": inc["status"],
            "message": f"Machine is placed out of service by active {inc['severity']} incident #{inc['id']}: '{inc['title']}'.",
        })

    # 2.5 Active in-progress maintenance jobs
    active_jobs = query_all(
        """
        SELECT id, title, priority, status, assigned_to_user_id
        FROM maintenance_jobs
        WHERE machine_id = ? AND status = 'in_progress'
        ORDER BY created_at DESC;
        """,
        (int(machine_id),),
        conn=conn,
    )
    for job in active_jobs:
        conflicts.append({
            "type": "maintenance_job_in_progress",
            "job_id": job["id"],
            "title": job["title"],
            "priority": job["priority"],
            "message": f"Machine has an active in-progress maintenance job #{job['id']}: '{job['title']}' (priority: {job['priority']}).",
        })

    # 3. Timing and Operating Hours
    is_valid_timing, timing_msg, timing_details = validate_reservation_timing(
        start_time=start_time,
        end_time=end_time,
        op_start_time_str=machine.get("operating_hours_start", "08:00"),
        op_end_time_str=machine.get("operating_hours_end", "22:00"),
    )
    if not is_valid_timing:
        conflicts.append({
            "type": "operating_hours_violation",
            "error_type": timing_details.get("error_type", "timing_error"),
            "message": timing_msg,
            "details": timing_details,
        })
        return conflicts

    s_dt = parse_datetime(start_time)
    e_dt = parse_datetime(end_time)

    # 4. Maintenance Windows
    maint_windows = query_all(
        """
        SELECT id, title, start_time, end_time, status, notes
        FROM maintenance_windows
        WHERE machine_id = ? AND status IN ('scheduled', 'in_progress')
        ORDER BY start_time ASC;
        """,
        (int(machine_id),),
        conn=conn,
    )
    for mw in maint_windows:
        mw_start = parse_datetime(mw["start_time"])
        mw_end = parse_datetime(mw["end_time"])
        if intervals_overlap(s_dt, e_dt, mw_start, mw_end):
            conflicts.append({
                "type": "maintenance_window",
                "id": mw["id"],
                "window_id": mw["id"],
                "title": mw["title"],
                "start_time": mw["start_time"],
                "end_time": mw["end_time"],
                "status": mw["status"],
                "message": f"Overlaps with {mw['status']} maintenance window #{mw['id']}: '{mw['title']}' ({format_display(mw['start_time'])} - {format_display(mw['end_time'])}).",
            })

    # 5. Capacity and Concurrent Overlapping Bookings
    capacity = int(machine.get("capacity", 1))
    utilization = calculate_concurrent_utilization(
        machine_id=machine_id,
        start_time=s_dt,
        end_time=e_dt,
        exclude_reservation_id=exclude_reservation_id,
        conn=conn,
    )
    peak = utilization["peak_concurrent"]
    if peak >= capacity:
        conflicts.append({
            "type": "capacity_exceeded",
            "capacity": capacity,
            "peak_concurrent": peak,
            "available_capacity": max(0, capacity - peak),
            "peak_interval_start": utilization["peak_interval_start"],
            "peak_interval_end": utilization["peak_interval_end"],
            "overlapping_reservations": utilization["overlapping_reservations"],
            "message": f"Capacity limit reached: {peak}/{capacity} active reservation slots booked during requested interval.",
        })

    # 6. Member Eligibility and Qualifications (if requested and member_id supplied)
    if check_qualifications and member_id is not None:
        is_eligible, elig_msg, elig_conflicts = validate_member_reservation_eligibility(
            member_id=member_id,
            machine_id=machine_id,
            start_time=s_dt,
            end_time=e_dt,
            conn=conn,
        )
        if not is_eligible:
            for ec in elig_conflicts:
                conflicts.append({
                    "type": ec.get("type", "eligibility_conflict"),
                    "message": elig_msg,
                    "details": ec,
                })

    return conflicts


# -----------------------------------------------------------------------------
# High-Level Availability Check
# -----------------------------------------------------------------------------

def check_machine_availability(
    machine_id: int,
    start_time: Union[str, datetime.datetime],
    end_time: Union[str, datetime.datetime],
    member_id: Optional[int] = None,
    exclude_reservation_id: Optional[int] = None,
    check_qualifications: bool = True,
    conn: Optional[Any] = None,
) -> Tuple[bool, str, List[Dict[str, Any]], Dict[str, Any]]:
    """Comprehensive multi-factor availability check.
    
    Returns:
        (is_available: bool, message: str, conflicts: List[Dict], metadata: Dict)
    """
    machine = query_one(
        """
        SELECT m.id, m.code, m.name, m.capacity, m.state, m.operating_hours_start, m.operating_hours_end,
               m.hourly_rate_cents, m.minimum_charge_cents, m.peak_hourly_rate_cents,
               c.name AS category_name
        FROM machines m
        LEFT JOIN machine_categories c ON c.id = m.category_id
        WHERE m.id = ?;
        """,
        (int(machine_id),),
        conn=conn,
    )

    if not machine:
        return False, f"Machine ID {machine_id} does not exist.", [{"type": "machine_not_found"}], {}

    conflicts = find_interval_conflicts(
        machine_id=machine_id,
        start_time=start_time,
        end_time=end_time,
        member_id=member_id,
        exclude_reservation_id=exclude_reservation_id,
        check_qualifications=check_qualifications,
        conn=conn,
    )

    capacity = int(machine.get("capacity", 1))

    # Calculate utilization metrics safely if timing is valid
    try:
        utilization = calculate_concurrent_utilization(
            machine_id=machine_id,
            start_time=start_time,
            end_time=end_time,
            exclude_reservation_id=exclude_reservation_id,
            conn=conn,
        )
        peak = utilization["peak_concurrent"]
        available_slots = max(0, capacity - peak)
    except Exception:
        peak = 0
        available_slots = capacity

    metadata = {
        "machine_id": machine["id"],
        "machine_code": machine["code"],
        "machine_name": machine["name"],
        "machine_state": machine["state"],
        "total_capacity": capacity,
        "peak_concurrent_booked": peak,
        "available_capacity_slots": available_slots,
        "operating_hours": f"{machine['operating_hours_start']} - {machine['operating_hours_end']}",
        "is_available": len(conflicts) == 0,
        "conflicts_count": len(conflicts),
    }

    if conflicts:
        primary_msg = conflicts[0].get("message", "Requested time is unavailable.")
        return False, primary_msg, conflicts, metadata

    return True, f"Machine '{machine['name']}' is available ({available_slots}/{capacity} slots open).", [], metadata


# -----------------------------------------------------------------------------
# Timeline & Slot Generation Engine
# -----------------------------------------------------------------------------

def generate_timeline_slots(
    machine_id: int,
    date_str: str,
    slot_duration_minutes: int = 30,
    member_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate daily timeline slots with capacity and conflict breakdown for visual calendar and UI."""
    machine = query_one(
        """
        SELECT m.id, m.code, m.name, m.capacity, m.state, m.operating_hours_start, m.operating_hours_end,
               c.name AS category_name
        FROM machines m
        LEFT JOIN machine_categories c ON c.id = m.category_id
        WHERE m.id = ?;
        """,
        (int(machine_id),),
    )
    if not machine:
        raise ValueError(f"Machine with ID {machine_id} does not exist.")

    # Validate date
    target_date = parse_date_only(date_str)
    date_formatted = target_date.strftime("%Y-%m-%d")

    capacity = int(machine.get("capacity", 1))
    op_start_str = machine.get("operating_hours_start", "08:00")
    op_end_str = machine.get("operating_hours_end", "22:00")

    sh, sm = map(int, op_start_str.split(":"))
    eh, em = map(int, op_end_str.split(":"))

    op_start_dt = parse_datetime(f"{date_formatted} {sh:02d}:{sm:02d}:00")
    op_end_dt = parse_datetime(f"{date_formatted} {eh:02d}:{em:02d}:00")

    step_delta = datetime.timedelta(minutes=slot_duration_minutes)
    slots: List[Dict[str, Any]] = []

    # Fetch active reservations for the day
    res_rows = query_all(
        """
        SELECT r.id, r.member_id, r.title, r.start_time, r.end_time, r.status,
               m.full_name AS member_name, m.member_number
        FROM reservations r
        JOIN members m ON m.id = r.member_id
        WHERE r.machine_id = ? AND r.status IN ('pending', 'confirmed', 'checked_in', 'late')
          AND date(r.start_time) <= ? AND date(r.end_time) >= ?
        """,
        (int(machine_id), date_formatted, date_formatted),
    )

    # Fetch maintenance windows for the day
    mw_rows = query_all(
        """
        SELECT id, title, start_time, end_time, status
        FROM maintenance_windows
        WHERE machine_id = ? AND status IN ('scheduled', 'in_progress')
          AND date(start_time) <= ? AND date(end_time) >= ?
        """,
        (int(machine_id), date_formatted, date_formatted),
    )

    # Fetch active out-of-service incidents
    inc_rows = query_all(
        """
        SELECT id, title, severity, status
        FROM incidents
        WHERE machine_id = ? AND takes_machine_out_of_service = 1 AND status IN ('reported', 'investigating')
        """,
        (int(machine_id),),
    )

    # Fetch active in-progress maintenance jobs
    job_rows = query_all(
        """
        SELECT id, title, priority, status
        FROM maintenance_jobs
        WHERE machine_id = ? AND status = 'in_progress'
        """,
        (int(machine_id),),
    )

    curr_slot_start = op_start_dt
    machine_state = machine.get("state", "available")

    while curr_slot_start < op_end_dt:
        curr_slot_end = min(curr_slot_start + step_delta, op_end_dt)
        slot_start_iso = format_iso(curr_slot_start)
        slot_end_iso = format_iso(curr_slot_end)
        slot_time_label = f"{curr_slot_start.strftime('%H:%M')} - {curr_slot_end.strftime('%H:%M')}"

        # Overlapping maintenance windows
        slot_mw = [
            mw for mw in mw_rows
            if intervals_overlap(curr_slot_start, curr_slot_end, mw["start_time"], mw["end_time"])
        ]

        # Overlapping reservations
        slot_res = [
            r for r in res_rows
            if intervals_overlap(curr_slot_start, curr_slot_end, r["start_time"], r["end_time"])
        ]

        booked_count = len(slot_res)
        remaining_capacity = max(0, capacity - booked_count)

        if machine_state == "retired":
            slot_status = "retired"
            is_open = False
        elif machine_state in ("under_maintenance", "temporarily_unavailable"):
            slot_status = machine_state
            is_open = False
        elif inc_rows:
            slot_status = "incident_out_of_service"
            is_open = False
        elif job_rows:
            slot_status = "maintenance_job"
            is_open = False
        elif slot_mw:
            slot_status = "maintenance"
            is_open = False
        elif booked_count >= capacity:
            slot_status = "fully_booked"
            is_open = False
        elif booked_count > 0:
            slot_status = "partially_booked"
            is_open = True
        else:
            slot_status = "available"
            is_open = True

        slots.append({
            "slot_index": len(slots),
            "time_label": slot_time_label,
            "start_time": slot_start_iso,
            "end_time": slot_end_iso,
            "status": slot_status,
            "is_open": is_open,
            "total_capacity": capacity,
            "booked_count": booked_count,
            "remaining_capacity": remaining_capacity,
            "reservations": slot_res,
            "maintenance_windows": slot_mw,
        })

        curr_slot_start = curr_slot_end

    return {
        "machine_id": machine["id"],
        "machine_code": machine["code"],
        "machine_name": machine["name"],
        "machine_state": machine["state"],
        "date": date_formatted,
        "operating_hours_start": op_start_str,
        "operating_hours_end": op_end_str,
        "slot_duration_minutes": slot_duration_minutes,
        "total_slots": len(slots),
        "available_slots_count": sum(1 for s in slots if s["is_open"]),
        "slots": slots,
    }


# -----------------------------------------------------------------------------
# Batch Availability & Historical Preservation
# -----------------------------------------------------------------------------

def get_batch_machine_availability(
    start_time: Union[str, datetime.datetime],
    end_time: Union[str, datetime.datetime],
    category_id: Optional[int] = None,
    member_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Scan and compute availability for all active machines in a category or workshop."""
    query = "SELECT id, code, name, category_id, state, capacity FROM machines WHERE state != 'retired'"
    params: List[Any] = []
    if category_id is not None:
        query += " AND category_id = ?"
        params.append(int(category_id))
    query += " ORDER BY name ASC;"

    machines = query_all(query, tuple(params))
    results: List[Dict[str, Any]] = []

    for m in machines:
        is_avail, msg, conflicts, meta = check_machine_availability(
            machine_id=m["id"],
            start_time=start_time,
            end_time=end_time,
            member_id=member_id,
            check_qualifications=True,
        )
        results.append({
            "machine_id": m["id"],
            "code": m["code"],
            "name": m["name"],
            "category_id": m["category_id"],
            "state": m["state"],
            "capacity": m["capacity"],
            "is_available": is_avail,
            "message": msg,
            "available_capacity_slots": meta.get("available_capacity_slots", 0),
            "conflicts": conflicts,
        })

    return results


def get_historical_reservations_for_machine(
    machine_id: int,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Retrieve historical reservations for a machine, preserving intact audit records even if retired."""
    machine = query_one("SELECT id, code, name, state FROM machines WHERE id = ?;", (int(machine_id),))
    if not machine:
        raise ValueError(f"Machine ID {machine_id} does not exist.")

    total_row = query_one(
        "SELECT COUNT(*) AS total FROM reservations WHERE machine_id = ?;",
        (int(machine_id),),
    )
    total_count = total_row["total"] if total_row else 0

    rows = query_all(
        """
        SELECT r.id, r.machine_id, r.member_id, r.title, r.start_time, r.end_time,
               r.status, r.actual_check_in, r.actual_check_out, r.cancellation_reason,
               r.cancelled_at, r.created_at, r.updated_at,
               m.full_name AS member_name, m.member_number, m.email AS member_email
        FROM reservations r
        JOIN members m ON m.id = r.member_id
        WHERE r.machine_id = ?
        ORDER BY r.start_time DESC
        LIMIT ? OFFSET ?;
        """,
        (int(machine_id), limit, offset),
    )

    return {
        "machine": dict(machine),
        "total_historical_reservations": total_count,
        "limit": limit,
        "offset": offset,
        "reservations": [dict(r) for r in rows],
    }
