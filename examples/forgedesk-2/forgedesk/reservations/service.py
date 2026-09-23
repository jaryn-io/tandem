"""Single and Multi-Capacity Reservation Lifecycle Service for ForgeDesk Makerspace.

Handles:
- Atomic reservation creation with immediate conflict detection and concurrency locking.
- Reservation updating with exclusion of self-overlap.
- Reservation cancellation and slot release.
- Temporal member qualification and active membership validation.
- RBAC authorization checks (Admin/Operator full control, Member self-service, Viewer read-only).
- Transactional audit log recording for append-only compliance.
- Estimated usage charge calculation.
"""

import datetime
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union

from forgedesk.audit.service import record_audit_event
from forgedesk.auth.permissions import (
    PERM_RESERVATIONS_CREATE,
    PERM_RESERVATIONS_MANAGE_ALL,
    PERM_RESERVATIONS_MANAGE_OWN,
    PERM_RESERVATIONS_VIEW_ALL,
    PERM_RESERVATIONS_VIEW_OWN,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    get_user_role,
    has_permission,
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
from forgedesk.reservations.recurrence import (
    MAX_RECURRENCE_COUNT,
    RecurrenceRuleError,
    format_recurrence_summary,
    generate_occurrence_datetimes,
    parse_recurrence_rule,
)
from forgedesk.utils.datetime_tz import (
    format_display,
    format_iso,
    now_rome,
    now_rome_iso,
    parse_date_only,
    parse_datetime,
    today_rome_str,
)

logger = logging.getLogger("forgedesk.reservations.service")

VALID_RESERVATION_STATUSES = (
    "pending",
    "confirmed",
    "checked_in",
    "checked_out",
    "cancelled",
    "late",
    "no_show",
)

ACTIVE_RESERVATION_STATUSES = ("pending", "confirmed", "checked_in", "late")


class ReservationConflictError(Exception):
    """Exception raised when a reservation cannot be scheduled due to operational conflicts."""

    def __init__(self, message: str, conflicts: Optional[List[Dict[str, Any]]] = None, metadata: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.conflicts = conflicts or []
        self.metadata = metadata or {}


class ReservationPermissionError(Exception):
    """Exception raised when an unauthorized user attempts an operation on a reservation."""
    pass


class ReservationNotFoundError(Exception):
    """Exception raised when a requested reservation does not exist."""
    pass


class ReservationEligibilityError(Exception):
    """Exception raised when member qualification is expired, missing, or account is ineligible for check-in or booking."""
    pass


# -----------------------------------------------------------------------------
# Pricing and Estimation Helper
# -----------------------------------------------------------------------------

def estimate_reservation_charge(
    machine_id: int,
    start_time: Union[str, datetime.datetime],
    end_time: Union[str, datetime.datetime],
) -> Dict[str, Any]:
    """Estimate financial charge in cents for a machine reservation interval."""
    try:
        from forgedesk.billing.service import calculate_usage_charge
        return calculate_usage_charge(machine_or_id=int(machine_id), start_time=start_time, end_time=end_time)
    except Exception as e:
        logger.error("Error estimating reservation charge for machine #%s: %s", machine_id, e)
        return {"estimated_total_cents": 0, "breakdown": f"Error: {e}"}



# -----------------------------------------------------------------------------
# Conflict Formatting
# -----------------------------------------------------------------------------

def format_conflict_explanation(conflicts: List[Dict[str, Any]]) -> str:
    """Generate a clear, human-understandable explanation for conflicts."""
    if not conflicts:
        return "No conflicts detected."

    explanations: List[str] = []
    for c in conflicts:
        c_type = c.get("type", "unknown")
        msg = c.get("message")
        if msg:
            explanations.append(msg)
        elif c_type == "operating_hours_violation":
            explanations.append("The requested reservation time falls outside the workshop operating hours.")
        elif c_type == "capacity_exceeded":
            peak = c.get("peak_concurrent", "Maximum")
            cap = c.get("capacity", 1)
            explanations.append(f"Machine capacity is fully booked ({peak}/{cap} concurrent slots occupied).")
        elif c_type == "maintenance_window":
            title = c.get("title", "Scheduled Maintenance")
            explanations.append(f"Conflicts with scheduled maintenance window: '{title}'.")
        elif c_type == "qualification_missing":
            cat = c.get("details", {}).get("category_name", "this machine category")
            explanations.append(f"Member is not certified for {cat}. Please request qualification before booking.")
        elif c_type == "qualification_expired":
            exp = c.get("details", {}).get("expiry_date", "earlier")
            explanations.append(f"Member's qualification expires ({exp}) before the reservation date. Recertification required.")
        elif c_type == "membership_expired":
            exp = c.get("details", {}).get("expiry_date", "earlier")
            explanations.append(f"Membership has expired ({exp}). Please renew membership.")
        elif c_type == "critical_incident":
            title = c.get("title", "Critical Incident")
            explanations.append(f"Machine is out of service due to active incident: '{title}'.")
        elif c_type == "machine_retired":
            explanations.append("This machine is retired and no longer accepts new bookings.")
        elif c_type == "machine_under_maintenance":
            explanations.append("Machine is currently under maintenance.")
        elif c_type == "maintenance_job_in_progress":
            title = c.get("title", "Maintenance Intervention")
            explanations.append(f"Machine has an active maintenance job in progress: '{title}'.")
        else:
            explanations.append(f"Conflict: {c_type}")

    return " ".join(explanations)


# -----------------------------------------------------------------------------
# Single Reservation CRUD & Lifecycle
# -----------------------------------------------------------------------------

def get_reservation_by_id(
    reservation_id: int,
    include_details: bool = True,
    conn: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve a single reservation record by ID with rich linked metadata."""
    query = """
        SELECT r.id, r.machine_id, r.member_id, r.title, r.start_time, r.end_time,
               r.status, r.recurrence_group_id, r.recurrence_rule,
               r.actual_check_in, r.actual_check_out, r.cancellation_reason, r.cancelled_at,
               r.created_by_user_id, r.created_at, r.updated_at,
               m.code AS machine_code, m.name AS machine_name, m.capacity AS machine_capacity,
               m.state AS machine_state, m.hourly_rate_cents, m.minimum_charge_cents,
               m.peak_hourly_rate_cents, m.operating_hours_start, m.operating_hours_end,
               c.id AS category_id, c.code AS category_code, c.name AS category_name,
               mem.member_number, mem.full_name AS member_name, mem.email AS member_email,
               mem.phone AS member_phone, mem.membership_status, mem.membership_expiry,
               mem.user_id AS member_user_id,
               u.username AS created_by_username, u.full_name AS created_by_name
        FROM reservations r
        JOIN machines m ON m.id = r.machine_id
        LEFT JOIN machine_categories c ON c.id = m.category_id
        JOIN members mem ON mem.id = r.member_id
        LEFT JOIN users u ON u.id = r.created_by_user_id
        WHERE r.id = ?;
    """
    row = query_one(query, (int(reservation_id),), conn=conn)
    if not row:
        return None

    res = dict(row)

    if include_details:
        s_dt = parse_datetime(res["start_time"])
        e_dt = parse_datetime(res["end_time"])
        duration_minutes = max(0, int((e_dt - s_dt).total_seconds() // 60))
        res["duration_minutes"] = duration_minutes
        res["formatted_start"] = format_display(res["start_time"])
        res["formatted_end"] = format_display(res["end_time"])
        res["formatted_created_at"] = format_display(res["created_at"])
        res["is_active"] = res["status"] in ACTIVE_RESERVATION_STATUSES
        res["is_cancelled"] = res["status"] == "cancelled"

        qual_check = query_one(
            """
            SELECT id, qualification_name, issue_date, expiry_date
            FROM qualifications
            WHERE member_id = ? AND category_id = ?;
            """,
            (int(res["member_id"]), int(res["category_id"] or 0)),
            conn=conn,
        )
        res["qualification_snapshot"] = dict(qual_check) if qual_check else None

        res["estimated_charge"] = estimate_reservation_charge(
            machine_id=res["machine_id"],
            start_time=res["start_time"],
            end_time=res["end_time"],
        )

        # Attach finalized usage charge if checked out / available
        ch_row = query_one(
            "SELECT id, base_charge_cents, final_charge_cents, status, duration_minutes, created_at FROM usage_charges WHERE reservation_id = ?;",
            (int(res["id"]),),
            conn=conn,
        )
        res["usage_charge"] = dict(ch_row) if ch_row else None

        # Recurrence series and isolated occurrence context
        if res.get("recurrence_group_id"):
            group_rows = query_all(
                """
                SELECT id, start_time, end_time, status, title
                FROM reservations
                WHERE recurrence_group_id = ?
                ORDER BY start_time ASC;
                """,
                (res["recurrence_group_id"],),
                conn=conn,
            )
            group_items = []
            curr_idx = 1
            idx_counter = 1
            for gr in group_rows:
                item = dict(gr)
                item["formatted_start"] = format_display(item["start_time"])
                item["formatted_end"] = format_display(item["end_time"])
                if item["id"] == res["id"]:
                    curr_idx = idx_counter
                group_items.append(item)
                idx_counter += 1

            rule_summary = "Recurring Series"
            if res.get("recurrence_rule"):
                try:
                    r_dict = json.loads(res["recurrence_rule"]) if isinstance(res["recurrence_rule"], str) else res["recurrence_rule"]
                    rule_summary = format_recurrence_summary(r_dict)
                except Exception:
                    rule_summary = str(res["recurrence_rule"])

            res["recurrence_summary"] = rule_summary
            res["recurrence_series"] = {
                "group_id": res["recurrence_group_id"],
                "summary": rule_summary,
                "total_occurrences": len(group_items),
                "current_index": curr_idx,
                "occurrences": group_items,
            }
        else:
            res["recurrence_summary"] = None
            res["recurrence_series"] = None

    return res


def list_reservations(
    machine_id: Optional[int] = None,
    member_id: Optional[int] = None,
    category_id: Optional[int] = None,
    status: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    order_by: str = "r.start_time DESC",
) -> List[Dict[str, Any]]:
    """List reservations with flexible filters, pagination, and joined metadata."""
    query = """
        SELECT r.id, r.machine_id, r.member_id, r.title, r.start_time, r.end_time,
               r.status, r.recurrence_group_id, r.actual_check_in, r.actual_check_out,
               r.cancellation_reason, r.cancelled_at, r.created_at, r.updated_at,
               m.code AS machine_code, m.name AS machine_name, m.capacity AS machine_capacity,
               c.name AS category_name,
               mem.member_number, mem.full_name AS member_name, mem.email AS member_email,
               mem.membership_status
        FROM reservations r
        JOIN machines m ON m.id = r.machine_id
        LEFT JOIN machine_categories c ON c.id = m.category_id
        JOIN members mem ON mem.id = r.member_id
        WHERE 1=1
    """
    params: List[Any] = []

    if machine_id is not None:
        query += " AND r.machine_id = ?"
        params.append(int(machine_id))

    if member_id is not None:
        query += " AND r.member_id = ?"
        params.append(int(member_id))

    if category_id is not None:
        query += " AND m.category_id = ?"
        params.append(int(category_id))

    if status and status.strip():
        st = status.strip().lower()
        if st == "active":
            query += " AND r.status IN ('pending', 'confirmed', 'checked_in')"
        elif st in VALID_RESERVATION_STATUSES:
            query += " AND r.status = ?"
            params.append(st)

    if start_date and start_date.strip():
        query += " AND r.end_time >= ?"
        params.append(f"{start_date.strip()}T00:00:00")

    if end_date and end_date.strip():
        query += " AND r.start_time <= ?"
        params.append(f"{end_date.strip()}T23:59:59")

    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query += """ AND (
            LOWER(COALESCE(r.title, '')) LIKE ? OR
            LOWER(m.name) LIKE ? OR
            LOWER(m.code) LIKE ? OR
            LOWER(mem.full_name) LIKE ? OR
            LOWER(mem.member_number) LIKE ?
        )"""
        params.extend([term, term, term, term, term])

    safe_order_map = {
        "r.start_time DESC": "r.start_time DESC, r.id DESC",
        "r.start_time ASC": "r.start_time ASC, r.id ASC",
        "r.id DESC": "r.id DESC",
        "r.created_at DESC": "r.created_at DESC",
    }
    order_clause = safe_order_map.get(order_by, "r.start_time DESC, r.id DESC")
    query += f" ORDER BY {order_clause} LIMIT ? OFFSET ?;"
    params.extend([max(1, min(limit, 500)), max(0, offset)])

    rows = query_all(query, tuple(params))
    results: List[Dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        s_dt = parse_datetime(item["start_time"])
        e_dt = parse_datetime(item["end_time"])
        item["duration_minutes"] = max(0, int((e_dt - s_dt).total_seconds() // 60))
        item["formatted_start"] = format_display(item["start_time"])
        item["formatted_end"] = format_display(item["end_time"])
        item["is_active"] = item["status"] in ACTIVE_RESERVATION_STATUSES
        results.append(item)

    return results


def create_reservation(
    machine_id: int,
    member_id: int,
    start_time: Union[str, datetime.datetime],
    end_time: Union[str, datetime.datetime],
    title: Optional[str] = None,
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a single reservation atomically with strict conflict detection, qualification checks, and audit logging."""
    # 1. Authorize Role & Ownership
    if actor_user:
        role = get_user_role(actor_user)
        if role == ROLE_VIEWER:
            raise ReservationPermissionError("Viewers have read-only access and cannot create reservations.")
        if role == ROLE_MEMBER:
            user_member = get_member_by_user_id(actor_user["id"], include_qualifications=False)
            if not user_member:
                raise ReservationPermissionError("Your user account is not linked to an active member profile.")
            if user_member["id"] != int(member_id):
                raise ReservationPermissionError("Members may only create reservations for themselves.")

    # 2. Parse & format normalized timestamps
    try:
        s_dt = parse_datetime(start_time)
        e_dt = parse_datetime(end_time)
    except Exception as e:
        raise ValueError(f"Invalid timestamp format: {e}")

    start_iso = format_iso(s_dt)
    end_iso = format_iso(e_dt)

    # 3. Comprehensive Availability & Conflict Validation
    is_avail, conflict_msg, conflicts, metadata = check_machine_availability(
        machine_id=machine_id,
        start_time=start_iso,
        end_time=end_iso,
        member_id=member_id,
        exclude_reservation_id=None,
        check_qualifications=True,
    )

    if not is_avail:
        human_explanation = format_conflict_explanation(conflicts)
        raise ReservationConflictError(
            message=human_explanation or conflict_msg,
            conflicts=conflicts,
            metadata=metadata,
        )

    # 4. Atomic Database Transaction with Immediate Re-Check (Guard Against Race Conditions)
    conn = get_connection()
    clean_title = (title or "").strip() or f"Reservation on {metadata.get('machine_name', 'Machine')}"
    created_at_iso = now_rome_iso()
    actor_user_id = actor_user.get("id") if actor_user else None

    with transaction(conn) as tx:
        re_conflicts = find_interval_conflicts(
            machine_id=machine_id,
            start_time=start_iso,
            end_time=end_iso,
            member_id=member_id,
            exclude_reservation_id=None,
            check_qualifications=True,
        )
        if re_conflicts:
            raise ReservationConflictError(
                message=format_conflict_explanation(re_conflicts),
                conflicts=re_conflicts,
                metadata={"machine_id": machine_id},
            )

        cursor = tx.execute(
            """
            INSERT INTO reservations (
                machine_id, member_id, title, start_time, end_time,
                status, created_by_user_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?);
            """,
            (
                int(machine_id),
                int(member_id),
                clean_title,
                start_iso,
                end_iso,
                actor_user_id,
                created_at_iso,
                created_at_iso,
            ),
        )
        new_res_id = cursor.lastrowid

        # 5. Append-Only Audit Logging inside transaction (SEC-AUDIT-FAIL-OPEN)
        record_audit_event(
            action="reservation:create",
            object_type="reservation",
            object_id=new_res_id,
            actor=actor_user,
            details={
                "machine_id": machine_id,
                "machine_name": metadata.get("machine_name"),
                "member_id": member_id,
                "member_name": metadata.get("member_name"),
                "start_time": start_iso,
                "end_time": end_iso,
                "title": clean_title,
            },
            after={
                "id": new_res_id,
                "machine_id": int(machine_id),
                "member_id": int(member_id),
                "title": clean_title,
                "start_time": start_iso,
                "end_time": end_iso,
                "status": "confirmed",
                "created_by_user_id": actor_user_id,
                "created_at": created_at_iso,
                "updated_at": created_at_iso,
            },
            ip_address=ip_address,
            conn=tx,
        )

    # 6. Fetch complete created reservation record
    res_obj = get_reservation_by_id(new_res_id, include_details=True)
    logger.info("Reservation #%d created for member #%d on machine #%d (%s to %s)", new_res_id, member_id, machine_id, start_iso, end_iso)
    return res_obj


def update_reservation(
    reservation_id: int,
    machine_id: Optional[int] = None,
    member_id: Optional[int] = None,
    start_time: Optional[Union[str, datetime.datetime]] = None,
    end_time: Optional[Union[str, datetime.datetime]] = None,
    title: Optional[str] = None,
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Update an existing reservation's machine, timing, or title with conflict detection and self-exclusion."""
    existing = get_reservation_by_id(reservation_id, include_details=True)
    if not existing:
        raise ReservationNotFoundError(f"Reservation ID {reservation_id} not found.")

    # 1. State Check: Cannot edit cancelled, checked-out, or terminal reservations
    if existing["status"] in ("cancelled", "checked_out", "no_show"):
        raise ValueError(f"Cannot edit a reservation in '{existing['status']}' status.")

    # 2. RBAC & Ownership Authorization
    if actor_user:
        role = get_user_role(actor_user)
        if role == ROLE_VIEWER:
            raise ReservationPermissionError("Viewers have read-only access and cannot edit reservations.")
        if role == ROLE_MEMBER:
            user_member = get_member_by_user_id(actor_user["id"], include_qualifications=False)
            if not user_member or user_member["id"] != existing["member_id"]:
                raise ReservationPermissionError("Members may only edit their own reservations.")
            if member_id is not None and int(member_id) != existing["member_id"]:
                raise ReservationPermissionError("Members cannot transfer reservations to other members.")

    target_machine_id = int(machine_id) if machine_id is not None else existing["machine_id"]
    target_member_id = int(member_id) if member_id is not None else existing["member_id"]
    target_start_iso = format_iso(parse_datetime(start_time)) if start_time is not None else existing["start_time"]
    target_end_iso = format_iso(parse_datetime(end_time)) if end_time is not None else existing["end_time"]
    target_title = title.strip() if title is not None else existing["title"]

    timing_or_target_changed = (
        target_machine_id != existing["machine_id"]
        or target_member_id != existing["member_id"]
        or target_start_iso != existing["start_time"]
        or target_end_iso != existing["end_time"]
    )

    # 3. If timing/machine/member changed, validate availability excluding this reservation ID
    if timing_or_target_changed:
        is_avail, conflict_msg, conflicts, metadata = check_machine_availability(
            machine_id=target_machine_id,
            start_time=target_start_iso,
            end_time=target_end_iso,
            member_id=target_member_id,
            exclude_reservation_id=reservation_id,
            check_qualifications=True,
        )
        if not is_avail:
            raise ReservationConflictError(
                message=format_conflict_explanation(conflicts) or conflict_msg,
                conflicts=conflicts,
                metadata=metadata,
            )

    # 4. Atomic Update
    conn = get_connection()
    updated_at_iso = now_rome_iso()

    with transaction(conn) as tx:
        if timing_or_target_changed:
            re_conflicts = find_interval_conflicts(
                machine_id=target_machine_id,
                start_time=target_start_iso,
                end_time=target_end_iso,
                member_id=target_member_id,
                exclude_reservation_id=reservation_id,
                check_qualifications=True,
            )
            if re_conflicts:
                raise ReservationConflictError(
                    message=format_conflict_explanation(re_conflicts),
                    conflicts=re_conflicts,
                    metadata={"machine_id": target_machine_id},
                )

        tx.execute(
            """
            UPDATE reservations
            SET machine_id = ?,
                member_id = ?,
                title = ?,
                start_time = ?,
                end_time = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (
                target_machine_id,
                target_member_id,
                target_title,
                target_start_iso,
                target_end_iso,
                updated_at_iso,
                int(reservation_id),
            ),
        )

        # 5. Audit Log inside transaction (SEC-AUDIT-FAIL-OPEN)
        record_audit_event(
            action="reservation:update",
            object_type="reservation",
            object_id=reservation_id,
            actor=actor_user,
            details={
                "changed_fields": {
                    "machine_id": (existing["machine_id"], target_machine_id),
                    "member_id": (existing["member_id"], target_member_id),
                    "start_time": (existing["start_time"], target_start_iso),
                    "end_time": (existing["end_time"], target_end_iso),
                    "title": (existing["title"], target_title),
                }
            },
            before=existing,
            after={
                **existing,
                "machine_id": target_machine_id,
                "member_id": target_member_id,
                "title": target_title,
                "start_time": target_start_iso,
                "end_time": target_end_iso,
                "updated_at": updated_at_iso,
            },
            ip_address=ip_address,
            conn=tx,
        )

    # 6. Fetch updated reservation
    updated_obj = get_reservation_by_id(reservation_id, include_details=True)
    logger.info("Reservation #%d updated successfully.", reservation_id)
    return updated_obj


def cancel_reservation(
    reservation_id: int,
    reason: Optional[str] = None,
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Cancel an active reservation, release the machine slot, atomically promote eligible waiting list entries, and record audit log."""
    existing = get_reservation_by_id(reservation_id, include_details=True)
    if not existing:
        raise ReservationNotFoundError(f"Reservation ID {reservation_id} not found.")

    if existing["status"] == "cancelled":
        raise ValueError(f"Reservation #{reservation_id} is already cancelled.")

    if existing["status"] in ("checked_out", "no_show"):
        raise ValueError(f"Cannot cancel a reservation with status '{existing['status']}'.")

    # RBAC & Ownership Authorization
    if actor_user:
        role = get_user_role(actor_user)
        if role == ROLE_VIEWER:
            raise ReservationPermissionError("Viewers have read-only access and cannot cancel reservations.")
        if role == ROLE_MEMBER:
            user_member = get_member_by_user_id(actor_user["id"], include_qualifications=False)
            if not user_member or user_member["id"] != existing["member_id"]:
                raise ReservationPermissionError("Members may only cancel their own reservations.")

    conn = get_connection()
    cancelled_at_iso = now_rome_iso()
    clean_reason = (reason or "Cancelled by user").strip()
    promoted_info: Optional[Dict[str, Any]] = None

    with transaction(conn) as tx:
        tx.execute(
            """
            UPDATE reservations
            SET status = 'cancelled',
                cancelled_at = ?,
                cancellation_reason = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (cancelled_at_iso, clean_reason, cancelled_at_iso, int(reservation_id)),
        )

        # Atomic promotion of the first eligible waiting-list entry
        from forgedesk.reservations.waiting_list import promote_first_eligible_waiting_entry
        promoted_info = promote_first_eligible_waiting_entry(
            machine_id=existing["machine_id"],
            freed_start_time=existing["start_time"],
            freed_end_time=existing["end_time"],
            actor_user=actor_user,
            ip_address=ip_address,
            tx_conn=tx,
        )

        # Audit Log inside transaction (SEC-AUDIT-FAIL-OPEN)
        audit_details = {
            "cancellation_reason": clean_reason,
            "cancelled_at": cancelled_at_iso,
            "machine_id": existing["machine_id"],
            "member_id": existing["member_id"],
        }
        if promoted_info:
            audit_details["promoted_waiting_list_entry_id"] = promoted_info["promoted_entry_id"]
            audit_details["promoted_reservation_id"] = promoted_info["promoted_reservation_id"]
            audit_details["promoted_member_id"] = promoted_info["member_id"]

        record_audit_event(
            action="reservation:cancel",
            object_type="reservation",
            object_id=reservation_id,
            actor=actor_user,
            details=audit_details,
            before=existing,
            after={
                **existing,
                "status": "cancelled",
                "cancelled_at": cancelled_at_iso,
                "cancellation_reason": clean_reason,
                "promoted_waiting_list_entry": promoted_info,
            },
            ip_address=ip_address,
            conn=tx,
        )

    cancelled_obj = get_reservation_by_id(reservation_id, include_details=True) or {}
    cancelled_obj["promoted_waiting_list_entry"] = promoted_info

    if promoted_info:
        logger.info(
            "Reservation #%d cancelled (reason: %s); automatically promoted waiting list entry #%d to Reservation #%d for Member #%d.",
            reservation_id, clean_reason, promoted_info["promoted_entry_id"], promoted_info["promoted_reservation_id"], promoted_info["member_id"]
        )
    else:
        logger.info("Reservation #%d cancelled (reason: %s).", reservation_id, clean_reason)

    return cancelled_obj


# -----------------------------------------------------------------------------
# Recurring Reservation Series Management
# -----------------------------------------------------------------------------

def get_recurring_series(recurrence_group_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve all reservations and series metadata for a recurrence group."""
    clean_group_id = str(recurrence_group_id).strip()
    rows = query_all(
        """
        SELECT r.id, r.machine_id, r.member_id, r.title, r.start_time, r.end_time,
               r.status, r.recurrence_group_id, r.recurrence_rule,
               r.cancellation_reason, r.cancelled_at, r.created_at,
               m.name AS machine_name, m.code AS machine_code,
               mem.full_name AS member_name, mem.member_number
        FROM reservations r
        JOIN machines m ON m.id = r.machine_id
        JOIN members mem ON mem.id = r.member_id
        WHERE r.recurrence_group_id = ?
        ORDER BY r.start_time ASC;
        """,
        (clean_group_id,),
    )
    if not rows:
        return None

    items = []
    rule_raw = None
    machine_info = None
    member_info = None
    for r in rows:
        item = dict(r)
        item["formatted_start"] = format_display(item["start_time"])
        item["formatted_end"] = format_display(item["end_time"])
        item["is_active"] = item["status"] in ACTIVE_RESERVATION_STATUSES
        items.append(item)
        if not rule_raw and item.get("recurrence_rule"):
            rule_raw = item["recurrence_rule"]
        if not machine_info:
            machine_info = {"id": item["machine_id"], "name": item["machine_name"], "code": item["machine_code"]}
        if not member_info:
            member_info = {"id": item["member_id"], "name": item["member_name"], "number": item["member_number"]}

    rule_dict = {}
    rule_summary = "Recurring Series"
    if rule_raw:
        try:
            rule_dict = json.loads(rule_raw) if isinstance(rule_raw, str) else rule_raw
            rule_summary = format_recurrence_summary(rule_dict)
        except Exception:
            rule_summary = str(rule_raw)

    total_count = len(items)
    active_count = sum(1 for i in items if i["is_active"])
    cancelled_count = sum(1 for i in items if i["status"] == "cancelled")

    return {
        "recurrence_group_id": clean_group_id,
        "machine": machine_info,
        "member": member_info,
        "recurrence_rule": rule_dict,
        "summary": rule_summary,
        "total_occurrences": total_count,
        "active_occurrences": active_count,
        "cancelled_occurrences": cancelled_count,
        "reservations": items,
    }


def create_recurring_reservations(
    machine_id: int,
    member_id: int,
    start_date: Union[str, datetime.date],
    start_time_of_day: str,
    end_time_of_day: str,
    recurrence_rule: Union[str, Dict[str, Any]],
    title: Optional[str] = None,
    skip_conflicts: bool = False,
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a series of recurring reservations in Europe/Rome timezone with strict wall-clock DST preservation.
    
    Validates operating hours, capacity overlap, and temporal member qualifications for each individual occurrence.
    If skip_conflicts is False, any single conflict rolls back the entire batch atomically.
    """
    # 1. Authorize Role & Ownership
    if actor_user:
        role = get_user_role(actor_user)
        if role == ROLE_VIEWER:
            raise ReservationPermissionError("Viewers have read-only access and cannot create reservations.")
        if role == ROLE_MEMBER:
            user_member = get_member_by_user_id(actor_user["id"], include_qualifications=False)
            if not user_member:
                raise ReservationPermissionError("Your user account is not linked to an active member profile.")
            if user_member["id"] != int(member_id):
                raise ReservationPermissionError("Members may only create reservations for themselves.")

    # 2. Parse rule & generate localized occurrence pairs
    canonical_rule = parse_recurrence_rule(recurrence_rule)
    intervals = generate_occurrence_datetimes(
        start_date=start_date,
        start_time_of_day=start_time_of_day,
        end_time_of_day=end_time_of_day,
        recurrence_rule=canonical_rule,
    )

    if not intervals:
        raise RecurrenceRuleError("No valid occurrence dates were generated from the given recurrence rule.")

    # 3. Check machine exists
    machine = query_one("SELECT * FROM machines WHERE id = ?;", (int(machine_id),))
    if not machine:
        raise ValueError(f"Machine ID {machine_id} does not exist.")

    # 4. Check each occurrence for conflicts
    planned_occurrences: List[Tuple[datetime.datetime, datetime.datetime, str, str]] = []
    occurrence_conflicts: List[Dict[str, Any]] = []

    for s_dt, e_dt in intervals:
        s_iso = format_iso(s_dt)
        e_iso = format_iso(e_dt)
        date_str = str(s_dt.date())

        is_avail, conflict_msg, conflicts, metadata = check_machine_availability(
            machine_id=machine_id,
            start_time=s_iso,
            end_time=e_iso,
            member_id=member_id,
            exclude_reservation_id=None,
            check_qualifications=True,
        )

        if not is_avail:
            explanation = format_conflict_explanation(conflicts) or conflict_msg
            conflict_entry = {
                "date": date_str,
                "start_time": s_iso,
                "end_time": e_iso,
                "message": explanation,
                "conflicts": conflicts,
            }
            occurrence_conflicts.append(conflict_entry)
        else:
            planned_occurrences.append((s_dt, e_dt, s_iso, e_iso))

    # Strict mode: fail if any conflict
    if occurrence_conflicts and not skip_conflicts:
        sample_conflicts = "; ".join(f"{c['date']}: {c['message']}" for c in occurrence_conflicts[:3])
        if len(occurrence_conflicts) > 3:
            sample_conflicts += f" ... (+{len(occurrence_conflicts) - 3} more)"
        raise ReservationConflictError(
            message=f"Recurring booking conflict on {len(occurrence_conflicts)} occurrence(s): {sample_conflicts}",
            conflicts=occurrence_conflicts,
            metadata={
                "machine_id": machine_id,
                "total_planned": len(intervals),
                "conflict_count": len(occurrence_conflicts),
            },
        )

    if not planned_occurrences:
        raise ReservationConflictError(
            message="All requested recurring occurrence dates have operational or qualification conflicts.",
            conflicts=occurrence_conflicts,
            metadata={"machine_id": machine_id, "total_planned": len(intervals)},
        )

    # 5. Atomic Insertion in Transaction
    recurrence_group_id = f"rec_{uuid.uuid4().hex[:12]}"
    clean_title = (title or "").strip() or f"Recurring on {machine.get('name', 'Machine')}"
    created_at_iso = now_rome_iso()
    actor_user_id = actor_user.get("id") if actor_user else None
    rule_json_str = json.dumps(canonical_rule)

    conn = get_connection()
    created_ids: List[int] = []

    with transaction(conn) as tx:
        for s_dt, e_dt, s_iso, e_iso in planned_occurrences:
            # Re-check interval conflicts within transaction
            re_conflicts = find_interval_conflicts(
                machine_id=machine_id,
                start_time=s_iso,
                end_time=e_iso,
                member_id=member_id,
                exclude_reservation_id=None,
                check_qualifications=True,
            )
            if re_conflicts:
                raise ReservationConflictError(
                    message=f"Concurrent conflict on {s_dt.date()}: {format_conflict_explanation(re_conflicts)}",
                    conflicts=re_conflicts,
                    metadata={"machine_id": machine_id},
                )

            cursor = tx.execute(
                """
                INSERT INTO reservations (
                    machine_id, member_id, title, start_time, end_time,
                    status, recurrence_group_id, recurrence_rule,
                    created_by_user_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?);
                """,
                (
                    int(machine_id),
                    int(member_id),
                    clean_title,
                    s_iso,
                    e_iso,
                    recurrence_group_id,
                    rule_json_str,
                    actor_user_id,
                    created_at_iso,
                    created_at_iso,
                ),
            )
            created_ids.append(cursor.lastrowid)

        # 6. Audit Log inside transaction (SEC-AUDIT-FAIL-OPEN)
        record_audit_event(
            action="reservation:create_recurring",
            object_type="recurrence_group",
            object_id=recurrence_group_id,
            actor=actor_user,
            details={
                "recurrence_group_id": recurrence_group_id,
                "machine_id": machine_id,
                "member_id": member_id,
                "recurrence_rule": canonical_rule,
                "summary": format_recurrence_summary(canonical_rule),
                "created_count": len(created_ids),
                "reservation_ids": created_ids,
                "skipped_conflicts_count": len(occurrence_conflicts),
            },
            ip_address=ip_address,
            conn=tx,
        )

    # 7. Fetch created records
    created_reservations: List[Dict[str, Any]] = []
    for cid in created_ids:
        r_obj = get_reservation_by_id(cid, include_details=True)
        if r_obj:
            created_reservations.append(r_obj)

    logger.info(
        "Recurring reservation group %s created: %d occurrences on machine #%d for member #%d",
        recurrence_group_id, len(created_ids), machine_id, member_id
    )

    return {
        "recurrence_group_id": recurrence_group_id,
        "recurrence_rule": canonical_rule,
        "summary": format_recurrence_summary(canonical_rule),
        "total_planned": len(intervals),
        "created_count": len(created_ids),
        "created_reservations": created_reservations,
        "skipped_count": len(occurrence_conflicts),
        "skipped_conflicts": occurrence_conflicts,
    }


def cancel_recurring_series(
    recurrence_group_id: str,
    future_only: bool = False,
    from_date: Optional[str] = None,
    reason: Optional[str] = None,
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Cancel all or remaining future occurrences in a recurring reservation series."""
    series = get_recurring_series(recurrence_group_id)
    if not series or not series["reservations"]:
        raise ReservationNotFoundError(f"Recurring series '{recurrence_group_id}' not found.")

    first_res = series["reservations"][0]
    # RBAC authorization
    if actor_user:
        role = get_user_role(actor_user)
        if role == ROLE_VIEWER:
            raise ReservationPermissionError("Viewers have read-only access and cannot cancel recurring reservations.")
        if role == ROLE_MEMBER:
            user_member = get_member_by_user_id(actor_user["id"], include_qualifications=False)
            if not user_member or user_member["id"] != first_res["member_id"]:
                raise ReservationPermissionError("Members may only cancel their own recurring reservations.")

    clean_reason = (reason or "Recurring series cancelled").strip()
    cancelled_at_iso = now_rome_iso()

    query = """
        SELECT id FROM reservations
        WHERE recurrence_group_id = ? AND status IN ('pending', 'confirmed')
    """
    params: List[Any] = [recurrence_group_id]

    if future_only:
        cutoff = from_date or now_rome_iso()
        query += " AND start_time >= ?"
        params.append(cutoff)

    target_rows = query_all(query, tuple(params))
    target_ids = [r["id"] for r in target_rows]

    if not target_ids:
        return {
            "recurrence_group_id": recurrence_group_id,
            "cancelled_count": 0,
            "message": "No active occurrences to cancel in this series.",
        }

    conn = get_connection()
    promoted_list: List[Dict[str, Any]] = []

    with transaction(conn) as tx:
        placeholders = ",".join("?" * len(target_ids))
        tx.execute(
            f"""
            UPDATE reservations
            SET status = 'cancelled',
                cancelled_at = ?,
                cancellation_reason = ?,
                updated_at = ?
            WHERE id IN ({placeholders});
            """,
            (cancelled_at_iso, clean_reason, cancelled_at_iso, *target_ids),
        )

        from forgedesk.reservations.waiting_list import promote_first_eligible_waiting_entry
        for tid in target_ids:
            t_res = get_reservation_by_id(tid, include_details=False)
            if t_res:
                p_info = promote_first_eligible_waiting_entry(
                    machine_id=t_res["machine_id"],
                    freed_start_time=t_res["start_time"],
                    freed_end_time=t_res["end_time"],
                    actor_user=actor_user,
                    ip_address=ip_address,
                    tx_conn=tx,
                )
                if p_info:
                    promoted_list.append(p_info)

        # Record audit log inside transaction (SEC-AUDIT-FAIL-OPEN)
        record_audit_event(
            action="reservation:cancel_recurring_series",
            object_type="recurrence_group",
            object_id=recurrence_group_id,
            actor=actor_user,
            details={
                "recurrence_group_id": recurrence_group_id,
                "cancelled_ids": target_ids,
                "cancelled_count": len(target_ids),
                "promoted_waiting_list_count": len(promoted_list),
                "reason": clean_reason,
                "future_only": future_only,
            },
            ip_address=ip_address,
            conn=tx,
        )

    return {
        "recurrence_group_id": recurrence_group_id,
        "cancelled_count": len(target_ids),
        "cancelled_reservation_ids": target_ids,
        "promoted_waiting_list_entries": promoted_list,
        "reason": clean_reason,
    }


# -----------------------------------------------------------------------------
# Check-In, Check-Out, Late Arrival & No-Show Lifecycle Transitions (Step S15)
# -----------------------------------------------------------------------------

def check_in_reservation(
    reservation_id: int,
    actor_user: Optional[Dict[str, Any]] = None,
    actual_check_in_time: Optional[Union[str, datetime.datetime]] = None,
    ip_address: Optional[str] = None,
    conn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Perform check-in for a confirmed or late reservation.
    
    Verifies:
    - Reservation exists and is in an eligible state ('confirmed', 'late', 'pending').
    - User has permission (Admin/Operator, or Member owning the reservation).
    - Machine is currently available and not retired/under maintenance/out of service.
    - Member's membership is active and not expired.
    - Member holds valid (unexpired) qualification for the machine category at the time of check-in.
    
    Updates reservation status to 'checked_in', records actual_check_in timestamp, and logs audit event.
    """
    existing = get_reservation_by_id(reservation_id, include_details=True, conn=conn)
    if not existing:
        raise ReservationNotFoundError(f"Reservation ID {reservation_id} not found.")

    current_status = existing["status"]
    if current_status == "checked_in":
        raise ValueError(f"Reservation #{reservation_id} is already checked in.")
    if current_status == "checked_out":
        raise ValueError(f"Reservation #{reservation_id} is already checked out.")
    if current_status == "cancelled":
        raise ValueError("Cannot check in a cancelled reservation.")
    if current_status == "no_show":
        raise ValueError("Cannot check in a reservation marked as no-show.")
    if current_status not in ("confirmed", "late", "pending"):
        raise ValueError(f"Cannot check in reservation with status '{current_status}'.")

    # RBAC & Ownership Authorization
    if actor_user:
        role = get_user_role(actor_user)
        if role == ROLE_VIEWER:
            raise ReservationPermissionError("Viewers have read-only access and cannot check in reservations.")
        if role == ROLE_MEMBER:
            user_member = get_member_by_user_id(actor_user["id"], include_qualifications=False)
            if not user_member or user_member["id"] != existing["member_id"]:
                raise ReservationPermissionError("Members may only check in their own reservations.")

    # Resolve check-in timestamp (FD-SEC-001)
    is_manual_override = False
    server_now = now_rome()

    if is_operator_or_admin(actor_user) and actual_check_in_time:
        check_in_dt = parse_datetime(actual_check_in_time)
        # Limit future timestamps to a 5-minute clock-skew tolerance
        if check_in_dt > server_now + datetime.timedelta(minutes=5):
            raise ValueError("Manual check-in timestamp cannot be in the future.")
        if check_in_dt < server_now - datetime.timedelta(days=30):
            raise ValueError("Manual check-in timestamp cannot be older than 30 days.")
        is_manual_override = True
    else:
        # Regular members, non-privileged callers, or default flow strictly use current server time
        check_in_dt = server_now
    check_in_iso = format_iso(check_in_dt)

    # Validate machine state
    machine = query_one(
        "SELECT id, name, code, state, category_id, required_qualification_category_id FROM machines WHERE id = ?;",
        (int(existing["machine_id"]),),
        conn=conn,
    )
    if not machine:
        raise ReservationNotFoundError(f"Machine ID {existing['machine_id']} does not exist.")

    if machine.get("state") in ("retired", "under_maintenance", "temporarily_unavailable"):
        st_label = machine["state"].replace("_", " ")
        raise ReservationConflictError(
            f"Cannot check in: Machine '{machine['name']}' is currently {st_label}.",
            conflicts=[{"type": f"machine_{machine['state']}", "machine_id": machine["id"]}],
        )

    # Check for active critical incidents taking machine out of service
    critical_incident = query_one(
        """
        SELECT id, title, severity FROM incidents
        WHERE machine_id = ? AND takes_machine_out_of_service = 1 AND status != 'closed';
        """,
        (int(existing["machine_id"]),),
        conn=conn,
    )
    if critical_incident:
        raise ReservationConflictError(
            f"Cannot check in: Machine '{machine['name']}' is out of service due to critical incident '{critical_incident['title']}'.",
            conflicts=[{"type": "critical_incident", "incident_id": critical_incident["id"], "title": critical_incident["title"]}],
        )

    # Validate member qualifications and membership status AT CHECK-IN TIME
    # Brief requirement:
    # "A member may reserve or use a machine only when all required qualifications are valid
    # for the relevant time. The system must handle a qualification that is valid when a
    # reservation is created but expires before check-in."
    is_eligible, elig_msg, elig_conflicts = validate_member_reservation_eligibility(
        member_id=existing["member_id"],
        machine_id=existing["machine_id"],
        start_time=check_in_iso,
        end_time=check_in_iso,
        conn=conn,
    )
    if not is_eligible:
        raise ReservationEligibilityError(f"Check-in rejected: {elig_msg}")

    updated_at_iso = now_rome_iso()
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        with transaction(conn) as tx:
            tx.execute(
                """
                UPDATE reservations
                SET status = 'checked_in',
                    actual_check_in = ?,
                    updated_at = ?
                WHERE id = ?;
                """,
                (check_in_iso, updated_at_iso, int(reservation_id)),
            )

            # Audit event inside transaction (SEC-AUDIT-FAIL-OPEN)
            record_audit_event(
                action="reservation:check_in",
                object_type="reservation",
                object_id=reservation_id,
                actor=actor_user,
                details={
                    "actual_check_in": check_in_iso,
                    "previous_status": current_status,
                    "machine_id": existing["machine_id"],
                    "member_id": existing["member_id"],
                    "manual_timestamp_override": is_manual_override,
                    "server_time_enforced": not is_manual_override,
                },
                before=existing,
                after={
                    **existing,
                    "status": "checked_in",
                    "actual_check_in": check_in_iso,
                    "updated_at": updated_at_iso,
                },
                ip_address=ip_address,
                conn=tx,
            )
    finally:
        if owns_conn:
            conn.close()

    updated_res = get_reservation_by_id(reservation_id, include_details=True)
    logger.info("Reservation #%d checked in successfully at %s.", reservation_id, check_in_iso)
    return updated_res


def check_out_reservation(
    reservation_id: int,
    actor_user: Optional[Dict[str, Any]] = None,
    actual_check_out_time: Optional[Union[str, datetime.datetime]] = None,
    notes: Optional[str] = None,
    ip_address: Optional[str] = None,
    conn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Perform check-out for an active checked-in reservation.
    
    Verifies:
    - Reservation is currently in 'checked_in' status.
    - User has permission (Admin/Operator, or Member owning the reservation).
    
    Calculates actual usage duration, estimates usage charge, sets status to 'checked_out',
    records actual_check_out timestamp, and records audit event.
    """
    existing = get_reservation_by_id(reservation_id, include_details=True, conn=conn)
    if not existing:
        raise ReservationNotFoundError(f"Reservation ID {reservation_id} not found.")

    current_status = existing["status"]
    if current_status == "checked_out":
        raise ValueError(f"Reservation #{reservation_id} is already checked out.")
    if current_status != "checked_in":
        raise ValueError(f"Cannot check out reservation with status '{current_status}'. Reservation must be checked in first.")

    # RBAC & Ownership Authorization
    if actor_user:
        role = get_user_role(actor_user)
        if role == ROLE_VIEWER:
            raise ReservationPermissionError("Viewers have read-only access and cannot check out reservations.")
        if role == ROLE_MEMBER:
            user_member = get_member_by_user_id(actor_user["id"], include_qualifications=False)
            if not user_member or user_member["id"] != existing["member_id"]:
                raise ReservationPermissionError("Members may only check out their own reservations.")

    # Resolve timestamp (FD-SEC-001)
    is_manual_override = False
    server_now = now_rome()
    check_in_dt = parse_datetime(existing.get("actual_check_in") or existing["start_time"])

    if is_operator_or_admin(actor_user) and actual_check_out_time:
        check_out_dt = parse_datetime(actual_check_out_time)
        if check_out_dt > server_now + datetime.timedelta(minutes=5):
            raise ValueError("Manual check-out timestamp cannot be in the future.")
        if check_out_dt < check_in_dt:
            raise ValueError("Manual check-out timestamp cannot be earlier than check-in time.")
        is_manual_override = True
    else:
        # Regular members, non-privileged callers, or default flow strictly use current server time
        check_out_dt = server_now
        if check_out_dt < check_in_dt:
            check_out_dt = check_in_dt
    check_out_iso = format_iso(check_out_dt)

    duration_minutes = max(1, int((check_out_dt - check_in_dt).total_seconds() // 60))

    # Calculate actual usage charges based on actual check_in and check_out
    usage_charge_estimate = estimate_reservation_charge(
        machine_id=existing["machine_id"],
        start_time=format_iso(check_in_dt),
        end_time=check_out_iso,
    )

    updated_at_iso = now_rome_iso()
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        from forgedesk.billing.service import create_usage_charge_for_checkout

        with transaction(conn) as tx:
            tx.execute(
                """
                UPDATE reservations
                SET status = 'checked_out',
                    actual_check_out = ?,
                    updated_at = ?
                WHERE id = ?;
                """,
                (check_out_iso, updated_at_iso, int(reservation_id)),
            )

            # Persist finalized usage charge snapshot in usage_charges ledger
            finalized_charge = create_usage_charge_for_checkout(
                reservation_id=int(reservation_id),
                check_in_time=format_iso(check_in_dt),
                check_out_time=check_out_iso,
                actor_user=actor_user,
                ip_address=ip_address,
                conn=tx,
            )

            # Audit log inside transaction (SEC-AUDIT-FAIL-OPEN)
            record_audit_event(
                action="reservation:check_out",
                object_type="reservation",
                object_id=reservation_id,
                actor=actor_user,
                details={
                    "actual_check_in": existing.get("actual_check_in"),
                    "actual_check_out": check_out_iso,
                    "duration_minutes": duration_minutes,
                    "estimated_charge_cents": usage_charge_estimate.get("estimated_total_cents", 0),
                    "final_charge_cents": finalized_charge.get("final_charge_cents", 0),
                    "charge_id": finalized_charge.get("id"),
                    "notes": notes,
                    "manual_timestamp_override": is_manual_override,
                    "server_time_enforced": not is_manual_override,
                },
                before=existing,
                after={
                    **existing,
                    "status": "checked_out",
                    "actual_check_out": check_out_iso,
                    "updated_at": updated_at_iso,
                    "actual_usage_minutes": duration_minutes,
                    "usage_charge": finalized_charge,
                },
                ip_address=ip_address,
                conn=tx,
            )
    finally:
        if owns_conn:
            conn.close()

    updated_res = get_reservation_by_id(reservation_id, include_details=True)
    if updated_res:
        updated_res["actual_usage_minutes"] = duration_minutes
        updated_res["usage_charge_estimate"] = usage_charge_estimate
        if "usage_charge" not in updated_res or not updated_res["usage_charge"]:
            updated_res["usage_charge"] = finalized_charge

    logger.info("Reservation #%d checked out successfully at %s (duration: %d min).", reservation_id, check_out_iso, duration_minutes)
    return updated_res


def mark_reservation_late(
    reservation_id: int,
    actor_user: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
    ip_address: Optional[str] = None,
    conn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Transition a confirmed reservation to 'late' status when member arrives late or reports delay."""
    existing = get_reservation_by_id(reservation_id, include_details=True, conn=conn)
    if not existing:
        raise ReservationNotFoundError(f"Reservation ID {reservation_id} not found.")

    current_status = existing["status"]
    if current_status == "late":
        return existing
    if current_status in ("checked_in", "checked_out", "cancelled", "no_show"):
        raise ValueError(f"Cannot mark reservation with status '{current_status}' as late.")

    # RBAC & Ownership Authorization
    if actor_user:
        role = get_user_role(actor_user)
        if role == ROLE_VIEWER:
            raise ReservationPermissionError("Viewers have read-only access and cannot update reservation status.")
        if role == ROLE_MEMBER:
            user_member = get_member_by_user_id(actor_user["id"], include_qualifications=False)
            if not user_member or user_member["id"] != existing["member_id"]:
                raise ReservationPermissionError("Members may only report delays on their own reservations.")

    updated_at_iso = now_rome_iso()
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        with transaction(conn) as tx:
            tx.execute(
                """
                UPDATE reservations
                SET status = 'late',
                    updated_at = ?
                WHERE id = ?;
                """,
                (updated_at_iso, int(reservation_id)),
            )

            # Audit log inside transaction (SEC-AUDIT-FAIL-OPEN)
            record_audit_event(
                action="reservation:mark_late",
                object_type="reservation",
                object_id=reservation_id,
                actor=actor_user,
                details={
                    "previous_status": current_status,
                    "reason": reason or "Late arrival reported",
                },
                before=existing,
                after={
                    **existing,
                    "status": "late",
                    "updated_at": updated_at_iso,
                },
                ip_address=ip_address,
                conn=tx,
            )
    finally:
        if owns_conn:
            conn.close()

    updated_res = get_reservation_by_id(reservation_id, include_details=True)
    logger.info("Reservation #%d marked as late.", reservation_id)
    return updated_res


def mark_reservation_no_show(
    reservation_id: int,
    actor_user: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
    promote_waiting_list: bool = True,
    ip_address: Optional[str] = None,
    conn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Transition a confirmed or late reservation to 'no_show' status, releasing the slot for waiting list."""
    existing = get_reservation_by_id(reservation_id, include_details=True, conn=conn)
    if not existing:
        raise ReservationNotFoundError(f"Reservation ID {reservation_id} not found.")

    current_status = existing["status"]
    if current_status == "no_show":
        raise ValueError(f"Reservation #{reservation_id} is already marked as no-show.")
    if current_status in ("checked_in", "checked_out", "cancelled"):
        raise ValueError(f"Cannot mark reservation with status '{current_status}' as no-show.")

    # RBAC: Only staff can mark no-show (or system automation)
    if actor_user:
        role = get_user_role(actor_user)
        if role not in (ROLE_ADMIN, ROLE_OPERATOR):
            raise ReservationPermissionError("Only administrators and operators may mark reservations as no-show.")

    updated_at_iso = now_rome_iso()
    clean_reason = (reason or "Member did not arrive for scheduled booking").strip()
    promoted_info: Optional[Dict[str, Any]] = None

    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        with transaction(conn) as tx:
            tx.execute(
                """
                UPDATE reservations
                SET status = 'no_show',
                    cancellation_reason = ?,
                    updated_at = ?
                WHERE id = ?;
                """,
                (f"No-show: {clean_reason}", updated_at_iso, int(reservation_id)),
            )

            if promote_waiting_list:
                from forgedesk.reservations.waiting_list import promote_first_eligible_waiting_entry
                promoted_info = promote_first_eligible_waiting_entry(
                    machine_id=existing["machine_id"],
                    freed_start_time=existing["start_time"],
                    freed_end_time=existing["end_time"],
                    actor_user=actor_user,
                    ip_address=ip_address,
                    tx_conn=tx,
                )

            # Audit log inside transaction (SEC-AUDIT-FAIL-OPEN)
            audit_details = {
                "previous_status": current_status,
                "reason": clean_reason,
                "machine_id": existing["machine_id"],
                "member_id": existing["member_id"],
            }
            if promoted_info:
                audit_details["promoted_waiting_list_entry_id"] = promoted_info["promoted_entry_id"]
                audit_details["promoted_reservation_id"] = promoted_info["promoted_reservation_id"]
            record_audit_event(
                action="reservation:mark_no_show",
                object_type="reservation",
                object_id=reservation_id,
                actor=actor_user,
                details=audit_details,
                before=existing,
                after={
                    **existing,
                    "status": "no_show",
                    "cancellation_reason": f"No-show: {clean_reason}",
                    "updated_at": updated_at_iso,
                    "promoted_waiting_list_entry": promoted_info,
                },
                ip_address=ip_address,
                conn=tx,
            )
    finally:
        if owns_conn:
            conn.close()

    updated_res = get_reservation_by_id(reservation_id, include_details=True) or {}
    updated_res["promoted_waiting_list_entry"] = promoted_info
    logger.info("Reservation #%d marked as no-show.", reservation_id)
    return updated_res


def process_overdue_reservations(
    current_time: Optional[Union[str, datetime.datetime]] = None,
    late_grace_minutes: int = 15,
    no_show_grace_minutes: int = 30,
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Scan and automatically transition overdue confirmed reservations to 'late' or 'no_show'.
    
    - Confirmed reservations past start_time + late_grace_minutes (and not yet end_time) -> 'late'.
    - Confirmed or Late reservations past start_time + no_show_grace_minutes (or past end_time) -> 'no_show'.
    """
    now_dt = parse_datetime(current_time) if current_time else now_rome()
    now_iso = format_iso(now_dt)

    rows = query_all(
        """
        SELECT id, start_time, end_time, status, machine_id, member_id
        FROM reservations
        WHERE status IN ('confirmed', 'late') AND start_time <= ?;
        """,
        (now_iso,),
    )

    marked_late_ids: List[int] = []
    marked_noshow_ids: List[int] = []

    for r in rows:
        r_id = r["id"]
        s_dt = parse_datetime(r["start_time"])
        e_dt = parse_datetime(r["end_time"])
        mins_since_start = int((now_dt - s_dt).total_seconds() // 60)

        if mins_since_start >= no_show_grace_minutes or now_dt >= e_dt:
            try:
                mark_reservation_no_show(
                    reservation_id=r_id,
                    actor_user=actor_user,
                    reason=f"Automated scan: passed {mins_since_start} min without check-in",
                    promote_waiting_list=True,
                    ip_address=ip_address,
                )
                marked_noshow_ids.append(r_id)
            except Exception as e:
                logger.error("Error auto marking reservation #%d as no-show: %s", r_id, e)
        elif mins_since_start >= late_grace_minutes and r["status"] == "confirmed":
            try:
                mark_reservation_late(
                    reservation_id=r_id,
                    actor_user=actor_user,
                    reason=f"Automated scan: passed {mins_since_start} min grace period",
                    ip_address=ip_address,
                )
                marked_late_ids.append(r_id)
            except Exception as e:
                logger.error("Error auto marking reservation #%d as late: %s", r_id, e)

    return {
        "processed_at": now_iso,
        "scanned_count": len(rows),
        "marked_late_count": len(marked_late_ids),
        "marked_late_ids": marked_late_ids,
        "marked_no_show_count": len(marked_noshow_ids),
        "marked_no_show_ids": marked_noshow_ids,
    }


