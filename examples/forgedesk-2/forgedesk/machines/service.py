"""Machines, Operating Hours, Qualifications, Maintenance Windows, Jobs, and Incidents management services for ForgeDesk."""

import datetime
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

from forgedesk.audit.service import record_audit_event
from forgedesk.config import (
    ALLOWED_UPLOAD_EXTENSIONS,
    MAX_UPLOAD_SIZE_BYTES,
    UPLOAD_DIR,
    ensure_directories,
)
from forgedesk.core.http import UploadedFile
from forgedesk.db.connection import get_connection, query_all, query_one, transaction
from forgedesk.utils.datetime_tz import (
    format_display,
    intervals_overlap,
    is_within_operating_hours,
    now_rome,
    now_rome_iso,
    parse_datetime,
)

logger = logging.getLogger("forgedesk.machines.service")

VALID_MACHINE_STATES = ("available", "temporarily_unavailable", "under_maintenance", "retired")
VALID_MAINTENANCE_STATUSES = ("scheduled", "in_progress", "completed", "cancelled")
VALID_MAINTENANCE_PRIORITIES = ("low", "medium", "high", "critical")
VALID_MAINTENANCE_JOB_STATUSES = ("open", "in_progress", "completed", "cancelled")
VALID_INCIDENT_SEVERITIES = ("minor", "major", "critical")
VALID_INCIDENT_STATUSES = ("reported", "investigating", "resolved", "closed")

FORBIDDEN_ATTACHMENT_EXTENSIONS = {
    ".exe", ".sh", ".py", ".php", ".phtml", ".js", ".html", ".htm", ".svg",
    ".bat", ".cmd", ".vbs", ".elf", ".pl", ".cgi", ".com", ".scr", ".jar",
    ".bin", ".dll", ".so", ".dylib", ".asp", ".aspx", ".jsp"
}


def validate_machine_code(code: Optional[str]) -> str:
    """Validate and normalize machine identifier code (e.g. PRUSA-MK4-01)."""
    if not code or not code.strip():
        raise ValueError("Machine code cannot be empty.")
    clean = code.strip().upper()
    if not re.match(r"^[A-Z0-9_-]{2,32}$", clean):
        raise ValueError(
            "Machine code must be 2-32 characters and contain only uppercase letters, numbers, hyphens, and underscores."
        )
    return clean


def validate_time_str(time_str: Optional[str], field_name: str = "Time") -> str:
    """Validate time string in HH:MM (24-hour) format."""
    if not time_str or not time_str.strip():
        raise ValueError(f"{field_name} cannot be empty.")
    clean = time_str.strip()
    match = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", clean)
    if not match:
        raise ValueError(f"{field_name} must be in HH:MM format (00:00 to 23:59), got '{time_str}'.")
    return clean


def validate_operating_hours(start_time: str, end_time: str) -> Tuple[str, str]:
    """Validate daily operating hours start and end bounds."""
    s = validate_time_str(start_time, "Operating hours start")
    e = validate_time_str(end_time, "Operating hours end")
    sh, sm = map(int, s.split(":"))
    eh, em = map(int, e.split(":"))
    if (sh * 60 + sm) >= (eh * 60 + em):
        raise ValueError(f"Operating hours start ({s}) must be strictly earlier than operating hours end ({e}).")
    return s, e


def validate_cents(val: Any, field_name: str = "Rate") -> int:
    """Validate that a monetary value is a non-negative integer of cents."""
    if val is None or val == "":
        return 0
    try:
        cents = int(val)
        if cents < 0:
            raise ValueError(f"{field_name} cannot be negative.")
        return cents
    except (ValueError, TypeError) as err:
        raise ValueError(f"{field_name} must be a valid non-negative integer in cents (got '{val}').") from err


# -----------------------------------------------------------------------------
# Machine Queries
# -----------------------------------------------------------------------------

def list_machines(
    category_id: Optional[int] = None,
    state: Optional[str] = None,
    search: Optional[str] = None,
    include_retired: bool = True,
    order_by: str = "m.name ASC",
) -> List[Dict[str, Any]]:
    """List makerspace machines with category info, maintenance counts, and rates."""
    query = """
        SELECT m.id, m.code, m.name, m.category_id, m.required_qualification_category_id,
               m.capacity, m.state, m.operating_hours_start, m.operating_hours_end,
               m.hourly_rate_cents, m.minimum_charge_cents, m.peak_hourly_rate_cents,
               m.peak_hours_start, m.peak_hours_end, m.location, m.description,
               m.created_at, m.updated_at,
               c.code AS category_code, c.name AS category_name,
               qc.code AS required_qualification_category_code, qc.name AS required_qualification_category_name,
               (
                   SELECT COUNT(*) FROM maintenance_windows mw
                   WHERE mw.machine_id = m.id AND mw.status IN ('scheduled', 'in_progress')
               ) AS active_maintenance_windows_count,
               (
                   SELECT COUNT(*) FROM reservations r
                   WHERE r.machine_id = m.id AND r.status IN ('pending', 'confirmed', 'checked_in')
               ) AS active_reservations_count,
               (
                   SELECT COUNT(*) FROM reservations r
                   WHERE r.machine_id = m.id
               ) AS total_reservations_count
        FROM machines m
        LEFT JOIN machine_categories c ON c.id = m.category_id
        LEFT JOIN machine_categories qc ON qc.id = m.required_qualification_category_id
        WHERE 1=1
    """
    params: List[Any] = []

    if not include_retired:
        query += " AND m.state != 'retired'"

    if state and state.strip() and state.strip().lower() in VALID_MACHINE_STATES:
        query += " AND m.state = ?"
        params.append(state.strip().lower())

    if category_id is not None:
        query += " AND m.category_id = ?"
        params.append(int(category_id))

    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query += """ AND (
            LOWER(m.code) LIKE ? OR
            LOWER(m.name) LIKE ? OR
            LOWER(COALESCE(m.location, '')) LIKE ? OR
            LOWER(COALESCE(c.name, '')) LIKE ?
        )"""
        params.extend([term, term, term, term])

    # Whitelist sort ordering
    safe_orders = {
        "m.name ASC": "m.name ASC",
        "m.name DESC": "m.name DESC",
        "m.code ASC": "m.code ASC",
        "m.code DESC": "m.code DESC",
        "m.state ASC": "m.state ASC",
        "m.created_at DESC": "m.created_at DESC",
    }
    order_clause = safe_orders.get(order_by, "m.name ASC")
    query += f" ORDER BY {order_clause};"

    return query_all(query, tuple(params))


