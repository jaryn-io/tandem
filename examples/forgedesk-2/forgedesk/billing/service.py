"""Machine pricing, usage charge snapshot finalization, and explicit adjustments service.

Implements exact financial arithmetic (no floating-point rounding errors) using Decimal,
peak/off-peak time-window calculation in Europe/Rome timezone, immutable finalized
charge records, and administrator-owned explicit adjustments.
"""

import datetime
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from forgedesk.audit.service import record_audit_event
from forgedesk.auth.permissions import can_adjust_charges, can_view_charges
from forgedesk.db.connection import get_connection, query_all, query_one, transaction
from forgedesk.utils.datetime_tz import (
    APP_TIMEZONE,
    format_display,
    format_iso,
    now_rome,
    now_rome_iso,
    parse_datetime,
)

logger = logging.getLogger("forgedesk.billing.service")


# -----------------------------------------------------------------------------
# Formatting and Conversion Utilities
# -----------------------------------------------------------------------------

def format_cents_currency(cents: Optional[int], symbol: str = "€") -> str:
    """Format an integer number of cents as a formatted currency string (e.g. 1250 -> '€12.50')."""
    if cents is None:
        return f"{symbol}0.00"
    dec = Decimal(cents) / Decimal(100)
    # Format with 2 decimal places
    if cents < 0:
        return f"-{symbol}{abs(dec):.2f}"
    return f"{symbol}{dec:.2f}"


