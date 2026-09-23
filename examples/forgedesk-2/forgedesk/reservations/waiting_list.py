"""Waiting list management and atomic slot promotion for ForgeDesk Makerspace.

Handles:
- Creation, retrieval, listing, and cancellation of waiting list entries.
- Member qualification, membership status, and machine availability eligibility checks.
- Atomic promotion of the first eligible waiting-list entry upon reservation cancellation.
- Prevention of duplicate waiting list requests.
- RBAC enforcement (Admin/Operator full management, Member self-service, Viewer read-only).
- Transactional integrity and append-only audit logging.
"""

import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

from forgedesk.audit.service import record_audit_event
from forgedesk.auth.permissions import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    get_user_role,
    is_operator_or_admin,
)
from forgedesk.db.connection import get_connection, query_all, query_one, transaction
from forgedesk.members.service import get_member_by_id, get_member_by_user_id
from forgedesk.reservations.availability import (
    check_machine_availability,
    find_interval_conflicts,
    validate_member_reservation_eligibility,
    validate_reservation_timing,
)
from forgedesk.utils.datetime_tz import (
    format_display,
    format_iso,
    now_rome,
    now_rome_iso,
    parse_datetime,
)

logger = logging.getLogger("forgedesk.reservations.waiting_list")

VALID_WAITING_STATUSES = ("waiting", "promoted", "expired", "cancelled")


class WaitingListError(Exception):
    """Base exception for waiting list operations."""
    pass