def get_machine_by_id(machine_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve full machine record by primary key."""
    query = """
        SELECT m.id, m.code, m.name, m.category_id, m.required_qualification_category_id,
               m.capacity, m.state, m.operating_hours_start, m.operating_hours_end,
               m.hourly_rate_cents, m.minimum_charge_cents, m.peak_hourly_rate_cents,
               m.peak_hours_start, m.peak_hours_end, m.location, m.description,
               m.created_at, m.updated_at,
               c.code AS category_code, c.name AS category_name,
               qc.code AS required_qualification_category_code, qc.name AS required_qualification_category_name,
               (
                   SELECT COUNT(*) FROM maintenance_windows mw
                   WHERE mw.machine_id = m.id AND mw.status IN ('scheduled', 'in_progress')
               ) AS active_maintenance_windows_count,
               (
                   SELECT COUNT(*) FROM reservations r
                   WHERE r.machine_id = m.id AND r.status IN ('pending', 'confirmed', 'checked_in')
               ) AS active_reservations_count,
               (
                   SELECT COUNT(*) FROM reservations r
                   WHERE r.machine_id = m.id
               ) AS total_reservations_count
        FROM machines m
        LEFT JOIN machine_categories c ON c.id = m.category_id
        LEFT JOIN machine_categories qc ON qc.id = m.required_qualification_category_id
        WHERE m.id = ?;
    """
    return query_one(query, (int(machine_id),))


def get_machine_by_code(code: str) -> Optional[Dict[str, Any]]:
    """Retrieve machine by unique code (case-insensitive)."""
    clean_code = str(code).strip().upper()
    query = """
        SELECT m.id, m.code, m.name, m.category_id, m.required_qualification_category_id,
               m.capacity, m.state, m.operating_hours_start, m.operating_hours_end,
               m.hourly_rate_cents, m.minimum_charge_cents, m.peak_hourly_rate_cents,
               m.peak_hours_start, m.peak_hours_end, m.location, m.description,
               m.created_at, m.updated_at,
               c.code AS category_code, c.name AS category_name
        FROM machines m
        LEFT JOIN machine_categories c ON c.id = m.category_id
        WHERE UPPER(m.code) = ?;
    """
    return query_one(query, (clean_code,))


def get_machines_summary_stats() -> Dict[str, int]:
    """Retrieve aggregate machine counts by operational state."""
    stats = {
        "total": 0,
        "available": 0,
        "temporarily_unavailable": 0,
        "under_maintenance": 0,
        "retired": 0,
    }
    rows = query_all("SELECT state, COUNT(*) AS count FROM machines GROUP BY state;")
    for r in rows:
        st = r["state"]
        c = r["count"]
        stats["total"] += c
        if st in stats:
            stats[st] = c
    return stats


# -----------------------------------------------------------------------------
# Machine Mutations
# -----------------------------------------------------------------------------

def create_machine(
    code: str,
    name: str,
    category_id: int,
    required_qualification_category_id: Optional[int] = None,
    capacity: int = 1,
    state: str = "available",
    operating_hours_start: str = "08:00",
    operating_hours_end: str = "22:00",
    hourly_rate_cents: int = 0,
    minimum_charge_cents: int = 0,
    peak_hourly_rate_cents: int = 0,
    peak_hours_start: Optional[str] = "17:00",
    peak_hours_end: Optional[str] = "21:00",
    location: Optional[str] = None,
    description: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new machine in the makerspace database with validation and audit trail."""
    clean_code = validate_machine_code(code)
    if not name or not name.strip():
        raise ValueError("Machine name cannot be empty.")
    clean_name = name.strip()

    # Verify category exists
    cat = query_one("SELECT id, name FROM machine_categories WHERE id = ?;", (int(category_id),))
    if not cat:
        raise ValueError(f"Machine category ID {category_id} does not exist.")

    # Verify required qualification category if given
    req_cat_id: Optional[int] = None
    if required_qualification_category_id:
        req_cat = query_one(
            "SELECT id, name FROM machine_categories WHERE id = ?;",
            (int(required_qualification_category_id),),
        )
        if not req_cat:
            raise ValueError(f"Required qualification category ID {required_qualification_category_id} does not exist.")
        req_cat_id = int(required_qualification_category_id)

    # Validate capacity
    try:
        cap_val = int(capacity)
        if cap_val < 1:
            raise ValueError("Machine capacity must be a positive integer (at least 1).")
    except (ValueError, TypeError) as err:
        raise ValueError(f"Machine capacity must be a positive integer (at least 1) (got '{capacity}').") from err

    # Validate state
    clean_state = str(state).strip().lower()
    if clean_state not in VALID_MACHINE_STATES:
        raise ValueError(f"Invalid machine state '{state}'. Must be one of: {', '.join(VALID_MACHINE_STATES)}.")

    # Validate operating hours
    op_start, op_end = validate_operating_hours(operating_hours_start, operating_hours_end)

    # Validate peak hours if peak rate is set
    p_start: Optional[str] = None
    p_end: Optional[str] = None
    if peak_hours_start and peak_hours_start.strip():
        p_start = validate_time_str(peak_hours_start, "Peak hours start")
    if peak_hours_end and peak_hours_end.strip():
        p_end = validate_time_str(peak_hours_end, "Peak hours end")
    if (p_start and not p_end) or (p_end and not p_start):
        raise ValueError("Both peak hours start and end must be specified together.")

    hr_cents = validate_cents(hourly_rate_cents, "Hourly rate")
    min_cents = validate_cents(minimum_charge_cents, "Minimum charge")
    pk_cents = validate_cents(peak_hourly_rate_cents, "Peak hourly rate")

    now_str = now_rome_iso()

    with transaction() as tx:
        # Check uniqueness of machine code
        existing = tx.execute("SELECT id FROM machines WHERE UPPER(code) = ?;", (clean_code,)).fetchone()
        if existing:
            raise ValueError(f"Machine with code '{clean_code}' already exists (ID: {existing['id']}).")

        cur = tx.execute(
            """
            INSERT INTO machines (
                code, name, category_id, required_qualification_category_id, capacity, state,
                operating_hours_start, operating_hours_end, hourly_rate_cents, minimum_charge_cents,
                peak_hourly_rate_cents, peak_hours_start, peak_hours_end, location, description,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                clean_code,
                clean_name,
                int(category_id),
                req_cat_id,
                cap_val,
                clean_state,
                op_start,
                op_end,
                hr_cents,
                min_cents,
                pk_cents,
                p_start,
                p_end,
                location.strip() if location else None,
                description.strip() if description else None,
                now_str,
                now_str,
            ),
        )
        new_id = cur.lastrowid

        # Record audit log
        created_data = {
            "id": new_id,
            "code": clean_code,
            "name": clean_name,
            "category_id": int(category_id),
            "required_qualification_category_id": req_cat_id,
            "capacity": cap_val,
            "state": clean_state,
            "operating_hours": f"{op_start}-{op_end}",
            "hourly_rate_cents": hr_cents,
        }
        record_audit_event(
            action="machine.created",
            object_type="machine",
            object_id=new_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"code": clean_code, "name": clean_name, "category_id": int(category_id)},
            after=created_data,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Machine created: ID %d (%s - %s)", new_id, clean_code, clean_name)
    machine = get_machine_by_id(new_id)
    if not machine:
        raise RuntimeError("Failed to retrieve machine after creation.")
    return machine


def update_machine(
    machine_id: int,
    name: Optional[str] = None,
    category_id: Optional[int] = None,
    required_qualification_category_id: Any = "UNSET",
    capacity: Optional[int] = None,
    state: Optional[str] = None,
    operating_hours_start: Optional[str] = None,
    operating_hours_end: Optional[str] = None,
    hourly_rate_cents: Optional[int] = None,
    minimum_charge_cents: Optional[int] = None,
    peak_hourly_rate_cents: Optional[int] = None,
    peak_hours_start: Any = "UNSET",
    peak_hours_end: Any = "UNSET",
    location: Any = "UNSET",
    description: Any = "UNSET",
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Update machine specifications, pricing, hours, or requirements."""
    current = get_machine_by_id(machine_id)
    if not current:
        raise ValueError(f"Machine with ID {machine_id} does not exist.")

    updates: Dict[str, Any] = {}

    if name is not None:
        if not name.strip():
            raise ValueError("Machine name cannot be empty.")
        updates["name"] = name.strip()

    if category_id is not None:
        cat = query_one("SELECT id FROM machine_categories WHERE id = ?;", (int(category_id),))
        if not cat:
            raise ValueError(f"Machine category ID {category_id} does not exist.")
        updates["category_id"] = int(category_id)

    if required_qualification_category_id != "UNSET":
        if required_qualification_category_id in (None, "", 0, "0"):
            updates["required_qualification_category_id"] = None
        else:
            req_cat = query_one(
                "SELECT id FROM machine_categories WHERE id = ?;",
                (int(required_qualification_category_id),),
            )
            if not req_cat:
                raise ValueError(f"Required qualification category ID {required_qualification_category_id} does not exist.")
            updates["required_qualification_category_id"] = int(required_qualification_category_id)

    if capacity is not None:
        try:
            cap_val = int(capacity)
            if cap_val < 1:
                raise ValueError("Machine capacity must be at least 1.")
            updates["capacity"] = cap_val
        except (ValueError, TypeError) as err:
            raise ValueError(f"Machine capacity must be a positive integer (got '{capacity}').") from err

    if state is not None:
        clean_state = str(state).strip().lower()
        if clean_state not in VALID_MACHINE_STATES:
            raise ValueError(f"Invalid state '{state}'. Must be one of: {', '.join(VALID_MACHINE_STATES)}.")
        updates["state"] = clean_state

    # Handle operating hours
    op_start = operating_hours_start if operating_hours_start is not None else current["operating_hours_start"]
    op_end = operating_hours_end if operating_hours_end is not None else current["operating_hours_end"]
    if operating_hours_start is not None or operating_hours_end is not None:
        v_start, v_end = validate_operating_hours(op_start, op_end)
        updates["operating_hours_start"] = v_start
        updates["operating_hours_end"] = v_end

    if hourly_rate_cents is not None:
        updates["hourly_rate_cents"] = validate_cents(hourly_rate_cents, "Hourly rate")

    if minimum_charge_cents is not None:
        updates["minimum_charge_cents"] = validate_cents(minimum_charge_cents, "Minimum charge")

    if peak_hourly_rate_cents is not None:
        updates["peak_hourly_rate_cents"] = validate_cents(peak_hourly_rate_cents, "Peak hourly rate")

    if peak_hours_start != "UNSET":
        if peak_hours_start:
            updates["peak_hours_start"] = validate_time_str(peak_hours_start, "Peak hours start")
        else:
            updates["peak_hours_start"] = None

    if peak_hours_end != "UNSET":
        if peak_hours_end:
            updates["peak_hours_end"] = validate_time_str(peak_hours_end, "Peak hours end")
        else:
            updates["peak_hours_end"] = None

    if location != "UNSET":
        updates["location"] = location.strip() if (location and isinstance(location, str) and location.strip()) else None

    if description != "UNSET":
        updates["description"] = description.strip() if (description and isinstance(description, str) and description.strip()) else None

    if not updates:
        return current

    updates["updated_at"] = now_rome_iso()

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [int(machine_id)]

    with transaction() as tx:
        tx.execute(f"UPDATE machines SET {set_clause} WHERE id = ?;", tuple(values))

        updated_rec = dict(current)
        updated_rec.update(updates)

        action = "machine.state_changed" if (len(updates) == 2 and "state" in updates) else "machine.updated"
        record_audit_event(
            action=action,
            object_type="machine",
            object_id=machine_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"changes": list(updates.keys())},
            before=current,
            after=updated_rec,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Machine %d (%s) updated. Changes: %s", machine_id, current["code"], list(updates.keys()))
    res = get_machine_by_id(machine_id)
    if not res:
        raise RuntimeError("Failed to retrieve machine after update.")
    return res


def change_machine_state(
    machine_id: int,
    new_state: str,
    reason: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Change machine operational state (available, temporarily_unavailable, under_maintenance, retired)."""
    current = get_machine_by_id(machine_id)
    if not current:
        raise ValueError(f"Machine with ID {machine_id} does not exist.")

    clean_state = str(new_state).strip().lower()
    if clean_state not in VALID_MACHINE_STATES:
        raise ValueError(f"Invalid machine state '{new_state}'. Allowed: {', '.join(VALID_MACHINE_STATES)}.")

    if current["state"] == clean_state:
        return current

    now_str = now_rome_iso()
    with transaction() as tx:
        tx.execute("UPDATE machines SET state = ?, updated_at = ? WHERE id = ?;", (clean_state, now_str, int(machine_id)))

        before_state = {"state": current["state"]}
        after_state = {"state": clean_state, "reason": reason}

        record_audit_event(
            action="machine.state_changed",
            object_type="machine",
            object_id=machine_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"old_state": current["state"], "new_state": clean_state, "reason": reason},
            before=before_state,
            after=after_state,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Machine %d (%s) state changed: %s -> %s (Reason: %s)", machine_id, current["code"], current["state"], clean_state, reason)
    res = get_machine_by_id(machine_id)
    if not res:
        raise RuntimeError("Failed to retrieve machine after state change.")
    return res


def retire_machine(
    machine_id: int,
    reason: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Retire machine, preserving all historical reservations and maintenance logs."""
    return change_machine_state(
        machine_id=machine_id,
        new_state="retired",
        reason=reason or "Decommissioned and retired from active makerspace fleet.",
        actor=actor,
        actor_id=actor_id,
        actor_name=actor_name,
        ip_address=ip_address,
    )


def delete_machine(
    machine_id: int,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> bool:
    """Delete a machine only if no historical reservations, maintenance jobs, or incidents exist.
    
    If historical records exist, deletion is rejected and retirement is required to
    preserve immutable historical operational integrity.
    """
    machine = get_machine_by_id(machine_id)
    if not machine:
        raise ValueError(f"Machine with ID {machine_id} does not exist.")

    # Check for historical reservations
    res_count_row = query_one("SELECT COUNT(*) AS c FROM reservations WHERE machine_id = ?;", (int(machine_id),))
    if res_count_row and res_count_row["c"] > 0:
        raise ValueError(
            f"Cannot delete machine '{machine['name']}' ({machine['code']}): {res_count_row['c']} historical "
            f"reservation(s) exist. Machines with historical activity must be retired rather than deleted to "
            f"preserve audit and billing history."
        )

    # Check for maintenance jobs or incidents
    jobs_count = query_one("SELECT COUNT(*) AS c FROM maintenance_jobs WHERE machine_id = ?;", (int(machine_id),))
    if jobs_count and jobs_count["c"] > 0:
        raise ValueError(
            f"Cannot delete machine '{machine['name']}': {jobs_count['c']} maintenance job(s) are attached. "
            f"Retire the machine instead."
        )

    incidents_count = query_one("SELECT COUNT(*) AS c FROM incidents WHERE machine_id = ?;", (int(machine_id),))
    if incidents_count and incidents_count["c"] > 0:
        raise ValueError(
            f"Cannot delete machine '{machine['name']}': {incidents_count['c']} incident report(s) are attached. "
            f"Retire the machine instead."
        )

    with transaction() as tx:
        # Delete associated maintenance windows if any exist before deleting machine
        tx.execute("DELETE FROM maintenance_windows WHERE machine_id = ?;", (int(machine_id),))
        tx.execute("DELETE FROM machines WHERE id = ?;", (int(machine_id),))

        record_audit_event(
            action="machine.deleted",
            object_type="machine",
            object_id=machine_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"code": machine["code"], "name": machine["name"]},
            before=machine,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Machine ID %d (%s - %s) successfully deleted.", machine_id, machine["code"], machine["name"])
    return True


# -----------------------------------------------------------------------------
# Maintenance Windows
# -----------------------------------------------------------------------------

def list_maintenance_windows(
    machine_id: Optional[int] = None,
    status: Optional[str] = None,
    upcoming_only: bool = False,
    order_by: str = "mw.start_time DESC",
) -> List[Dict[str, Any]]:
    """List maintenance windows with joined machine and creator information."""
    query = """
        SELECT mw.id, mw.machine_id, mw.title, mw.start_time, mw.end_time, mw.status,
               mw.created_by_user_id, mw.notes, mw.created_at,
               m.code AS machine_code, m.name AS machine_name, m.state AS machine_state,
               u.username AS creator_username, u.full_name AS creator_name
        FROM maintenance_windows mw
        JOIN machines m ON m.id = mw.machine_id
        LEFT JOIN users u ON u.id = mw.created_by_user_id
        WHERE 1=1
    """
    params: List[Any] = []

    if machine_id is not None:
        query += " AND mw.machine_id = ?"
        params.append(int(machine_id))

    if status and status.strip() and status.strip().lower() in VALID_MAINTENANCE_STATUSES:
        query += " AND mw.status = ?"
        params.append(status.strip().lower())

    if upcoming_only:
        now_str = now_rome_iso()
        query += " AND mw.end_time >= ? AND mw.status IN ('scheduled', 'in_progress')"
        params.append(now_str)

    safe_orders = {
        "mw.start_time DESC": "mw.start_time DESC",
        "mw.start_time ASC": "mw.start_time ASC",
        "mw.created_at DESC": "mw.created_at DESC",
    }
    order_clause = safe_orders.get(order_by, "mw.start_time DESC")
    query += f" ORDER BY {order_clause};"

    return query_all(query, tuple(params))


def get_maintenance_window_by_id(window_id: int) -> Optional[Dict[str, Any]]:
    """Get single maintenance window record by ID."""
    query = """
        SELECT mw.id, mw.machine_id, mw.title, mw.start_time, mw.end_time, mw.status,
               mw.created_by_user_id, mw.notes, mw.created_at,
               m.code AS machine_code, m.name AS machine_name, m.state AS machine_state,
               u.username AS creator_username, u.full_name AS creator_name
        FROM maintenance_windows mw
        JOIN machines m ON m.id = mw.machine_id
        LEFT JOIN users u ON u.id = mw.created_by_user_id
        WHERE mw.id = ?;
    """
    return query_one(query, (int(window_id),))


def create_maintenance_window(
    machine_id: int,
    title: str,
    start_time: str,
    end_time: str,
    status: str = "scheduled",
    created_by_user_id: Optional[int] = None,
    notes: Optional[str] = None,
    set_machine_under_maintenance: bool = False,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Schedule or open a maintenance window on a machine."""
    machine = get_machine_by_id(machine_id)
    if not machine:
        raise ValueError(f"Machine with ID {machine_id} does not exist.")
    if machine["state"] == "retired":
        raise ValueError(f"Cannot schedule maintenance on retired machine '{machine['name']}'.")

    if not title or not title.strip():
        raise ValueError("Maintenance window title cannot be empty.")
    clean_title = title.strip()

    # Parse and validate datetimes
    s_dt = parse_datetime(start_time)
    e_dt = parse_datetime(end_time)
    if s_dt >= e_dt:
        raise ValueError(f"Maintenance start time ({s_dt.isoformat()}) must be strictly before end time ({e_dt.isoformat()}).")

    clean_status = str(status).strip().lower()
    if clean_status not in VALID_MAINTENANCE_STATUSES:
        raise ValueError(f"Invalid maintenance status '{status}'. Must be one of: {', '.join(VALID_MAINTENANCE_STATUSES)}.")

    now_str = now_rome_iso()

    with transaction() as tx:
        cur = tx.execute(
            """
            INSERT INTO maintenance_windows (
                machine_id, title, start_time, end_time, status, created_by_user_id, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                int(machine_id),
                clean_title,
                s_dt.isoformat(),
                e_dt.isoformat(),
                clean_status,
                created_by_user_id,
                notes.strip() if notes else None,
                now_str,
            ),
        )
        new_win_id = cur.lastrowid

        # If window is active/in_progress or caller requested machine state update, set machine to under_maintenance
        if (set_machine_under_maintenance or clean_status == "in_progress") and machine["state"] != "under_maintenance":
            tx.execute(
                "UPDATE machines SET state = 'under_maintenance', updated_at = ? WHERE id = ?;",
                (now_str, int(machine_id)),
            )
            record_audit_event(
                action="machine.state_changed",
                object_type="machine",
                object_id=machine_id,
                actor=actor,
                actor_id=actor_id,
                actor_name=actor_name,
                details={"reason": f"Maintenance window #{new_win_id} ({clean_title}) scheduled/opened."},
                before={"state": machine["state"]},
                after={"state": "under_maintenance"},
                ip_address=ip_address,
                conn=tx,
            )

        win_data = {
            "id": new_win_id,
            "machine_id": int(machine_id),
            "machine_code": machine["code"],
            "title": clean_title,
            "start_time": s_dt.isoformat(),
            "end_time": e_dt.isoformat(),
            "status": clean_status,
        }
        record_audit_event(
            action="maintenance_window.created",
            object_type="maintenance_window",
            object_id=new_win_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"machine_id": machine_id, "machine_code": machine["code"], "title": clean_title},
            after=win_data,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Maintenance window created: ID %d for machine %s (%s)", new_win_id, machine["code"], clean_title)
    win = get_maintenance_window_by_id(new_win_id)
    if not win:
        raise RuntimeError("Failed to retrieve maintenance window after creation.")
    return win


def update_maintenance_window(
    window_id: int,
    title: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    status: Optional[str] = None,
    notes: Any = "UNSET",
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Update maintenance window details or schedule."""
    current = get_maintenance_window_by_id(window_id)
    if not current:
        raise ValueError(f"Maintenance window with ID {window_id} does not exist.")

    updates: Dict[str, Any] = {}

    if title is not None:
        if not title.strip():
            raise ValueError("Title cannot be empty.")
        updates["title"] = title.strip()

    s_dt = parse_datetime(start_time) if start_time else parse_datetime(current["start_time"])
    e_dt = parse_datetime(end_time) if end_time else parse_datetime(current["end_time"])
    if start_time is not None or end_time is not None:
        if s_dt >= e_dt:
            raise ValueError(f"Start time ({s_dt.isoformat()}) must be before end time ({e_dt.isoformat()}).")
        updates["start_time"] = s_dt.isoformat()
        updates["end_time"] = e_dt.isoformat()

    if status is not None:
        clean_status = str(status).strip().lower()
        if clean_status not in VALID_MAINTENANCE_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Must be one of: {', '.join(VALID_MAINTENANCE_STATUSES)}.")
        updates["status"] = clean_status

    if notes != "UNSET":
        updates["notes"] = notes.strip() if (notes and isinstance(notes, str) and notes.strip()) else None

    if not updates:
        return current

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [int(window_id)]

    with transaction() as tx:
        tx.execute(f"UPDATE maintenance_windows SET {set_clause} WHERE id = ?;", tuple(values))

        updated_win = dict(current)
        updated_win.update(updates)

        record_audit_event(
            action="maintenance_window.updated",
            object_type="maintenance_window",
            object_id=window_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"changes": list(updates.keys())},
            before=current,
            after=updated_win,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Maintenance window %d updated: %s", window_id, list(updates.keys()))
    res = get_maintenance_window_by_id(window_id)
    if not res:
        raise RuntimeError("Failed to retrieve maintenance window after update.")
    return res


def change_maintenance_window_status(
    window_id: int,
    new_status: str,
    notes: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Change maintenance window status with automatic machine state synchronization."""
    current = get_maintenance_window_by_id(window_id)
    if not current:
        raise ValueError(f"Maintenance window with ID {window_id} does not exist.")

    clean_status = str(new_status).strip().lower()
    if clean_status not in VALID_MAINTENANCE_STATUSES:
        raise ValueError(f"Invalid status '{new_status}'. Must be one of: {', '.join(VALID_MAINTENANCE_STATUSES)}.")

    if current["status"] == clean_status:
        return current

    now_str = now_rome_iso()
    machine_id = current["machine_id"]
    machine = get_machine_by_id(machine_id)

    with transaction() as tx:
        if notes:
            tx.execute(
                "UPDATE maintenance_windows SET status = ?, notes = ? WHERE id = ?;",
                (clean_status, notes.strip(), int(window_id)),
            )
        else:
            tx.execute("UPDATE maintenance_windows SET status = ? WHERE id = ?;", (clean_status, int(window_id)))

        # Machine state sync:
        if clean_status == "in_progress" and machine and machine["state"] != "under_maintenance" and machine["state"] != "retired":
            tx.execute("UPDATE machines SET state = 'under_maintenance', updated_at = ? WHERE id = ?;", (now_str, machine_id))
            record_audit_event(
                action="machine.state_changed",
                object_type="machine",
                object_id=machine_id,
                actor=actor,
                actor_id=actor_id,
                actor_name=actor_name,
                details={"reason": f"Maintenance window #{window_id} transitioned to in_progress."},
                before={"state": machine["state"]},
                after={"state": "under_maintenance"},
                ip_address=ip_address,
                conn=tx,
            )
        elif clean_status in ("completed", "cancelled") and machine and machine["state"] == "under_maintenance":
            # Check if other active maintenance windows remain
            other_active = tx.execute(
                """
                SELECT id FROM maintenance_windows
                WHERE machine_id = ? AND id != ? AND status IN ('in_progress')
                LIMIT 1;
                """,
                (machine_id, int(window_id)),
            ).fetchone()
            other_critical_inc = tx.execute(
                """
                SELECT id FROM incidents
                WHERE machine_id = ? AND takes_machine_out_of_service = 1 AND status IN ('reported', 'investigating')
                LIMIT 1;
                """,
                (machine_id,),
            ).fetchone()
            other_active_job = tx.execute(
                """
                SELECT id FROM maintenance_jobs
                WHERE machine_id = ? AND status = 'in_progress' AND priority IN ('critical', 'high')
                LIMIT 1;
                """,
                (machine_id,),
            ).fetchone()
            if not other_active and not other_critical_inc and not other_active_job:
                tx.execute("UPDATE machines SET state = 'available', updated_at = ? WHERE id = ?;", (now_str, machine_id))
                record_audit_event(
                    action="machine.state_changed",
                    object_type="machine",
                    object_id=machine_id,
                    actor=actor,
                    actor_id=actor_id,
                    actor_name=actor_name,
                    details={"reason": f"Maintenance window #{window_id} ({clean_status}) closed with no other active windows, incidents, or high-priority jobs."},
                    before={"state": "under_maintenance"},
                    after={"state": "available"},
                    ip_address=ip_address,
                    conn=tx,
                )

        record_audit_event(
            action="maintenance_window.status_changed",
            object_type="maintenance_window",
            object_id=window_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"old_status": current["status"], "new_status": clean_status, "notes": notes},
            before={"status": current["status"]},
            after={"status": clean_status},
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Maintenance window %d status changed: %s -> %s", window_id, current["status"], clean_status)
    res = get_maintenance_window_by_id(window_id)
    if not res:
        raise RuntimeError("Failed to retrieve maintenance window after status update.")
    return res


def delete_maintenance_window(
    window_id: int,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> bool:
    """Delete a maintenance window from the database."""
    current = get_maintenance_window_by_id(window_id)
    if not current:
        raise ValueError(f"Maintenance window with ID {window_id} does not exist.")

    with transaction() as tx:
        tx.execute("DELETE FROM maintenance_windows WHERE id = ?;", (int(window_id),))

        record_audit_event(
            action="maintenance_window.deleted",
            object_type="maintenance_window",
            object_id=window_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"machine_id": current["machine_id"], "title": current["title"]},
            before=current,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Maintenance window ID %d successfully deleted.", window_id)
    return True


# -----------------------------------------------------------------------------
# Availability and Conflict Checking Helper
# -----------------------------------------------------------------------------

def check_machine_availability(
    machine_id: int,
    start_time: str,
    end_time: str,
    exclude_reservation_id: Optional[int] = None,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """Check whether a machine is available for a requested interval [start_time, end_time).
    
    Delegates to the high-performance availability engine in forgedesk.reservations.availability.
    Returns (is_available, message, list_of_conflicts).
    """
    from forgedesk.reservations.availability import check_machine_availability as _engine_check
    is_avail, msg, conflicts, _meta = _engine_check(
        machine_id=machine_id,
        start_time=start_time,
        end_time=end_time,
        exclude_reservation_id=exclude_reservation_id,
        check_qualifications=False,
    )
    return is_avail, msg, conflicts


# -----------------------------------------------------------------------------
# Maintenance Jobs (Interventi di Manutenzione)
# -----------------------------------------------------------------------------

def list_maintenance_jobs(
    machine_id: Optional[int] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    assigned_to_user_id: Optional[int] = None,
    search: Optional[str] = None,
    order_by: str = "mj.created_at DESC",
) -> List[Dict[str, Any]]:
    """List maintenance jobs with machine, assignee, and creator details."""
    query = """
        SELECT mj.id, mj.machine_id, mj.title, mj.description, mj.priority, mj.status,
               mj.assigned_to_user_id, mj.opened_by_user_id, mj.scheduled_date,
               mj.completed_at, mj.notes, mj.created_at, mj.updated_at,
               m.code AS machine_code, m.name AS machine_name, m.state AS machine_state,
               ua.username AS assignee_username, ua.full_name AS assignee_name,
               uo.username AS opener_username, uo.full_name AS opener_name
        FROM maintenance_jobs mj
        JOIN machines m ON m.id = mj.machine_id
        LEFT JOIN users ua ON ua.id = mj.assigned_to_user_id
        LEFT JOIN users uo ON uo.id = mj.opened_by_user_id
        WHERE 1=1
    """
    params: List[Any] = []

    if machine_id is not None:
        query += " AND mj.machine_id = ?"
        params.append(int(machine_id))

    if status and status.strip() and status.strip().lower() in VALID_MAINTENANCE_JOB_STATUSES:
        query += " AND mj.status = ?"
        params.append(status.strip().lower())

    if priority and priority.strip() and priority.strip().lower() in VALID_MAINTENANCE_PRIORITIES:
        query += " AND mj.priority = ?"
        params.append(priority.strip().lower())

    if assigned_to_user_id is not None:
        query += " AND mj.assigned_to_user_id = ?"
        params.append(int(assigned_to_user_id))

    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query += """ AND (
            LOWER(mj.title) LIKE ? OR
            LOWER(COALESCE(mj.description, '')) LIKE ? OR
            LOWER(COALESCE(mj.notes, '')) LIKE ? OR
            LOWER(m.code) LIKE ? OR
            LOWER(m.name) LIKE ?
        )"""
        params.extend([term, term, term, term, term])

    safe_orders = {
        "mj.created_at DESC": "mj.created_at DESC",
        "mj.created_at ASC": "mj.created_at ASC",
        "mj.priority DESC": "CASE mj.priority WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 WHEN 'low' THEN 4 ELSE 5 END ASC",
        "mj.scheduled_date ASC": "mj.scheduled_date ASC",
        "m.name ASC": "m.name ASC",
    }
    order_clause = safe_orders.get(order_by, "mj.created_at DESC")
    query += f" ORDER BY {order_clause};"

    return query_all(query, tuple(params))


def get_maintenance_job_by_id(job_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve maintenance job details by ID."""
    query = """
        SELECT mj.id, mj.machine_id, mj.title, mj.description, mj.priority, mj.status,
               mj.assigned_to_user_id, mj.opened_by_user_id, mj.scheduled_date,
               mj.completed_at, mj.notes, mj.created_at, mj.updated_at,
               m.code AS machine_code, m.name AS machine_name, m.state AS machine_state,
               ua.username AS assignee_username, ua.full_name AS assignee_name,
               uo.username AS opener_username, uo.full_name AS opener_name
        FROM maintenance_jobs mj
        JOIN machines m ON m.id = mj.machine_id
        LEFT JOIN users ua ON ua.id = mj.assigned_to_user_id
        LEFT JOIN users uo ON uo.id = mj.opened_by_user_id
        WHERE mj.id = ?;
    """
    return query_one(query, (int(job_id),))


def create_maintenance_job(
    machine_id: int,
    title: str,
    description: Optional[str] = None,
    priority: str = "medium",
    status: str = "open",
    assigned_to_user_id: Optional[int] = None,
    opened_by_user_id: Optional[int] = None,
    scheduled_date: Optional[str] = None,
    notes: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new maintenance job for equipment with audit logging."""
    machine = get_machine_by_id(machine_id)
    if not machine:
        raise ValueError(f"Machine with ID {machine_id} does not exist.")
    if machine["state"] == "retired":
        raise ValueError(f"Cannot create maintenance job on retired machine '{machine['name']}'.")

    if not title or not title.strip():
        raise ValueError("Maintenance job title cannot be empty.")
    clean_title = title.strip()

    clean_priority = str(priority).strip().lower()
    if clean_priority not in VALID_MAINTENANCE_PRIORITIES:
        raise ValueError(f"Invalid priority '{priority}'. Must be one of: {', '.join(VALID_MAINTENANCE_PRIORITIES)}.")

    clean_status = str(status).strip().lower()
    if clean_status not in VALID_MAINTENANCE_JOB_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of: {', '.join(VALID_MAINTENANCE_JOB_STATUSES)}.")

    # Validate assignee if given
    assignee_id_val: Optional[int] = None
    if assigned_to_user_id:
        user_row = query_one("SELECT id FROM users WHERE id = ?;", (int(assigned_to_user_id),))
        if not user_row:
            raise ValueError(f"Assigned technician user ID {assigned_to_user_id} does not exist.")
        assignee_id_val = int(assigned_to_user_id)

    opener_id_val = int(opened_by_user_id) if opened_by_user_id else None
    clean_date = scheduled_date.strip() if (scheduled_date and isinstance(scheduled_date, str) and scheduled_date.strip()) else None

    now_str = now_rome_iso()
    completed_at_val = now_str if clean_status == "completed" else None

    with transaction() as tx:
        cur = tx.execute(
            """
            INSERT INTO maintenance_jobs (
                machine_id, title, description, priority, status,
                assigned_to_user_id, opened_by_user_id, scheduled_date, completed_at,
                notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                int(machine_id),
                clean_title,
                description.strip() if description else None,
                clean_priority,
                clean_status,
                assignee_id_val,
                opener_id_val,
                clean_date,
                completed_at_val,
                notes.strip() if notes else None,
                now_str,
                now_str,
            ),
        )
        new_job_id = cur.lastrowid

        job_data = {
            "id": new_job_id,
            "machine_id": int(machine_id),
            "machine_code": machine["code"],
            "title": clean_title,
            "priority": clean_priority,
            "status": clean_status,
            "assigned_to_user_id": assignee_id_val,
            "scheduled_date": clean_date,
        }
        record_audit_event(
            action="maintenance_job.created",
            object_type="maintenance_job",
            object_id=new_job_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"machine_id": machine_id, "machine_code": machine["code"], "title": clean_title, "priority": clean_priority},
            after=job_data,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Maintenance job #%d created for machine %s (%s)", new_job_id, machine["code"], clean_title)
    job = get_maintenance_job_by_id(new_job_id)
    if not job:
        raise RuntimeError("Failed to retrieve maintenance job after creation.")
    return job


def update_maintenance_job(
    job_id: int,
    title: Optional[str] = None,
    description: Any = "UNSET",
    priority: Optional[str] = None,
    status: Optional[str] = None,
    assigned_to_user_id: Any = "UNSET",
    scheduled_date: Any = "UNSET",
    completed_at: Any = "UNSET",
    notes: Any = "UNSET",
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Update maintenance job details, assignment, priority, or notes."""
    current = get_maintenance_job_by_id(job_id)
    if not current:
        raise ValueError(f"Maintenance job with ID {job_id} does not exist.")

    updates: Dict[str, Any] = {}

    if title is not None:
        if not title.strip():
            raise ValueError("Title cannot be empty.")
        updates["title"] = title.strip()

    if description != "UNSET":
        updates["description"] = description.strip() if (description and isinstance(description, str) and description.strip()) else None

    if priority is not None:
        clean_p = str(priority).strip().lower()
        if clean_p not in VALID_MAINTENANCE_PRIORITIES:
            raise ValueError(f"Invalid priority '{priority}'. Must be one of: {', '.join(VALID_MAINTENANCE_PRIORITIES)}.")
        updates["priority"] = clean_p

    if status is not None:
        clean_st = str(status).strip().lower()
        if clean_st not in VALID_MAINTENANCE_JOB_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Must be one of: {', '.join(VALID_MAINTENANCE_JOB_STATUSES)}.")
        updates["status"] = clean_st
        if clean_st == "completed" and not current.get("completed_at"):
            updates["completed_at"] = now_rome_iso()

    if assigned_to_user_id != "UNSET":
        if assigned_to_user_id in (None, "", 0, "0"):
            updates["assigned_to_user_id"] = None
        else:
            u_row = query_one("SELECT id FROM users WHERE id = ?;", (int(assigned_to_user_id),))
            if not u_row:
                raise ValueError(f"User ID {assigned_to_user_id} does not exist.")
            updates["assigned_to_user_id"] = int(assigned_to_user_id)

    if scheduled_date != "UNSET":
        updates["scheduled_date"] = scheduled_date.strip() if (scheduled_date and isinstance(scheduled_date, str) and scheduled_date.strip()) else None

    if completed_at != "UNSET":
        updates["completed_at"] = completed_at.strip() if (completed_at and isinstance(completed_at, str) and completed_at.strip()) else None

    if notes != "UNSET":
        updates["notes"] = notes.strip() if (notes and isinstance(notes, str) and notes.strip()) else None

    if not updates:
        return current

    updates["updated_at"] = now_rome_iso()

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [int(job_id)]

    with transaction() as tx:
        tx.execute(f"UPDATE maintenance_jobs SET {set_clause} WHERE id = ?;", tuple(values))

        updated_job = dict(current)
        updated_job.update(updates)

        action = "maintenance_job.status_changed" if (len(updates) == 2 and "status" in updates) else "maintenance_job.updated"
        record_audit_event(
            action=action,
            object_type="maintenance_job",
            object_id=job_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"changes": list(updates.keys())},
            before=current,
            after=updated_job,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Maintenance job #%d updated: %s", job_id, list(updates.keys()))
    res = get_maintenance_job_by_id(job_id)
    if not res:
        raise RuntimeError("Failed to retrieve maintenance job after update.")
    return res


def change_maintenance_job_status(
    job_id: int,
    new_status: str,
    notes: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Change maintenance job status with completed_at tracking and audit trail."""
    current = get_maintenance_job_by_id(job_id)
    if not current:
        raise ValueError(f"Maintenance job with ID {job_id} does not exist.")

    clean_status = str(new_status).strip().lower()
    if clean_status not in VALID_MAINTENANCE_JOB_STATUSES:
        raise ValueError(f"Invalid status '{new_status}'. Must be one of: {', '.join(VALID_MAINTENANCE_JOB_STATUSES)}.")

    now_str = now_rome_iso()
    completed_at_val = now_str if clean_status == "completed" else current.get("completed_at")
    combined_notes = current.get("notes") or ""
    if notes and notes.strip():
        timestamp_prefix = f"[{now_rome().strftime('%Y-%m-%d %H:%M')}] "
        if combined_notes:
            combined_notes += "\n" + timestamp_prefix + notes.strip()
        else:
            combined_notes = timestamp_prefix + notes.strip()

    with transaction() as tx:
        tx.execute(
            """
            UPDATE maintenance_jobs
            SET status = ?, completed_at = ?, notes = ?, updated_at = ?
            WHERE id = ?;
            """,
            (clean_status, completed_at_val, combined_notes if combined_notes else None, now_str, int(job_id)),
        )

        # Synchronize machine state if critical or high priority job begins or finishes
        machine_id = current["machine_id"]
        if clean_status == "in_progress" and current.get("priority") in ("critical", "high"):
            m_row = tx.execute("SELECT state FROM machines WHERE id = ?;", (machine_id,)).fetchone()
            if m_row and m_row["state"] == "available":
                tx.execute("UPDATE machines SET state = 'under_maintenance', updated_at = ? WHERE id = ?;", (now_str, machine_id))
                record_audit_event(
                    action="machine.state_changed",
                    object_type="machine",
                    object_id=machine_id,
                    actor=actor,
                    actor_id=actor_id,
                    actor_name=actor_name,
                    details={"reason": f"High/critical maintenance job #{job_id} ({current['title']}) transitioned to in_progress."},
                    before={"state": "available"},
                    after={"state": "under_maintenance"},
                    ip_address=ip_address,
                    conn=tx,
                )
        elif clean_status in ("completed", "cancelled") and current.get("priority") in ("critical", "high"):
            other_active_job = tx.execute(
                """
                SELECT id FROM maintenance_jobs
                WHERE machine_id = ? AND id != ? AND status = 'in_progress' AND priority IN ('critical', 'high')
                LIMIT 1;
                """,
                (machine_id, int(job_id)),
            ).fetchone()
            other_active_win = tx.execute(
                """
                SELECT id FROM maintenance_windows
                WHERE machine_id = ? AND status IN ('in_progress')
                LIMIT 1;
                """,
                (machine_id,),
            ).fetchone()
            other_critical_inc = tx.execute(
                """
                SELECT id FROM incidents
                WHERE machine_id = ? AND takes_machine_out_of_service = 1 AND status IN ('reported', 'investigating')
                LIMIT 1;
                """,
                (machine_id,),
            ).fetchone()
            if not other_active_job and not other_active_win and not other_critical_inc:
                m_row = tx.execute("SELECT state FROM machines WHERE id = ?;", (machine_id,)).fetchone()
                if m_row and m_row["state"] in ("under_maintenance", "temporarily_unavailable"):
                    tx.execute("UPDATE machines SET state = 'available', updated_at = ? WHERE id = ?;", (now_str, machine_id))
                    record_audit_event(
                        action="machine.state_changed",
                        object_type="machine",
                        object_id=machine_id,
                        actor=actor,
                        actor_id=actor_id,
                        actor_name=actor_name,
                        details={"reason": f"High/critical maintenance job #{job_id} ({clean_status}) closed with no remaining active jobs, windows, or critical incidents."},
                        before={"state": m_row["state"]},
                        after={"state": "available"},
                        ip_address=ip_address,
                        conn=tx,
                    )

        before_data = {"status": current["status"], "completed_at": current.get("completed_at")}
        after_data = {"status": clean_status, "completed_at": completed_at_val, "notes": notes}

        record_audit_event(
            action="maintenance_job.status_changed",
            object_type="maintenance_job",
            object_id=job_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"old_status": current["status"], "new_status": clean_status, "notes": notes},
            before=before_data,
            after=after_data,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Maintenance job #%d status changed: %s -> %s", job_id, current["status"], clean_status)
    res = get_maintenance_job_by_id(job_id)
    if not res:
        raise RuntimeError("Failed to retrieve maintenance job after status change.")
    return res


def delete_maintenance_job(
    job_id: int,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> bool:
    """Delete a maintenance job record."""
    current = get_maintenance_job_by_id(job_id)
    if not current:
        raise ValueError(f"Maintenance job with ID {job_id} does not exist.")

    with transaction() as tx:
        tx.execute("DELETE FROM maintenance_jobs WHERE id = ?;", (int(job_id),))

        record_audit_event(
            action="maintenance_job.deleted",
            object_type="maintenance_job",
            object_id=job_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"machine_id": current["machine_id"], "title": current["title"]},
            before=current,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Maintenance job #%d successfully deleted.", job_id)
    return True


# -----------------------------------------------------------------------------
# Incidents (Segnalazione e Gestione Incidenti / Guasti)
# -----------------------------------------------------------------------------

def list_incidents(
    machine_id: Optional[int] = None,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    search: Optional[str] = None,
    order_by: str = "i.created_at DESC",
) -> List[Dict[str, Any]]:
    """List incident reports with machine details, reporter info, and attachment count."""
    query = """
        SELECT i.id, i.machine_id, i.title, i.description, i.severity, i.status,
               i.reported_by_user_id, i.takes_machine_out_of_service,
               i.created_at, i.resolved_at, i.updated_at,
               m.code AS machine_code, m.name AS machine_name, m.state AS machine_state,
               u.username AS reporter_username, u.full_name AS reporter_name,
               (
                   SELECT COUNT(*) FROM incident_attachments ia
                   WHERE ia.incident_id = i.id
               ) AS attachments_count
        FROM incidents i
        JOIN machines m ON m.id = i.machine_id
        LEFT JOIN users u ON u.id = i.reported_by_user_id
        WHERE 1=1
    """
    params: List[Any] = []

    if machine_id is not None:
        query += " AND i.machine_id = ?"
        params.append(int(machine_id))

    if status and status.strip() and status.strip().lower() in VALID_INCIDENT_STATUSES:
        query += " AND i.status = ?"
        params.append(status.strip().lower())

    if severity and severity.strip() and severity.strip().lower() in VALID_INCIDENT_SEVERITIES:
        query += " AND i.severity = ?"
        params.append(severity.strip().lower())

    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query += """ AND (
            LOWER(i.title) LIKE ? OR
            LOWER(COALESCE(i.description, '')) LIKE ? OR
            LOWER(m.code) LIKE ? OR
            LOWER(m.name) LIKE ?
        )"""
        params.extend([term, term, term, term])

    safe_orders = {
        "i.created_at DESC": "i.created_at DESC",
        "i.created_at ASC": "i.created_at ASC",
        "i.severity DESC": "CASE i.severity WHEN 'critical' THEN 1 WHEN 'major' THEN 2 WHEN 'minor' THEN 3 ELSE 4 END ASC",
        "m.name ASC": "m.name ASC",
    }
    order_clause = safe_orders.get(order_by, "i.created_at DESC")
    query += f" ORDER BY {order_clause};"

    return query_all(query, tuple(params))


def get_incident_by_id(incident_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve full incident report by ID with attachments count and machine details."""
    query = """
        SELECT i.id, i.machine_id, i.title, i.description, i.severity, i.status,
               i.reported_by_user_id, i.takes_machine_out_of_service,
               i.created_at, i.resolved_at, i.updated_at,
               m.code AS machine_code, m.name AS machine_name, m.state AS machine_state,
               u.username AS reporter_username, u.full_name AS reporter_name,
               (
                   SELECT COUNT(*) FROM incident_attachments ia
                   WHERE ia.incident_id = i.id
               ) AS attachments_count
        FROM incidents i
        JOIN machines m ON m.id = i.machine_id
        LEFT JOIN users u ON u.id = i.reported_by_user_id
        WHERE i.id = ?;
    """
    return query_one(query, (int(incident_id),))


def create_incident(
    machine_id: int,
    title: str,
    description: Optional[str] = None,
    severity: str = "minor",
    status: str = "reported",
    reported_by_user_id: Optional[int] = None,
    takes_machine_out_of_service: bool = False,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an incident report.
    
    If takes_machine_out_of_service is True (or severity is 'critical'),
    the machine operational state is transitioned to 'temporarily_unavailable' or 'under_maintenance'
    without deleting historical or existing reservations.
    """
    machine = get_machine_by_id(machine_id)
    if not machine:
        raise ValueError(f"Machine with ID {machine_id} does not exist.")

    if not title or not title.strip():
        raise ValueError("Incident title cannot be empty.")
    clean_title = title.strip()

    clean_sev = str(severity).strip().lower()
    if clean_sev not in VALID_INCIDENT_SEVERITIES:
        raise ValueError(f"Invalid incident severity '{severity}'. Must be one of: {', '.join(VALID_INCIDENT_SEVERITIES)}.")

    clean_st = str(status).strip().lower()
    if clean_st not in VALID_INCIDENT_STATUSES:
        raise ValueError(f"Invalid incident status '{status}'. Must be one of: {', '.join(VALID_INCIDENT_STATUSES)}.")

    # A critical incident or explicit out-of-service flag marks machine out of service
    out_of_service_flag = 1 if (takes_machine_out_of_service or clean_sev == "critical") else 0

    now_str = now_rome_iso()
    resolved_at_val = now_str if clean_st in ("resolved", "closed") else None

    with transaction() as tx:
        cur = tx.execute(
            """
            INSERT INTO incidents (
                machine_id, title, description, severity, status,
                reported_by_user_id, takes_machine_out_of_service,
                created_at, resolved_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                int(machine_id),
                clean_title,
                description.strip() if description else None,
                clean_sev,
                clean_st,
                int(reported_by_user_id) if reported_by_user_id else None,
                out_of_service_flag,
                now_str,
                resolved_at_val,
                now_str,
            ),
        )
        new_inc_id = cur.lastrowid

        # If taking machine out of service, change machine state to temporarily_unavailable or under_maintenance
        if out_of_service_flag and machine["state"] == "available":
            new_m_state = "under_maintenance" if clean_sev == "critical" else "temporarily_unavailable"
            tx.execute("UPDATE machines SET state = ?, updated_at = ? WHERE id = ?;", (new_m_state, now_str, int(machine_id)))
            record_audit_event(
                action="machine.state_changed",
                object_type="machine",
                object_id=machine_id,
                actor=actor,
                actor_id=actor_id,
                actor_name=actor_name,
                details={"reason": f"Incident #{new_inc_id} ({clean_sev}: {clean_title}) placed machine out of service."},
                before={"state": machine["state"]},
                after={"state": new_m_state},
                ip_address=ip_address,
                conn=tx,
            )

        inc_data = {
            "id": new_inc_id,
            "machine_id": int(machine_id),
            "machine_code": machine["code"],
            "title": clean_title,
            "severity": clean_sev,
            "status": clean_st,
            "takes_machine_out_of_service": out_of_service_flag,
        }
        record_audit_event(
            action="incident.created",
            object_type="incident",
            object_id=new_inc_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"machine_id": machine_id, "machine_code": machine["code"], "title": clean_title, "severity": clean_sev},
            after=inc_data,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Incident #%d reported for machine %s (%s, severity: %s)", new_inc_id, machine["code"], clean_title, clean_sev)
    inc = get_incident_by_id(new_inc_id)
    if not inc:
        raise RuntimeError("Failed to retrieve incident after creation.")
    return inc


def update_incident(
    incident_id: int,
    title: Optional[str] = None,
    description: Any = "UNSET",
    severity: Optional[str] = None,
    status: Optional[str] = None,
    takes_machine_out_of_service: Optional[bool] = None,
    resolved_at: Any = "UNSET",
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Update incident report details, severity, or status."""
    current = get_incident_by_id(incident_id)
    if not current:
        raise ValueError(f"Incident with ID {incident_id} does not exist.")

    updates: Dict[str, Any] = {}

    if title is not None:
        if not title.strip():
            raise ValueError("Incident title cannot be empty.")
        updates["title"] = title.strip()

    if description != "UNSET":
        updates["description"] = description.strip() if (description and isinstance(description, str) and description.strip()) else None

    if severity is not None:
        clean_sev = str(severity).strip().lower()
        if clean_sev not in VALID_INCIDENT_SEVERITIES:
            raise ValueError(f"Invalid severity '{severity}'. Must be one of: {', '.join(VALID_INCIDENT_SEVERITIES)}.")
        updates["severity"] = clean_sev

    if status is not None:
        clean_st = str(status).strip().lower()
        if clean_st not in VALID_INCIDENT_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Must be one of: {', '.join(VALID_INCIDENT_STATUSES)}.")
        updates["status"] = clean_st
        if clean_st in ("resolved", "closed") and not current.get("resolved_at"):
            updates["resolved_at"] = now_rome_iso()

    if takes_machine_out_of_service is not None:
        updates["takes_machine_out_of_service"] = 1 if takes_machine_out_of_service else 0

    if resolved_at != "UNSET":
        updates["resolved_at"] = resolved_at.strip() if (resolved_at and isinstance(resolved_at, str) and resolved_at.strip()) else None

    if not updates:
        return current

    updates["updated_at"] = now_rome_iso()

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [int(incident_id)]

    with transaction() as tx:
        tx.execute(f"UPDATE incidents SET {set_clause} WHERE id = ?;", tuple(values))

        updated_inc = dict(current)
        updated_inc.update(updates)

        action = "incident.status_changed" if (len(updates) == 2 and "status" in updates) else "incident.updated"
        record_audit_event(
            action=action,
            object_type="incident",
            object_id=incident_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"changes": list(updates.keys())},
            before=current,
            after=updated_inc,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Incident #%d updated: %s", incident_id, list(updates.keys()))
    res = get_incident_by_id(incident_id)
    if not res:
        raise RuntimeError("Failed to retrieve incident after update.")
    return res


def change_incident_status(
    incident_id: int,
    new_status: str,
    notes: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Change incident status (reported -> investigating -> resolved -> closed)."""
    current = get_incident_by_id(incident_id)
    if not current:
        raise ValueError(f"Incident with ID {incident_id} does not exist.")

    clean_status = str(new_status).strip().lower()
    if clean_status not in VALID_INCIDENT_STATUSES:
        raise ValueError(f"Invalid status '{new_status}'. Must be one of: {', '.join(VALID_INCIDENT_STATUSES)}.")

    now_str = now_rome_iso()
    resolved_at_val = now_str if clean_status in ("resolved", "closed") else current.get("resolved_at")

    with transaction() as tx:
        tx.execute(
            """
            UPDATE incidents
            SET status = ?, resolved_at = ?, updated_at = ?
            WHERE id = ?;
            """,
            (clean_status, resolved_at_val, now_str, int(incident_id)),
        )

        # Synchronize machine state if incident had taken machine out of service
        machine_id = current["machine_id"]
        if clean_status in ("resolved", "closed") and current.get("takes_machine_out_of_service"):
            other_active_inc = tx.execute(
                """
                SELECT id FROM incidents
                WHERE machine_id = ? AND id != ? AND takes_machine_out_of_service = 1 AND status IN ('reported', 'investigating')
                LIMIT 1;
                """,
                (machine_id, int(incident_id)),
            ).fetchone()
            active_mw = tx.execute(
                """
                SELECT id FROM maintenance_windows
                WHERE machine_id = ? AND status IN ('in_progress')
                LIMIT 1;
                """,
                (machine_id,),
            ).fetchone()
            active_job = tx.execute(
                """
                SELECT id FROM maintenance_jobs
                WHERE machine_id = ? AND status = 'in_progress' AND priority IN ('critical', 'high')
                LIMIT 1;
                """,
                (machine_id,),
            ).fetchone()
            if not other_active_inc and not active_mw and not active_job:
                m_row = tx.execute("SELECT state FROM machines WHERE id = ?;", (machine_id,)).fetchone()
                if m_row and m_row["state"] in ("under_maintenance", "temporarily_unavailable"):
                    tx.execute("UPDATE machines SET state = 'available', updated_at = ? WHERE id = ?;", (now_str, machine_id))
                    record_audit_event(
                        action="machine.state_changed",
                        object_type="machine",
                        object_id=machine_id,
                        actor=actor,
                        actor_id=actor_id,
                        actor_name=actor_name,
                        details={"reason": f"Incident #{incident_id} ({clean_status}) resolved with no other active incidents, maintenance windows, or high-priority jobs."},
                        before={"state": m_row["state"]},
                        after={"state": "available"},
                        ip_address=ip_address,
                        conn=tx,
                    )

        record_audit_event(
            action="incident.status_changed",
            object_type="incident",
            object_id=incident_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"old_status": current["status"], "new_status": clean_status, "notes": notes},
            before={"status": current["status"], "resolved_at": current.get("resolved_at")},
            after={"status": clean_status, "resolved_at": resolved_at_val},
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Incident #%d status changed: %s -> %s", incident_id, current["status"], clean_status)
    res = get_incident_by_id(incident_id)
    if not res:
        raise RuntimeError("Failed to retrieve incident after status change.")
    return res


def delete_incident(
    incident_id: int,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> bool:
    """Delete an incident along with its attachments from disk and database."""
    current = get_incident_by_id(incident_id)
    if not current:
        raise ValueError(f"Incident with ID {incident_id} does not exist.")

    # Retrieve all attachments to safely clean disk files
    attachments = list_incident_attachments(incident_id)

    with transaction() as tx:
        for att in attachments:
            try:
                p = Path(att["file_path"]).resolve()
                if p.is_file() and p.is_relative_to(UPLOAD_DIR):
                    p.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("Could not delete attachment file %s: %s", att.get("file_path"), e)

        tx.execute("DELETE FROM incident_attachments WHERE incident_id = ?;", (int(incident_id),))
        tx.execute("DELETE FROM incidents WHERE id = ?;", (int(incident_id),))

        record_audit_event(
            action="incident.deleted",
            object_type="incident",
            object_id=incident_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={"machine_id": current["machine_id"], "title": current["title"]},
            before=current,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Incident #%d and %d attachments deleted.", incident_id, len(attachments))
    return True


# -----------------------------------------------------------------------------
# Incident Attachments (Allegati Incidenti con Protezione Sicurezza)
# -----------------------------------------------------------------------------

def list_incident_attachments(incident_id: int) -> List[Dict[str, Any]]:
    """List all attachments associated with an incident."""
    query = """
        SELECT ia.id, ia.incident_id, ia.original_filename, ia.stored_filename,
               ia.file_path, ia.file_size, ia.mime_type, ia.uploaded_by_user_id, ia.created_at,
               u.username AS uploader_username, u.full_name AS uploader_name
        FROM incident_attachments ia
        LEFT JOIN users u ON u.id = ia.uploaded_by_user_id
        WHERE ia.incident_id = ?
        ORDER BY ia.created_at ASC;
    """
    return query_all(query, (int(incident_id),))


def get_incident_attachment_by_id(attachment_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve single incident attachment record."""
    query = """
        SELECT ia.id, ia.incident_id, ia.original_filename, ia.stored_filename,
               ia.file_path, ia.file_size, ia.mime_type, ia.uploaded_by_user_id, ia.created_at,
               u.username AS uploader_username, u.full_name AS uploader_name,
               i.machine_id, i.title AS incident_title
        FROM incident_attachments ia
        JOIN incidents i ON i.id = ia.incident_id
        LEFT JOIN users u ON u.id = ia.uploaded_by_user_id
        WHERE ia.id = ?;
    """
    return query_one(query, (int(attachment_id),))


def validate_attachment_file(
    filename: str,
    file_size: int,
    max_size: int = MAX_UPLOAD_SIZE_BYTES,
) -> Tuple[str, str]:
    """Validate upload filename, size, and extension against path traversal and executable exploits.
    
    Returns (clean_original_filename, extension_with_dot).
    """
    if not filename or not filename.strip():
        raise ValueError("Attachment filename cannot be empty.")

    # Strip directory components (path traversal and cross-platform backslash defense)
    normalized = filename.strip().replace("\x00", "").replace("\\", "/")
    clean_name = os.path.basename(normalized).strip()
    if not clean_name or clean_name in (".", ".."):
        raise ValueError("Invalid attachment filename.")

    if file_size <= 0:
        raise ValueError("Uploaded file is empty (0 bytes).")

    if file_size > max_size:
        raise ValueError(
            f"File size ({file_size} bytes / {file_size / (1024*1024):.2f} MB) exceeds maximum allowed "
            f"upload limit of {max_size / (1024*1024):.1f} MB."
        )

    _, ext = os.path.splitext(clean_name)
    ext_clean = ext.lower()

    if not ext_clean:
        raise ValueError("Attachment must have a valid file extension (e.g. .png, .jpg, .pdf, .log).")

    if ext_clean in FORBIDDEN_ATTACHMENT_EXTENSIONS:
        raise ValueError(f"File format '{ext_clean}' is prohibited for security reasons (executable/script files are forbidden).")

    if ext_clean not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError(
            f"File format '{ext_clean}' is not supported. Allowed formats: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}."
        )

    return clean_name, ext_clean


def add_incident_attachment(
    incident_id: int,
    uploaded_file: Any,
    uploaded_by_user_id: Optional[int] = None,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Save an uploaded file safely to UPLOAD_DIR and record attachment in database."""
    ensure_directories()
    incident = get_incident_by_id(incident_id)
    if not incident:
        raise ValueError(f"Incident with ID {incident_id} does not exist.")

    if not uploaded_file:
        raise ValueError("No file provided for upload.")

    filename = getattr(uploaded_file, "filename", "")
    data = getattr(uploaded_file, "data", b"")
    size = getattr(uploaded_file, "size", len(data))
    content_type = getattr(uploaded_file, "content_type", "application/octet-stream")

    clean_orig_name, ext = validate_attachment_file(filename, size)

    # Generate secure, unpredictable storage filename (UUID-based)
    unique_token = uuid.uuid4().hex
    stored_name = f"inc_{incident_id}_{unique_token[:12]}{ext}"
    target_path = (UPLOAD_DIR / stored_name).resolve()

    # Verify target path is strictly within UPLOAD_DIR (defense-in-depth against traversal)
    if not str(target_path).startswith(str(UPLOAD_DIR)):
        raise ValueError("Path traversal attempt detected in storage path resolution.")

    # Write file bytes to disk
    target_path.write_bytes(data)

    now_str = now_rome_iso()

    with transaction() as tx:
        cur = tx.execute(
            """
            INSERT INTO incident_attachments (
                incident_id, original_filename, stored_filename, file_path,
                file_size, mime_type, uploaded_by_user_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                int(incident_id),
                clean_orig_name,
                stored_name,
                str(target_path),
                size,
                content_type,
                int(uploaded_by_user_id) if uploaded_by_user_id else None,
                now_str,
            ),
        )
        new_att_id = cur.lastrowid

        record_audit_event(
            action="incident.attachment_added",
            object_type="incident_attachment",
            object_id=new_att_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={
                "incident_id": incident_id,
                "original_filename": clean_orig_name,
                "file_size": size,
                "mime_type": content_type,
            },
            after={"id": new_att_id, "incident_id": incident_id, "original_filename": clean_orig_name, "file_size": size},
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Attachment #%d ('%s', %d bytes) added to Incident #%d", new_att_id, clean_orig_name, size, incident_id)
    att = get_incident_attachment_by_id(new_att_id)
    if not att:
        raise RuntimeError("Failed to retrieve attachment after insertion.")
    return att


def delete_incident_attachment(
    attachment_id: int,
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> bool:
    """Delete attachment from database and unlink physical file from disk."""
    current = get_incident_attachment_by_id(attachment_id)
    if not current:
        raise ValueError(f"Incident attachment with ID {attachment_id} does not exist.")

    file_path = current.get("file_path")
    if file_path:
        try:
            p = Path(file_path).resolve()
            if p.is_file() and str(p).startswith(str(UPLOAD_DIR)):
                p.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Could not unlink physical file %s: %s", file_path, e)

    with transaction() as tx:
        tx.execute("DELETE FROM incident_attachments WHERE id = ?;", (int(attachment_id),))

        record_audit_event(
            action="incident.attachment_deleted",
            object_type="incident_attachment",
            object_id=attachment_id,
            actor=actor,
            actor_id=actor_id,
            actor_name=actor_name,
            details={
                "incident_id": current["incident_id"],
                "original_filename": current["original_filename"],
            },
            before=current,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Incident attachment #%d ('%s') deleted.", attachment_id, current["original_filename"])
    return True