def parse_currency_to_cents(val: Union[str, int, float, Decimal], field_name: str = "Amount") -> int:
    """Parse a user-entered monetary amount (e.g. '12.50', '-5.00', '1250') into integer cents without floating point errors."""
    if val is None or str(val).strip() == "":
        raise ValueError(f"{field_name} cannot be empty.")

    clean = str(val).strip().replace("€", "").replace("$", "").replace(" ", "").replace(",", ".")
    try:
        dec = Decimal(clean)
        # Check if the number has more than 2 decimal places
        cents = int((dec * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return cents
    except Exception as e:
        raise ValueError(f"{field_name} must be a valid numeric currency amount (e.g. '12.50' or '-5.00', got '{val}').") from e


# -----------------------------------------------------------------------------
# Peak and Off-Peak Time Interval Breakdown
# -----------------------------------------------------------------------------

def compute_peak_offpeak_minutes(
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
    peak_start_str: Optional[str] = "17:00",
    peak_end_str: Optional[str] = "21:00",
) -> Tuple[int, int]:
    """Calculate the exact number of off-peak and peak minutes within a time interval in Rome timezone.

    Returns:
        (offpeak_minutes, peak_minutes)
    """
    if end_dt <= start_dt:
        return 0, 0

    # Ensure timezone awareness in APP_TIMEZONE
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=APP_TIMEZONE)
    else:
        start_dt = start_dt.astimezone(APP_TIMEZONE)

    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=APP_TIMEZONE)
    else:
        end_dt = end_dt.astimezone(APP_TIMEZONE)

    total_duration_minutes = max(0, int((end_dt - start_dt).total_seconds() // 60))
    if total_duration_minutes == 0:
        return 0, 0

    if not peak_start_str or not peak_end_str:
        return total_duration_minutes, 0

    p_s = peak_start_str.strip()
    p_e = peak_end_str.strip()
    if not p_s or not p_e:
        return total_duration_minutes, 0

    try:
        sh, sm = map(int, p_s.split(":"))
        eh, em = map(int, p_e.split(":"))
    except Exception:
        return total_duration_minutes, 0

    if (sh, sm) == (eh, em):
        return total_duration_minutes, 0

    peak_minutes = 0

    # Iterate day by day from start_dt date to end_dt date
    curr_date = start_dt.date()
    end_date = end_dt.date()

    while curr_date <= end_date:
        day_start = datetime.datetime.combine(curr_date, datetime.time.min).replace(tzinfo=APP_TIMEZONE)
        day_end = datetime.datetime.combine(curr_date + datetime.timedelta(days=1), datetime.time.min).replace(tzinfo=APP_TIMEZONE)

        interval_start = max(start_dt, day_start)
        interval_end = min(end_dt, day_end)

        if interval_start < interval_end:
            if (sh * 60 + sm) < (eh * 60 + em):
                # Standard daytime peak window (e.g. 17:00 to 21:00)
                peak_w_start = datetime.datetime.combine(curr_date, datetime.time(sh, sm)).replace(tzinfo=APP_TIMEZONE)
                peak_w_end = datetime.datetime.combine(curr_date, datetime.time(eh, em)).replace(tzinfo=APP_TIMEZONE)

                ov_start = max(interval_start, peak_w_start)
                ov_end = min(interval_end, peak_w_end)
                if ov_start < ov_end:
                    peak_minutes += int((ov_end - ov_start).total_seconds() // 60)
            else:
                # Overnight peak window (e.g. 22:00 to 06:00)
                # Window 1: 00:00 to eh:em
                p1_start = datetime.datetime.combine(curr_date, datetime.time.min).replace(tzinfo=APP_TIMEZONE)
                p1_end = datetime.datetime.combine(curr_date, datetime.time(eh, em)).replace(tzinfo=APP_TIMEZONE)
                ov1_s = max(interval_start, p1_start)
                ov1_e = min(interval_end, p1_end)
                if ov1_s < ov1_e:
                    peak_minutes += int((ov1_e - ov1_s).total_seconds() // 60)

                # Window 2: sh:sm to 24:00
                p2_start = datetime.datetime.combine(curr_date, datetime.time(sh, sm)).replace(tzinfo=APP_TIMEZONE)
                p2_end = datetime.datetime.combine(curr_date + datetime.timedelta(days=1), datetime.time.min).replace(tzinfo=APP_TIMEZONE)
                ov2_s = max(interval_start, p2_start)
                ov2_e = min(interval_end, p2_end)
                if ov2_s < ov2_e:
                    peak_minutes += int((ov2_e - ov2_s).total_seconds() // 60)

        curr_date += datetime.timedelta(days=1)

    peak_minutes = min(total_duration_minutes, max(0, peak_minutes))
    offpeak_minutes = max(0, total_duration_minutes - peak_minutes)

    return offpeak_minutes, peak_minutes


# -----------------------------------------------------------------------------
# Pricing Calculation Engine (Exact Decimal Arithmetic)
# -----------------------------------------------------------------------------

def calculate_usage_charge(
    machine_or_id: Union[int, Dict[str, Any]],
    start_time: Union[str, datetime.datetime],
    end_time: Union[str, datetime.datetime],
    rates_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Calculate the precise usage charge breakdown using exact Decimal arithmetic.

    Guarantees no floating-point rounding errors, supports standard hourly rates,
    peak/off-peak rate windows, and minimum machine charge thresholds.

    Returns:
        Structured breakdown dictionary suitable for JSON serialization and audit trails.
    """
    if isinstance(machine_or_id, dict):
        machine = machine_or_id
    else:
        machine = query_one("SELECT * FROM machines WHERE id = ?;", (int(machine_or_id),))
        if not machine:
            raise ValueError(f"Machine #{machine_or_id} not found.")

    if rates_override:
        if not isinstance(rates_override, dict):
            raise ValueError("rates_override must be a dictionary.")
        # Validate rates_override monetary fields
        for field in ("hourly_rate_cents", "minimum_charge_cents", "peak_hourly_rate_cents"):
            if field in rates_override and rates_override[field] is not None:
                try:
                    val = int(rates_override[field])
                    if val < 0:
                        raise ValueError(f"rates_override {field} cannot be negative.")
                except (ValueError, TypeError) as e:
                    raise ValueError(f"Invalid rates_override {field}: {e}")
        # Validate peak_hours format HH:MM
        for field in ("peak_hours_start", "peak_hours_end"):
            if field in rates_override and rates_override[field]:
                p_str = str(rates_override[field]).strip()
                try:
                    p_parts = p_str.split(":")
                    if len(p_parts) != 2 or not (0 <= int(p_parts[0]) <= 23) or not (0 <= int(p_parts[1]) <= 59):
                        raise ValueError()
                except Exception:
                    raise ValueError(f"Invalid rates_override {field} format '{p_str}'. Expected HH:MM.")

    rates = rates_override or {}

    hourly_rate_cents = int(rates.get("hourly_rate_cents", machine.get("hourly_rate_cents", 0)) or 0)
    minimum_charge_cents = int(rates.get("minimum_charge_cents", machine.get("minimum_charge_cents", 0)) or 0)
    peak_hourly_rate_cents = int(
        rates.get("peak_hourly_rate_cents", machine.get("peak_hourly_rate_cents", 0)) or hourly_rate_cents
    )
    peak_hours_start = rates.get("peak_hours_start", machine.get("peak_hours_start") or "17:00")
    peak_hours_end = rates.get("peak_hours_end", machine.get("peak_hours_end") or "21:00")

    s_dt = parse_datetime(start_time)
    e_dt = parse_datetime(end_time)

    if e_dt < s_dt:
        e_dt = s_dt

    offpeak_minutes, peak_minutes = compute_peak_offpeak_minutes(
        s_dt, e_dt, peak_start_str=peak_hours_start, peak_end_str=peak_hours_end
    )
    total_minutes = offpeak_minutes + peak_minutes

    # Decimal arithmetic for currency calculations without float rounding errors
    dec_60 = Decimal(60)
    dec_offpeak_mins = Decimal(offpeak_minutes)
    dec_peak_mins = Decimal(peak_minutes)
    dec_hourly_rate = Decimal(hourly_rate_cents)
    dec_peak_rate = Decimal(peak_hourly_rate_cents)

    # Exact rounded cents: (minutes * rate / 60) rounded half up to nearest integer cent
    if offpeak_minutes > 0 and hourly_rate_cents > 0:
        offpeak_cost_cents = int(
            ((dec_offpeak_mins * dec_hourly_rate) / dec_60).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    else:
        offpeak_cost_cents = 0

    if peak_minutes > 0 and peak_hourly_rate_cents > 0:
        peak_cost_cents = int(
            ((dec_peak_mins * dec_peak_rate) / dec_60).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    else:
        peak_cost_cents = 0

    base_charge_cents = offpeak_cost_cents + peak_cost_cents
    final_charge_cents = max(minimum_charge_cents, base_charge_cents)
    minimum_applied = final_charge_cents > base_charge_cents

    duration_hours = round(float(Decimal(total_minutes) / dec_60), 2)
    offpeak_hours = round(float(Decimal(offpeak_minutes) / dec_60), 2)
    peak_hours = round(float(Decimal(peak_minutes) / dec_60), 2)

    is_simulation = bool(rates_override)

    return {
        "duration_minutes": total_minutes,
        "duration_hours": duration_hours,
        "offpeak_minutes": offpeak_minutes,
        "offpeak_hours": offpeak_hours,
        "peak_minutes": peak_minutes,
        "peak_hours": peak_hours,
        "hourly_rate_cents": hourly_rate_cents,
        "peak_hourly_rate_cents": peak_hourly_rate_cents,
        "minimum_charge_cents": minimum_charge_cents,
        "peak_hours_start": peak_hours_start,
        "peak_hours_end": peak_hours_end,
        "offpeak_cost_cents": offpeak_cost_cents,
        "peak_cost_cents": peak_cost_cents,
        "base_charge_cents": base_charge_cents,
        "minimum_charge_applied": minimum_applied,
        "final_charge_cents": final_charge_cents,
        "estimated_total_cents": final_charge_cents,  # backward compatibility alias
        "currency": "EUR",
        "formatted_base_charge": format_cents_currency(base_charge_cents),
        "formatted_final_charge": format_cents_currency(final_charge_cents),
        "formatted_hourly_rate": format_cents_currency(hourly_rate_cents),
        "formatted_peak_rate": format_cents_currency(peak_hourly_rate_cents),
        "formatted_minimum_charge": format_cents_currency(minimum_charge_cents),
        "is_simulation": is_simulation,
        "rates_source": "override_simulation" if is_simulation else "server_database",
        "disclaimer": "Non-binding rate simulation. Official checkout charges are calculated strictly using server-configured machine tariffs." if is_simulation else None,
    }


# -----------------------------------------------------------------------------
# Finalized Charge Snapshot Creation (on Checkout)
# -----------------------------------------------------------------------------

def create_usage_charge_for_checkout(
    reservation_id: int,
    check_in_time: Union[str, datetime.datetime],
    check_out_time: Union[str, datetime.datetime],
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    conn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Create and persist an immutable usage charge snapshot at checkout time.

    If a charge already exists for this reservation, returns the existing record.
    Preserves historical rates even if machine tariffs are updated in the future.
    """
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        # Check if charge already exists for reservation
        existing_charge = conn.execute(
            "SELECT * FROM usage_charges WHERE reservation_id = ?;", (int(reservation_id),)
        ).fetchone()
        if existing_charge:
            return dict(existing_charge)

        # Retrieve reservation and machine data
        res_row = conn.execute(
            "SELECT r.*, m.id AS m_id, m.code AS machine_code, m.name AS machine_name, "
            "m.hourly_rate_cents, m.minimum_charge_cents, m.peak_hourly_rate_cents, "
            "m.peak_hours_start, m.peak_hours_end, mem.full_name AS member_name "
            "FROM reservations r "
            "JOIN machines m ON m.id = r.machine_id "
            "JOIN members mem ON mem.id = r.member_id "
            "WHERE r.id = ?;",
            (int(reservation_id),),
        ).fetchone()

        if not res_row:
            raise ValueError(f"Reservation #{reservation_id} not found.")

        machine_data = {
            "id": res_row["machine_id"],
            "code": res_row["machine_code"],
            "name": res_row["machine_name"],
            "hourly_rate_cents": res_row["hourly_rate_cents"],
            "minimum_charge_cents": res_row["minimum_charge_cents"],
            "peak_hourly_rate_cents": res_row["peak_hourly_rate_cents"],
            "peak_hours_start": res_row["peak_hours_start"],
            "peak_hours_end": res_row["peak_hours_end"],
        }

        s_dt = parse_datetime(check_in_time)
        e_dt = parse_datetime(check_out_time)
        if e_dt < s_dt:
            e_dt = s_dt

        s_iso = format_iso(s_dt)
        e_iso = format_iso(e_dt)
        now_str = now_rome_iso()

        breakdown = calculate_usage_charge(machine_data, s_iso, e_iso)
        breakdown_json = json.dumps(breakdown)

        cur = conn.execute(
            """
            INSERT INTO usage_charges (
                reservation_id, machine_id, member_id, start_time, end_time,
                duration_minutes, rate_breakdown_json, base_charge_cents,
                final_charge_cents, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'finalized', ?, ?);
            """,
            (
                int(reservation_id),
                int(res_row["machine_id"]),
                int(res_row["member_id"]),
                s_iso,
                e_iso,
                breakdown["duration_minutes"],
                breakdown_json,
                breakdown["base_charge_cents"],
                breakdown["final_charge_cents"],
                now_str,
                now_str,
            ),
        )
        charge_id = cur.lastrowid

        # Record audit log inside the same transaction
        record_audit_event(
            action="charge:created",
            object_type="usage_charge",
            object_id=charge_id,
            actor=actor_user,
            details={
                "reservation_id": reservation_id,
                "machine_id": res_row["machine_id"],
                "member_id": res_row["member_id"],
                "duration_minutes": breakdown["duration_minutes"],
                "base_charge_cents": breakdown["base_charge_cents"],
                "final_charge_cents": breakdown["final_charge_cents"],
                "rate_breakdown": breakdown,
            },
            after={
                "id": charge_id,
                "reservation_id": reservation_id,
                "machine_id": res_row["machine_id"],
                "member_id": res_row["member_id"],
                "start_time": s_iso,
                "end_time": e_iso,
                "duration_minutes": breakdown["duration_minutes"],
                "base_charge_cents": breakdown["base_charge_cents"],
                "final_charge_cents": breakdown["final_charge_cents"],
                "status": "finalized",
                "created_at": now_str,
            },
            ip_address=ip_address,
            conn=conn,
        )

        logger.info(
            "Finalized usage charge #%d created for reservation #%d: base €%.2f, final €%.2f (%d min).",
            charge_id,
            reservation_id,
            breakdown["base_charge_cents"] / 100.0,
            breakdown["final_charge_cents"] / 100.0,
            breakdown["duration_minutes"],
        )

        return get_usage_charge_by_id(charge_id, conn=conn) or {}
    finally:
        if owns_conn:
            conn.close()


# -----------------------------------------------------------------------------
# Charge Queries
# -----------------------------------------------------------------------------

def get_usage_charge_by_id(
    charge_id: int,
    include_adjustments: bool = True,
    conn: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve usage charge by ID with parsed breakdown, machine/member info, and adjustment ledger."""
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT c.*,
                   m.code AS machine_code, m.name AS machine_name,
                   mem.member_number, mem.full_name AS member_name, mem.user_id AS member_user_id,
                   r.title AS reservation_title
            FROM usage_charges c
            JOIN machines m ON m.id = c.machine_id
            JOIN members mem ON mem.id = c.member_id
            LEFT JOIN reservations r ON r.id = c.reservation_id
            WHERE c.id = ?;
            """,
            (int(charge_id),),
        )
        row = cur.fetchone()
        if not row:
            return None

        charge = dict(row)
        try:
            charge["rate_breakdown"] = json.loads(charge.get("rate_breakdown_json") or "{}")
        except Exception:
            charge["rate_breakdown"] = {}

        charge["formatted_base_charge"] = format_cents_currency(charge.get("base_charge_cents", 0))
        charge["formatted_final_charge"] = format_cents_currency(charge.get("final_charge_cents", 0))
        charge["formatted_start_time"] = format_display(charge.get("start_time"))
        charge["formatted_end_time"] = format_display(charge.get("end_time"))
        charge["formatted_created_at"] = format_display(charge.get("created_at"))

        if include_adjustments:
            cur.execute(
                """
                SELECT a.*, u.username AS actor_username, u.full_name AS actor_name, u.role AS actor_role
                FROM charge_adjustments a
                LEFT JOIN users u ON u.id = a.actor_id
                WHERE a.charge_id = ?
                ORDER BY a.created_at ASC, a.id ASC;
                """,
                (int(charge_id),),
            )
            adjustments = [dict(r) for r in cur.fetchall()]
            total_adj_cents = sum(a.get("adjustment_cents", 0) for a in adjustments)
            for a in adjustments:
                a["formatted_amount"] = format_cents_currency(a.get("adjustment_cents", 0))
                a["formatted_created_at"] = format_display(a.get("created_at"))

            charge["adjustments"] = adjustments
            charge["adjustments_count"] = len(adjustments)
            charge["total_adjustments_cents"] = total_adj_cents
            charge["formatted_total_adjustments"] = format_cents_currency(total_adj_cents)

        return charge
    finally:
        if owns_conn:
            conn.close()


def get_usage_charge_by_reservation_id(
    reservation_id: int,
    conn: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve usage charge associated with a reservation."""
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM usage_charges WHERE reservation_id = ?;", (int(reservation_id),))
        row = cur.fetchone()
        if not row:
            return None
        return get_usage_charge_by_id(row["id"], conn=conn)
    finally:
        if owns_conn:
            conn.close()


def list_usage_charges(
    member_id: Optional[int] = None,
    machine_id: Optional[int] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    actor_user: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    """List usage charges with comprehensive filtering, search, pagination, and summary statistics.

    Enforces ownership permissions: regular members can only view their own charges.
    Returns: (charges_list, total_count, summary_stats)
    """
    # Enforce RBAC filtering for members
    if actor_user and actor_user.get("role") == "member":
        user_member_id = actor_user.get("member_id")
        if user_member_id is not None:
            member_id = user_member_id

    query = """
        SELECT c.*,
               m.code AS machine_code, m.name AS machine_name,
               mem.member_number, mem.full_name AS member_name, mem.user_id AS member_user_id,
               r.title AS reservation_title,
               (
                   SELECT COALESCE(SUM(a.adjustment_cents), 0)
                   FROM charge_adjustments a
                   WHERE a.charge_id = c.id
               ) AS total_adjustments_cents,
               (
                   SELECT COUNT(*)
                   FROM charge_adjustments a
                   WHERE a.charge_id = c.id
               ) AS adjustments_count
        FROM usage_charges c
        JOIN machines m ON m.id = c.machine_id
        JOIN members mem ON mem.id = c.member_id
        LEFT JOIN reservations r ON r.id = c.reservation_id
        WHERE 1=1
    """
    params: List[Any] = []

    if member_id is not None:
        query += " AND c.member_id = ?"
        params.append(int(member_id))

    if machine_id is not None:
        query += " AND c.machine_id = ?"
        params.append(int(machine_id))

    if status and status.strip():
        query += " AND c.status = ?"
        params.append(status.strip().lower())

    if date_from and date_from.strip():
        query += " AND c.start_time >= ?"
        params.append(date_from.strip())

    if date_to and date_to.strip():
        # If date only (YYYY-MM-DD), append end of day
        d_to = date_to.strip()
        if len(d_to) == 10:
            d_to = f"{d_to}T23:59:59+02:00"
        query += " AND c.start_time <= ?"
        params.append(d_to)

    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query += """ AND (
            LOWER(mem.full_name) LIKE ? OR
            LOWER(mem.member_number) LIKE ? OR
            LOWER(m.name) LIKE ? OR
            LOWER(m.code) LIKE ? OR
            LOWER(COALESCE(r.title, '')) LIKE ?
        )"""
        params.extend([term, term, term, term, term])

    # Calculate summary totals for the filtered set
    summary_query = f"""
        SELECT COUNT(*) AS total_count,
               COALESCE(SUM(c.base_charge_cents), 0) AS sum_base_charge,
               COALESCE(SUM(c.final_charge_cents), 0) AS sum_final_charge
        FROM ({query}) AS c
    """

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(summary_query, params)
        sum_row = cur.fetchone()
        total_count = sum_row["total_count"] if sum_row else 0
        sum_base = sum_row["sum_base_charge"] if sum_row else 0
        sum_final = sum_row["sum_final_charge"] if sum_row else 0
        sum_adjustments = sum_final - sum_base

        summary_stats = {
            "total_count": total_count,
            "sum_base_charge_cents": sum_base,
            "sum_final_charge_cents": sum_final,
            "sum_adjustments_cents": sum_adjustments,
            "formatted_sum_base": format_cents_currency(sum_base),
            "formatted_sum_final": format_cents_currency(sum_final),
            "formatted_sum_adjustments": format_cents_currency(sum_adjustments),
        }

        # Query paginated rows
        paginated_query = query + " ORDER BY c.created_at DESC, c.id DESC LIMIT ? OFFSET ?;"
        paginated_params = list(params) + [max(1, int(limit)), max(0, int(offset))]

        cur.execute(paginated_query, paginated_params)
        raw_charges = [dict(r) for r in cur.fetchall()]

        charges: List[Dict[str, Any]] = []
        for c in raw_charges:
            try:
                c["rate_breakdown"] = json.loads(c.get("rate_breakdown_json") or "{}")
            except Exception:
                c["rate_breakdown"] = {}

            c["formatted_base_charge"] = format_cents_currency(c.get("base_charge_cents", 0))
            c["formatted_final_charge"] = format_cents_currency(c.get("final_charge_cents", 0))
            c["formatted_total_adjustments"] = format_cents_currency(c.get("total_adjustments_cents", 0))
            c["formatted_start_time"] = format_display(c.get("start_time"))
            c["formatted_end_time"] = format_display(c.get("end_time"))
            c["formatted_created_at"] = format_display(c.get("created_at"))
            charges.append(c)

        return charges, total_count, summary_stats
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Explicit Charge Adjustments (Administrator Only)
# -----------------------------------------------------------------------------

def create_charge_adjustment(
    charge_id: int,
    adjustment_cents: int,
    reason: str,
    actor_user: Optional[Dict[str, Any]],
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an explicit financial adjustment on an existing usage charge.

    Strictly restricted to Administrators (`can_adjust_charges(actor_user)`).
    Does NOT rewrite original base charge or rate snapshot: appends an adjustment record,
    updates final_charge_cents and status to 'adjusted', and records an immutable audit event.
    """
    if not actor_user or not can_adjust_charges(actor_user):
        raise PermissionError("Only administrators are authorized to create financial charge adjustments.")

    if not reason or not reason.strip():
        raise ValueError("A clear, non-empty reason is required for financial adjustments.")

    clean_reason = reason.strip()
    adj_cents = int(adjustment_cents)
    if adj_cents == 0:
        raise ValueError("Adjustment amount cannot be zero.")

    now_str = now_rome_iso()

    with transaction() as tx:
        cur = tx.cursor()
        cur.execute("SELECT * FROM usage_charges WHERE id = ?;", (int(charge_id),))
        charge_row = cur.fetchone()
        if not charge_row:
            raise ValueError(f"Usage charge #{charge_id} not found.")

        before_charge = dict(charge_row)

        # 1. Insert new adjustment entry into append-only ledger
        cur.execute(
            """
            INSERT INTO charge_adjustments (charge_id, adjustment_cents, reason, actor_id, created_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (int(charge_id), adj_cents, clean_reason, int(actor_user["id"]), now_str),
        )
        adj_id = cur.lastrowid

        # 2. Recalculate total adjustments and update final charge
        cur.execute(
            "SELECT COALESCE(SUM(adjustment_cents), 0) AS total_adj FROM charge_adjustments WHERE charge_id = ?;",
            (int(charge_id),),
        )
        total_adj_row = cur.fetchone()
        total_adj = total_adj_row["total_adj"] if total_adj_row else adj_cents

        base_cents = before_charge["base_charge_cents"]
        new_final_cents = base_cents + total_adj

        cur.execute(
            """
            UPDATE usage_charges
            SET final_charge_cents = ?,
                status = 'adjusted',
                updated_at = ?
            WHERE id = ?;
            """,
            (new_final_cents, now_str, int(charge_id)),
        )

        after_charge = {
            **before_charge,
            "final_charge_cents": new_final_cents,
            "status": "adjusted",
            "updated_at": now_str,
            "total_adjustments_cents": total_adj,
        }

        # 3. Record append-only audit event
        record_audit_event(
            action="charge:adjusted",
            object_type="usage_charge",
            object_id=charge_id,
            actor=actor_user,
            details={
                "adjustment_id": adj_id,
                "adjustment_cents": adj_cents,
                "formatted_adjustment": format_cents_currency(adj_cents),
                "reason": clean_reason,
                "previous_final_charge_cents": before_charge["final_charge_cents"],
                "new_final_charge_cents": new_final_cents,
                "total_adjustments_cents": total_adj,
            },
            before=before_charge,
            after=after_charge,
            ip_address=ip_address,
            conn=tx,
        )

    logger.info(
        "Charge #%d adjusted by %s (amount: €%.2f, new total: €%.2f, reason: '%s').",
        charge_id,
        actor_user.get("username", "admin"),
        adj_cents / 100.0,
        new_final_cents / 100.0,
        clean_reason,
    )

    updated_charge = get_usage_charge_by_id(charge_id)
    if not updated_charge:
        raise RuntimeError("Failed to reload updated usage charge.")
    return updated_charge


def list_charge_adjustments(
    charge_id: Optional[int] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List financial adjustments with actor details."""
    query = """
        SELECT a.*, u.username AS actor_username, u.full_name AS actor_name, u.role AS actor_role,
               c.reservation_id, c.member_id, mem.full_name AS member_name
        FROM charge_adjustments a
        JOIN usage_charges c ON c.id = a.charge_id
        JOIN members mem ON mem.id = c.member_id
        LEFT JOIN users u ON u.id = a.actor_id
        WHERE 1=1
    """
    params: List[Any] = []
    if charge_id is not None:
        query += " AND a.charge_id = ?"
        params.append(int(charge_id))

    query += " ORDER BY a.created_at DESC LIMIT ?;"
    params.append(max(1, int(limit)))

    rows = query_all(query, tuple(params))
    for r in rows:
        r["formatted_amount"] = format_cents_currency(r.get("adjustment_cents", 0))
        r["formatted_created_at"] = format_display(r.get("created_at"))
    return rows


# -----------------------------------------------------------------------------
# Billing Summary Aggregator
# -----------------------------------------------------------------------------

def get_billing_summary(
    member_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Calculate aggregate billing revenue and adjustment statistics."""
    query = """
        SELECT
            COUNT(*) AS total_charges,
            COALESCE(SUM(base_charge_cents), 0) AS total_base_cents,
            COALESCE(SUM(final_charge_cents), 0) AS total_final_cents,
            COUNT(DISTINCT member_id) AS distinct_members,
            COUNT(DISTINCT machine_id) AS distinct_machines,
            COALESCE(SUM(duration_minutes), 0) AS total_duration_minutes
        FROM usage_charges
        WHERE 1=1
    """
    params: List[Any] = []
    if member_id is not None:
        query += " AND member_id = ?"
        params.append(int(member_id))
    if date_from:
        query += " AND start_time >= ?"
        params.append(date_from)
    if date_to:
        d_to = date_to
        if len(d_to) == 10:
            d_to = f"{d_to}T23:59:59+02:00"
        query += " AND start_time <= ?"
        params.append(d_to)

    row = query_one(query, tuple(params)) or {}

    total_charges = row.get("total_charges", 0)
    total_base = row.get("total_base_cents", 0)
    total_final = row.get("total_final_cents", 0)
    total_adj = total_final - total_base
    total_mins = row.get("total_duration_minutes", 0)

    avg_charge_cents = int(total_final // total_charges) if total_charges > 0 else 0

    return {
        "total_charges_count": total_charges,
        "total_base_cents": total_base,
        "total_final_cents": total_final,
        "total_adjustments_cents": total_adj,
        "distinct_members": row.get("distinct_members", 0),
        "distinct_machines": row.get("distinct_machines", 0),
        "total_duration_minutes": total_mins,
        "total_duration_hours": round(total_mins / 60.0, 1),
        "average_charge_cents": avg_charge_cents,
        "formatted_total_base": format_cents_currency(total_base),
        "formatted_total_final": format_cents_currency(total_final),
        "formatted_total_adjustments": format_cents_currency(total_adj),
        "formatted_average_charge": format_cents_currency(avg_charge_cents),
    }