class WaitingListEligibilityError(WaitingListError):
    """Exception raised when a member is ineligible to join or be promoted from the waiting list."""

    def __init__(self, message: str, conflicts: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.message = message
        self.conflicts = conflicts or []


class WaitingListNotFoundError(WaitingListError):
    """Exception raised when a requested waiting list entry is not found."""
    pass


class WaitingListPermissionError(WaitingListError):
    """Exception raised when an unauthorized user attempts an operation on a waiting list entry."""
    pass


# -----------------------------------------------------------------------------
# Read and Lookup Functions
# -----------------------------------------------------------------------------

def get_waiting_list_entry_by_id(
    entry_id: int,
    include_details: bool = True,
    conn: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve a single waiting list entry with joined machine, member, and qualification metadata."""
    query = """
        SELECT w.id, w.machine_id, w.member_id, w.desired_start_time, w.desired_end_time,
               w.status, w.promoted_reservation_id, w.created_at, w.updated_at,
               m.code AS machine_code, m.name AS machine_name, m.capacity AS machine_capacity,
               m.state AS machine_state, m.operating_hours_start, m.operating_hours_end,
               c.id AS category_id, c.code AS category_code, c.name AS category_name,
               mem.member_number, mem.full_name AS member_name, mem.email AS member_email,
               mem.phone AS member_phone, mem.membership_status, mem.membership_expiry,
               mem.user_id AS member_user_id,
               r.status AS promoted_reservation_status, r.title AS promoted_reservation_title
        FROM waiting_list w
        JOIN machines m ON m.id = w.machine_id
        LEFT JOIN machine_categories c ON c.id = m.category_id
        JOIN members mem ON mem.id = w.member_id
        LEFT JOIN reservations r ON r.id = w.promoted_reservation_id
        WHERE w.id = ?;
    """
    row = query_one(query, (int(entry_id),), conn=conn)
    if not row:
        return None

    entry = dict(row)
    if include_details:
        s_dt = parse_datetime(entry["desired_start_time"])
        e_dt = parse_datetime(entry["desired_end_time"])
        duration_minutes = max(0, int((e_dt - s_dt).total_seconds() // 60))
        entry["duration_minutes"] = duration_minutes
        entry["formatted_desired_start"] = format_display(entry["desired_start_time"])
        entry["desired_start_time_display"] = entry["formatted_desired_start"]
        entry["formatted_desired_end"] = format_display(entry["desired_end_time"])
        entry["desired_end_time_display"] = entry["formatted_desired_end"]
        entry["formatted_created_at"] = format_display(entry["created_at"])
        entry["is_active"] = entry["status"] == "waiting"

        # Check qualification snapshot
        qual_check = query_one(
            """
            SELECT id, qualification_name, issue_date, expiry_date
            FROM qualifications
            WHERE member_id = ? AND category_id = ?;
            """,
            (int(entry["member_id"]), int(entry["category_id"] or 0)),
            conn=conn,
        )
        entry["qualification_snapshot"] = dict(qual_check) if qual_check else None

    return entry


def list_waiting_list_entries(
    machine_id: Optional[int] = None,
    member_id: Optional[int] = None,
    status: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    order_by: str = "w.created_at ASC",
) -> List[Dict[str, Any]]:
    """List waiting list entries with filters, joined metadata, and pagination."""
    query = """
        SELECT w.id, w.machine_id, w.member_id, w.desired_start_time, w.desired_end_time,
               w.status, w.promoted_reservation_id, w.created_at, w.updated_at,
               m.code AS machine_code, m.name AS machine_name, m.capacity AS machine_capacity,
               m.state AS machine_state,
               c.name AS category_name,
               mem.member_number, mem.full_name AS member_name, mem.email AS member_email,
               mem.membership_status, mem.user_id AS member_user_id
        FROM waiting_list w
        JOIN machines m ON m.id = w.machine_id
        LEFT JOIN machine_categories c ON c.id = m.category_id
        JOIN members mem ON mem.id = w.member_id
        WHERE 1=1
    """
    params: List[Any] = []

    if machine_id is not None:
        query += " AND w.machine_id = ?"
        params.append(int(machine_id))

    if member_id is not None:
        query += " AND w.member_id = ?"
        params.append(int(member_id))

    if status and status.strip():
        st = status.strip().lower()
        if st in VALID_WAITING_STATUSES:
            query += " AND w.status = ?"
            params.append(st)

    if start_date and start_date.strip():
        query += " AND w.desired_end_time >= ?"
        params.append(f"{start_date.strip()}T00:00:00")

    if end_date and end_date.strip():
        query += " AND w.desired_start_time <= ?"
        params.append(f"{end_date.strip()}T23:59:59")

    safe_order_map = {
        "w.created_at ASC": "w.created_at ASC, w.id ASC",
        "w.created_at DESC": "w.created_at DESC, w.id DESC",
        "w.desired_start_time ASC": "w.desired_start_time ASC, w.id ASC",
        "w.desired_start_time DESC": "w.desired_start_time DESC, w.id DESC",
    }
    order_clause = safe_order_map.get(order_by, "w.created_at ASC, w.id ASC")
    query += f" ORDER BY {order_clause} LIMIT ? OFFSET ?;"
    params.extend([max(1, min(limit, 500)), max(0, offset)])

    rows = query_all(query, tuple(params))
    results: List[Dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        s_dt = parse_datetime(item["desired_start_time"])
        e_dt = parse_datetime(item["desired_end_time"])
        item["duration_minutes"] = max(0, int((e_dt - s_dt).total_seconds() // 60))
        item["formatted_desired_start"] = format_display(item["desired_start_time"])
        item["desired_start_time_display"] = item["formatted_desired_start"]
        item["formatted_desired_end"] = format_display(item["desired_end_time"])
        item["desired_end_time_display"] = item["formatted_desired_end"]
        item["formatted_created_at"] = format_display(item["created_at"])
        item["is_active"] = item["status"] == "waiting"
        results.append(item)

    return results


# -----------------------------------------------------------------------------
# Eligibility Evaluation
# -----------------------------------------------------------------------------

def evaluate_entry_eligibility(
    entry_id_or_dict: Union[int, Dict[str, Any]],
    target_start_time: Optional[str] = None,
    target_end_time: Optional[str] = None,
    conn: Optional[Any] = None,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """Evaluate whether a waiting list entry is currently eligible for promotion into a machine slot.
    
    Checks:
    1. Entry status is 'waiting'.
    2. Desired start time is not in the past.
    3. Member has active membership and valid required qualification at the scheduled time.
    4. Machine is available (capacity not exceeded, no maintenance conflict, not retired).
    
    Returns (is_eligible, explanation_string, conflicts_list).
    """
    if isinstance(entry_id_or_dict, dict):
        entry = entry_id_or_dict
    else:
        entry = get_waiting_list_entry_by_id(int(entry_id_or_dict), include_details=True, conn=conn)

    if not entry:
        return False, "Waiting list entry not found.", [{"type": "not_found"}]

    if entry["status"] != "waiting":
        return False, f"Entry status is '{entry['status']}', only active waiting entries are eligible.", [{"type": "invalid_status", "status": entry["status"]}]

    check_start = target_start_time or entry["desired_start_time"]
    check_end = target_end_time or entry["desired_end_time"]

    # 1. Temporal Qualification & Membership Check
    is_member_ok, qual_msg, qual_conflicts = validate_member_reservation_eligibility(
        member_id=entry["member_id"],
        machine_id=entry["machine_id"],
        start_time=check_start,
        end_time=check_end,
        conn=conn,
    )
    if not is_member_ok:
        return False, qual_msg, qual_conflicts

    # 2. Machine State & Operating Hours Check
    is_avail, avail_msg, avail_conflicts, _ = check_machine_availability(
        machine_id=entry["machine_id"],
        start_time=check_start,
        end_time=check_end,
        member_id=entry["member_id"],
        check_qualifications=False,  # Already checked above
        conn=conn,
    )
    if not is_avail:
        return False, avail_msg, avail_conflicts

    return True, "Eligible for promotion", []


# -----------------------------------------------------------------------------
# Join Waiting List (Create Entry)
# -----------------------------------------------------------------------------

def create_waiting_list_entry(
    machine_id: int,
    member_id: int,
    desired_start_time: Union[str, datetime.datetime],
    desired_end_time: Union[str, datetime.datetime],
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Add a member to the waiting list for a specific machine and desired time slot.
    
    Validates:
    - User authorization (Admin/Operator can add anyone; Member can only add self; Viewer rejected).
    - Timing format, duration, operating hours, and non-past scheduling.
    - Active membership status and temporal machine qualifications.
    - No duplicate active waiting list entry for the same member and overlapping interval.
    """
    # 1. RBAC & Ownership Authorization
    if actor_user:
        role = get_user_role(actor_user)
        if role == ROLE_VIEWER:
            raise WaitingListPermissionError("Viewers have read-only access and cannot join the waiting list.")
        if role == ROLE_MEMBER:
            user_member = get_member_by_user_id(actor_user["id"], include_qualifications=False)
            if not user_member:
                raise WaitingListPermissionError("Your user account is not linked to an active member profile.")
            if user_member["id"] != int(member_id):
                raise WaitingListPermissionError("Members may only add themselves to the waiting list.")

    # 2. Timing Parsing and Validation
    try:
        s_dt = parse_datetime(desired_start_time)
        e_dt = parse_datetime(desired_end_time)
    except Exception as e:
        raise ValueError(f"Invalid timestamp format: {e}")

    start_iso = format_iso(s_dt)
    end_iso = format_iso(e_dt)

    is_valid_timing, timing_msg, _ = validate_reservation_timing(start_iso, end_iso)
    if not is_valid_timing:
        raise ValueError(timing_msg)

    # 3. Machine Existence and State
    machine = query_one("SELECT * FROM machines WHERE id = ?;", (int(machine_id),))
    if not machine:
        raise ValueError(f"Machine ID {machine_id} does not exist.")
    if machine["state"] == "retired":
        raise ValueError("Cannot join waiting list for a retired machine.")

    # 4. Member & Qualification Eligibility Check
    is_eligible, qual_msg, qual_conflicts = validate_member_reservation_eligibility(
        member_id=member_id,
        machine_id=machine_id,
        start_time=start_iso,
        end_time=end_iso,
    )
    if not is_eligible:
        raise WaitingListEligibilityError(
            message=f"Member is not eligible for this machine: {qual_msg}",
            conflicts=qual_conflicts,
        )

    # 5. Check for duplicate active entry
    duplicate = query_one(
        """
        SELECT id FROM waiting_list
        WHERE machine_id = ? AND member_id = ? AND status = 'waiting'
          AND desired_start_time < ? AND desired_end_time > ?;
        """,
        (int(machine_id), int(member_id), end_iso, start_iso),
    )
    if duplicate:
        raise ValueError(f"Member is already on the waiting list for this machine during this time window (Entry #{duplicate['id']}).")

    # 6. Insert Entry Atomically
    conn = get_connection()
    created_at_iso = now_rome_iso()

    with transaction(conn) as tx:
        cursor = tx.execute(
            """
            INSERT INTO waiting_list (
                machine_id, member_id, desired_start_time, desired_end_time,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'waiting', ?, ?);
            """,
            (
                int(machine_id),
                int(member_id),
                start_iso,
                end_iso,
                created_at_iso,
                created_at_iso,
            ),
        )
        new_entry_id = cursor.lastrowid

        entry_obj = get_waiting_list_entry_by_id(new_entry_id, include_details=True, conn=tx)

        # 7. Audit Log inside transaction (SEC-AUDIT-FAIL-OPEN)
        record_audit_event(
            action="waiting_list:join",
            object_type="waiting_list",
            object_id=new_entry_id,
            actor=actor_user,
            details={
                "machine_id": machine_id,
                "machine_name": entry_obj.get("machine_name") if entry_obj else None,
                "member_id": member_id,
                "member_name": entry_obj.get("member_name") if entry_obj else None,
                "desired_start_time": start_iso,
                "desired_end_time": end_iso,
            },
            after=entry_obj,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Member #%d joined waiting list #%d for machine #%d (%s to %s)", member_id, new_entry_id, machine_id, start_iso, end_iso)
    return entry_obj or get_waiting_list_entry_by_id(new_entry_id, include_details=True)


# -----------------------------------------------------------------------------
# Cancel Waiting List Entry
# -----------------------------------------------------------------------------

def cancel_waiting_list_entry(
    entry_id: int,
    reason: Optional[str] = None,
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Cancel a waiting list entry and remove it from active waiting queue."""
    existing = get_waiting_list_entry_by_id(entry_id, include_details=True)
    if not existing:
        raise WaitingListNotFoundError(f"Waiting list entry #{entry_id} not found.")

    if existing["status"] != "waiting":
        raise ValueError(f"Cannot cancel waiting list entry #{entry_id} with status '{existing['status']}'.")

    # RBAC & Ownership Authorization
    if actor_user:
        role = get_user_role(actor_user)
        if role == ROLE_VIEWER:
            raise WaitingListPermissionError("Viewers have read-only access and cannot cancel waiting list entries.")
        if role == ROLE_MEMBER:
            user_member = get_member_by_user_id(actor_user["id"], include_qualifications=False)
            if not user_member or user_member["id"] != existing["member_id"]:
                raise WaitingListPermissionError("Members may only cancel their own waiting list entries.")

    conn = get_connection()
    updated_at_iso = now_rome_iso()
    clean_reason = (reason or "Cancelled by user").strip()

    with transaction(conn) as tx:
        tx.execute(
            """
            UPDATE waiting_list
            SET status = 'cancelled',
                updated_at = ?
            WHERE id = ?;
            """,
            (updated_at_iso, int(entry_id)),
        )

        updated_obj = get_waiting_list_entry_by_id(entry_id, include_details=True, conn=tx)

        # Audit Log inside transaction (SEC-AUDIT-FAIL-OPEN)
        record_audit_event(
            action="waiting_list:cancel",
            object_type="waiting_list",
            object_id=entry_id,
            actor=actor_user,
            details={
                "reason": clean_reason,
                "machine_id": existing["machine_id"],
                "member_id": existing["member_id"],
            },
            before=existing,
            after=updated_obj,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info("Waiting list entry #%d cancelled.", entry_id)
    return updated_obj or get_waiting_list_entry_by_id(entry_id, include_details=True)


# -----------------------------------------------------------------------------
# Atomic Promotion Engine (Called on Reservation Cancellation)
# -----------------------------------------------------------------------------

def promote_first_eligible_waiting_entry(
    machine_id: int,
    freed_start_time: str,
    freed_end_time: str,
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    tx_conn: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically find and promote the first eligible waiting-list entry when a reservation slot is released.
    
    Evaluates waiting list candidates in FIFO order (`ORDER BY created_at ASC, id ASC`):
    - Overlaps with the freed slot time window.
    - Member's membership is active and qualifications are currently valid.
    - Slot capacity and maintenance windows allow booking without conflict.
    
    If eligible:
    - Atomically creates a new confirmed reservation for the member.
    - Updates waiting list entry status to 'promoted' with promoted_reservation_id.
    - Records audit log events for promotion and reservation creation.
    
    Returns dict with promoted details or None if no candidate was eligible.
    """
    logger.info(
        "Checking waiting list for machine #%d slot %s to %s...",
        machine_id, freed_start_time, freed_end_time
    )

    now_iso = now_rome_iso()

    def _execute_promotion(conn: Any) -> Optional[Dict[str, Any]]:
        # Query candidate waiting entries for this machine overlapping with the freed slot
        candidates = query_all(
            """
            SELECT id, machine_id, member_id, desired_start_time, desired_end_time,
                   status, created_at
            FROM waiting_list
            WHERE machine_id = ?
              AND status = 'waiting'
              AND desired_start_time < ?
              AND desired_end_time > ?
            ORDER BY created_at ASC, id ASC;
            """,
            (int(machine_id), freed_end_time, freed_start_time),
            conn=conn,
        )

        if not candidates:
            logger.info("No active waiting list entries found for machine #%d in interval.", machine_id)
            return None

        for cand in candidates:
            cand_id = cand["id"]
            cand_member_id = cand["member_id"]
            cand_start = cand["desired_start_time"]
            cand_end = cand["desired_end_time"]

            # If desired start time has already passed, mark as expired and skip
            if cand_start < now_iso:
                logger.info("Waiting list entry #%d desired start has passed; marking expired.", cand_id)
                conn.execute(
                    "UPDATE waiting_list SET status = 'expired', updated_at = ? WHERE id = ?;",
                    (now_iso, cand_id),
                )
                continue

            # Check qualification & membership eligibility
            is_member_eligible, qual_msg, _ = validate_member_reservation_eligibility(
                member_id=cand_member_id,
                machine_id=machine_id,
                start_time=cand_start,
                end_time=cand_end,
                conn=conn,
            )
            if not is_member_eligible:
                logger.warning(
                    "Waiting list candidate #%d (Member #%d) is not qualified: %s. Skipping.",
                    cand_id, cand_member_id, qual_msg
                )
                continue

            # Check machine availability and capacity for candidate's exact interval
            conflicts = find_interval_conflicts(
                machine_id=machine_id,
                start_time=cand_start,
                end_time=cand_end,
                member_id=cand_member_id,
                exclude_reservation_id=None,
                check_qualifications=False,
                conn=conn,
            )
            if conflicts:
                logger.info(
                    "Waiting list candidate #%d has schedule conflicts for interval %s to %s. Skipping.",
                    cand_id, cand_start, cand_end
                )
                continue

            # Candidate is eligible! Perform atomic promotion.
            machine_info = query_one("SELECT name FROM machines WHERE id = ?;", (machine_id,), conn=conn)
            machine_name = machine_info["name"] if machine_info else "Machine"
            res_title = f"Promoted from Waiting List (#{cand_id}) - {machine_name}"

            created_at_iso = now_rome_iso()
            actor_user_id = actor_user.get("id") if actor_user else None

            cursor = conn.execute(
                """
                INSERT INTO reservations (
                    machine_id, member_id, title, start_time, end_time,
                    status, created_by_user_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?);
                """,
                (
                    int(machine_id),
                    int(cand_member_id),
                    res_title,
                    cand_start,
                    cand_end,
                    actor_user_id,
                    created_at_iso,
                    created_at_iso,
                ),
            )
            new_res_id = cursor.lastrowid

            conn.execute(
                """
                UPDATE waiting_list
                SET status = 'promoted',
                    promoted_reservation_id = ?,
                    updated_at = ?
                WHERE id = ?;
                """,
                (new_res_id, created_at_iso, cand_id),
            )

            promoted_entry = get_waiting_list_entry_by_id(cand_id, include_details=True, conn=conn)

            # Audit Logs inside atomic promotion transaction (SEC-AUDIT-FAIL-OPEN)
            record_audit_event(
                action="waiting_list:promoted",
                object_type="waiting_list",
                object_id=cand_id,
                actor=actor_user,
                details={
                    "machine_id": machine_id,
                    "member_id": cand_member_id,
                    "promoted_reservation_id": new_res_id,
                    "slot_start": cand_start,
                    "slot_end": cand_end,
                },
                after=promoted_entry,
                ip_address=ip_address,
                conn=conn,
            )
            record_audit_event(
                action="reservation:create_from_waiting_list",
                object_type="reservation",
                object_id=new_res_id,
                actor=actor_user,
                details={
                    "waiting_list_entry_id": cand_id,
                    "machine_id": machine_id,
                    "member_id": cand_member_id,
                    "start_time": cand_start,
                    "end_time": cand_end,
                },
                ip_address=ip_address,
                conn=conn,
            )

            logger.info(
                "Waiting list entry #%d successfully promoted to Reservation #%d for Member #%d on Machine #%d",
                cand_id, new_res_id, cand_member_id, machine_id
            )

            return {
                "promoted_entry_id": cand_id,
                "promoted_reservation_id": new_res_id,
                "member_id": cand_member_id,
                "member_name": promoted_entry.get("member_name") if promoted_entry else None,
                "machine_id": machine_id,
                "start_time": cand_start,
                "end_time": cand_end,
                "promoted_entry": promoted_entry,
            }

        return None

    if tx_conn is not None:
        return _execute_promotion(tx_conn)
    else:
        conn = get_connection()
        with transaction(conn) as tx:
            return _execute_promotion(tx)


# -----------------------------------------------------------------------------
# Manual Promotion (Admin / Operator Action)
# -----------------------------------------------------------------------------

def promote_waiting_list_entry_by_id(
    entry_id: int,
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Manually promote an active waiting list entry into a confirmed reservation if capacity and eligibility allow."""
    existing = get_waiting_list_entry_by_id(entry_id, include_details=True)
    if not existing:
        raise WaitingListNotFoundError(f"Waiting list entry #{entry_id} not found.")

    if existing["status"] != "waiting":
        raise ValueError(f"Cannot promote entry #{entry_id} with status '{existing['status']}'.")

    if actor_user and not is_operator_or_admin(actor_user):
        raise WaitingListPermissionError("Only Administrators and Operators can manually promote waiting list entries.")

    is_eligible, reason, conflicts = evaluate_entry_eligibility(existing)
    if not is_eligible:
        raise WaitingListEligibilityError(f"Cannot promote entry #{entry_id}: {reason}", conflicts=conflicts)

    conn = get_connection()
    created_at_iso = now_rome_iso()
    actor_user_id = actor_user.get("id") if actor_user else None
    res_title = f"Manual Promotion from Waiting List (#{entry_id}) - {existing.get('machine_name', 'Machine')}"

    with transaction(conn) as tx:
        # Re-verify interval conflicts inside transaction
        re_conflicts = find_interval_conflicts(
            machine_id=existing["machine_id"],
            start_time=existing["desired_start_time"],
            end_time=existing["desired_end_time"],
            member_id=existing["member_id"],
            exclude_reservation_id=None,
            check_qualifications=True,
            conn=tx,
        )
        if re_conflicts:
            raise WaitingListEligibilityError(
                f"Concurrent conflict detected during manual promotion: {re_conflicts[0].get('message', 'Machine is occupied')}",
                conflicts=re_conflicts,
            )

        cursor = tx.execute(
            """
            INSERT INTO reservations (
                machine_id, member_id, title, start_time, end_time,
                status, created_by_user_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?);
            """,
            (
                int(existing["machine_id"]),
                int(existing["member_id"]),
                res_title,
                existing["desired_start_time"],
                existing["desired_end_time"],
                actor_user_id,
                created_at_iso,
                created_at_iso,
            ),
        )
        new_res_id = cursor.lastrowid

        tx.execute(
            """
            UPDATE waiting_list
            SET status = 'promoted',
                promoted_reservation_id = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (new_res_id, created_at_iso, int(entry_id)),
        )

        promoted_obj = get_waiting_list_entry_by_id(entry_id, include_details=True, conn=tx)

        # Audit Logs inside transaction
        try:
            record_audit_event(
                action="waiting_list:promoted_manual",
                object_type="waiting_list",
                object_id=entry_id,
                actor=actor_user,
                details={
                    "machine_id": existing["machine_id"],
                    "member_id": existing["member_id"],
                    "promoted_reservation_id": new_res_id,
                },
                before=existing,
                after=promoted_obj,
                ip_address=ip_address,
                conn=tx,
            )
            record_audit_event(
                action="reservation:create_from_waiting_list",
                object_type="reservation",
                object_id=new_res_id,
                actor=actor_user,
                details={
                    "waiting_list_entry_id": entry_id,
                    "manual_promotion": True,
                },
                ip_address=ip_address,
                conn=tx,
            )
        except Exception as e:
            logger.error("Failed to record audit event for manual promotion: %s", e)

    return {
        "success": True,
        "entry": promoted_obj or get_waiting_list_entry_by_id(entry_id, include_details=True),
        "reservation_id": new_res_id,
    }
