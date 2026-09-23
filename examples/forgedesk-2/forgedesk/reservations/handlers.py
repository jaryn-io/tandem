"""HTTP and REST API route handlers for Reservations, Availability, Conflicts, and Timelines."""

import html
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from forgedesk.auth.middleware import require_auth
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
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.core.templates import csrf_input, escape_html, render_page
from forgedesk.db.connection import query_all, query_one
from forgedesk.members.service import get_member_by_id, get_member_by_user_id, list_members
from forgedesk.reservations.availability import (
    check_machine_availability,
    find_interval_conflicts,
    generate_timeline_slots,
    get_batch_machine_availability,
    get_historical_reservations_for_machine,
)
from forgedesk.reservations.recurrence import (
    MAX_RECURRENCE_COUNT,
    RecurrenceRuleError,
    WEEKDAY_CODES,
    WEEKDAY_NAMES,
    format_recurrence_summary,
    parse_recurrence_rule,
)
from forgedesk.reservations.service import (
    ACTIVE_RESERVATION_STATUSES,
    VALID_RESERVATION_STATUSES,
    ReservationConflictError,
    ReservationEligibilityError,
    ReservationNotFoundError,
    ReservationPermissionError,
    cancel_recurring_series,
    cancel_reservation,
    check_in_reservation,
    check_out_reservation,
    create_recurring_reservations,
    create_reservation,
    estimate_reservation_charge,
    format_conflict_explanation,
    get_recurring_series,
    get_reservation_by_id,
    list_reservations,
    mark_reservation_late,
    mark_reservation_no_show,
    process_overdue_reservations,
    update_reservation,
)
from forgedesk.reservations.waiting_list import (
    VALID_WAITING_STATUSES,
    WaitingListEligibilityError,
    WaitingListError,
    WaitingListNotFoundError,
    WaitingListPermissionError,
    cancel_waiting_list_entry,
    create_waiting_list_entry,
    evaluate_entry_eligibility,
    get_waiting_list_entry_by_id,
    list_waiting_list_entries,
    promote_first_eligible_waiting_entry,
    promote_waiting_list_entry_by_id,
)
from forgedesk.utils.datetime_tz import (
    format_display,
    format_iso,
    now_rome,
    now_rome_iso,
    parse_datetime,
    today_rome_str,
)

logger = logging.getLogger("forgedesk.reservations.handlers")


def register_reservation_routes(router: Router) -> None:
    """Register all reservation management and availability endpoints on the router."""

    # -------------------------------------------------------------------------
    # REST API: Check Single Machine Availability
    # -------------------------------------------------------------------------
    @router.get("/api/availability/check")
    def api_check_availability(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        machine_id_raw = req.query("machine_id")
        start_time = req.query("start_time")
        end_time = req.query("end_time")

        if not machine_id_raw or not start_time or not end_time:
            return Response.json(
                {
                    "error": "Missing required query parameters: 'machine_id', 'start_time', 'end_time'.",
                    "code": "missing_params",
                },
                status_code=400,
            )

        try:
            machine_id = int(machine_id_raw)
        except ValueError:
            return Response.json({"error": "Invalid machine_id (integer required)."}, status_code=400)

        member_id: Optional[int] = None
        member_id_raw = req.query("member_id")
        if member_id_raw:
            try:
                member_id = int(member_id_raw)
            except ValueError:
                return Response.json({"error": "Invalid member_id."}, status_code=400)
        elif req.user.get("role") == "member":
            m = get_member_by_user_id(req.user["id"], include_qualifications=False)
            if m:
                member_id = m["id"]

        exclude_id: Optional[int] = None
        exclude_raw = req.query("exclude_reservation_id")
        if exclude_raw:
            try:
                exclude_id = int(exclude_raw)
            except ValueError:
                return Response.json({"error": "Invalid exclude_reservation_id."}, status_code=400)

        is_avail, msg, conflicts, metadata = check_machine_availability(
            machine_id=machine_id,
            start_time=start_time,
            end_time=end_time,
            member_id=member_id,
            exclude_reservation_id=exclude_id,
            check_qualifications=True,
        )

        return Response.json({
            "success": True,
            "is_available": is_avail,
            "message": msg,
            "metadata": metadata,
            "conflicts": conflicts,
        })

    # -------------------------------------------------------------------------
    # REST API: Detailed Conflict Inspection
    # -------------------------------------------------------------------------
    @router.get("/api/availability/conflicts")
    def api_find_conflicts(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        machine_id_raw = req.query("machine_id")
        start_time = req.query("start_time")
        end_time = req.query("end_time")

        if not machine_id_raw or not start_time or not end_time:
            return Response.json(
                {"error": "Missing required query parameters: 'machine_id', 'start_time', 'end_time'."},
                status_code=400,
            )

        try:
            machine_id = int(machine_id_raw)
        except ValueError:
            return Response.json({"error": "Invalid machine_id."}, status_code=400)

        member_id: Optional[int] = None
        member_id_raw = req.query("member_id")
        if member_id_raw:
            try:
                member_id = int(member_id_raw)
            except ValueError:
                return Response.json({"error": "Invalid member_id."}, status_code=400)

        exclude_id: Optional[int] = None
        exclude_raw = req.query("exclude_reservation_id")
        if exclude_raw:
            try:
                exclude_id = int(exclude_raw)
            except ValueError:
                return Response.json({"error": "Invalid exclude_reservation_id."}, status_code=400)

        conflicts = find_interval_conflicts(
            machine_id=machine_id,
            start_time=start_time,
            end_time=end_time,
            member_id=member_id,
            exclude_reservation_id=exclude_id,
            check_qualifications=True,
        )

        return Response.json({
            "success": True,
            "machine_id": machine_id,
            "conflicts_count": len(conflicts),
            "conflicts": conflicts,
        })

    # -------------------------------------------------------------------------
    # REST API: Machine Daily Timeline Slots
    # -------------------------------------------------------------------------
    @router.get("/api/availability/timeline")
    def api_timeline_slots(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        machine_id_raw = req.query("machine_id")
        if not machine_id_raw:
            return Response.json({"error": "Missing 'machine_id' query parameter."}, status_code=400)

        try:
            machine_id = int(machine_id_raw)
        except ValueError:
            return Response.json({"error": "Invalid machine_id."}, status_code=400)

        date_str = req.query("date") or today_rome_str()
        duration_raw = req.query("slot_duration_minutes") or "30"
        try:
            slot_duration = int(duration_raw)
            if slot_duration not in (15, 30, 60, 120):
                slot_duration = 30
        except ValueError:
            slot_duration = 30

        try:
            timeline = generate_timeline_slots(
                machine_id=machine_id,
                date_str=date_str,
                slot_duration_minutes=slot_duration,
            )
            return Response.json({"success": True, "timeline": timeline})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=404)
        except Exception as e:
            logger.error("Error generating timeline: %s", e)
            return Response.json({"error": f"Failed to generate timeline: {e}"}, status_code=500)

    # -------------------------------------------------------------------------
    # REST API: Batch Machine Availability for a Time Window
    # -------------------------------------------------------------------------
    @router.get("/api/availability/machines")
    def api_batch_availability(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        start_time = req.query("start_time")
        end_time = req.query("end_time")

        if not start_time or not end_time:
            return Response.json(
                {"error": "Query parameters 'start_time' and 'end_time' are required."},
                status_code=400,
            )

        cat_id: Optional[int] = None
        cat_raw = req.query("category_id")
        if cat_raw:
            try:
                cat_id = int(cat_raw)
            except ValueError:
                return Response.json({"error": "Invalid category_id."}, status_code=400)

        member_id: Optional[int] = None
        if req.user.get("role") == "member":
            m = get_member_by_user_id(req.user["id"], include_qualifications=False)
            if m:
                member_id = m["id"]

        try:
            results = get_batch_machine_availability(
                start_time=start_time,
                end_time=end_time,
                category_id=cat_id,
                member_id=member_id,
            )
            return Response.json({
                "success": True,
                "start_time": start_time,
                "end_time": end_time,
                "total_machines": len(results),
                "available_machines": sum(1 for r in results if r["is_available"]),
                "machines": results,
            })
        except Exception as e:
            logger.error("Error in batch availability: %s", e)
            return Response.json({"error": f"Batch availability check failed: {e}"}, status_code=400)

    # -------------------------------------------------------------------------
    # REST API: Historical Reservations for Machine
    # -------------------------------------------------------------------------
    @router.get("/api/machines/{id}/historical-reservations")
    def api_machine_historical_reservations(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        machine_id_raw = req.route_params.get("id")
        try:
            machine_id = int(machine_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid machine ID."}, status_code=400)

        limit_raw = req.query("limit") or "50"
        offset_raw = req.query("offset") or "0"
        try:
            limit = min(max(1, int(limit_raw)), 200)
            offset = max(0, int(offset_raw))
        except ValueError:
            limit = 50
            offset = 0

        try:
            data = get_historical_reservations_for_machine(machine_id=machine_id, limit=limit, offset=offset)
            return Response.json({"success": True, "data": data})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=404)

    # -------------------------------------------------------------------------
    # REST API: Reservations Collection (List & Create)
    # -------------------------------------------------------------------------
    @router.get("/api/reservations")
    def api_list_reservations(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        user_role = get_user_role(req.user)
        machine_id_raw = req.query("machine_id")
        member_id_raw = req.query("member_id")
        cat_id_raw = req.query("category_id")
        status = req.query("status")
        start_date = req.query("start_date") or req.query("from_date")
        end_date = req.query("end_date") or req.query("to_date")
        search = req.query("q")
        limit_raw = req.query("limit") or "100"
        offset_raw = req.query("offset") or "0"

        machine_id = int(machine_id_raw) if machine_id_raw and machine_id_raw.isdigit() else None
        cat_id = int(cat_id_raw) if cat_id_raw and cat_id_raw.isdigit() else None
        limit = min(max(1, int(limit_raw) if limit_raw.isdigit() else 100), 500)
        offset = max(0, int(offset_raw) if offset_raw.isdigit() else 0)

        member_id: Optional[int] = None
        if member_id_raw and member_id_raw.isdigit():
            member_id = int(member_id_raw)

        results = list_reservations(
            machine_id=machine_id,
            member_id=member_id,
            category_id=cat_id,
            status=status,
            start_date=start_date,
            end_date=end_date,
            search=search,
            limit=limit,
            offset=offset,
        )

        return Response.json({
            "success": True,
            "count": len(results),
            "reservations": results,
        })

    @router.post("/api/reservations")
    def api_create_reservation(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        user_role = get_user_role(req.user)
        if user_role == ROLE_VIEWER:
            return Response.json({"error": "Viewers have read-only access and cannot create reservations."}, status_code=403)

        body = req.json() or {}
        machine_id_raw = body.get("machine_id")
        member_id_raw = body.get("member_id")
        start_time = body.get("start_time")
        end_time = body.get("end_time")
        title = body.get("title")

        if not machine_id_raw or not start_time or not end_time:
            return Response.json(
                {"error": "Missing required fields: 'machine_id', 'start_time', 'end_time'."},
                status_code=400,
            )

        try:
            machine_id = int(machine_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid machine_id."}, status_code=400)

        member_id: int
        if user_role == ROLE_MEMBER:
            user_member = get_member_by_user_id(req.user["id"], include_qualifications=False)
            if not user_member:
                return Response.json({"error": "User account is not linked to an active member profile."}, status_code=403)
            member_id = user_member["id"]
        else:
            if not member_id_raw:
                return Response.json({"error": "Field 'member_id' is required for staff bookings."}, status_code=400)
            try:
                member_id = int(member_id_raw)
            except (ValueError, TypeError):
                return Response.json({"error": "Invalid member_id."}, status_code=400)

        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            res_obj = create_reservation(
                machine_id=machine_id,
                member_id=member_id,
                start_time=start_time,
                end_time=end_time,
                title=title,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "reservation": res_obj}, status_code=201)
        except ReservationConflictError as e:
            return Response.json({
                "error": str(e),
                "code": "reservation_conflict",
                "conflicts": e.conflicts,
                "metadata": e.metadata,
            }, status_code=409)
        except ReservationPermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Unexpected error creating reservation: %s", e)
            return Response.json({"error": f"Failed to create reservation: {e}"}, status_code=500)

    # -------------------------------------------------------------------------
    # REST API: Recurring Reservations (Create Series, Get Group, Cancel Series)
    # -------------------------------------------------------------------------
    @router.post("/api/reservations/recurring")
    def api_create_recurring_reservations(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        user_role = get_user_role(req.user)
        if user_role == ROLE_VIEWER:
            return Response.json({"error": "Viewers have read-only access and cannot create recurring reservations."}, status_code=403)

        body = req.json() or {}
        machine_id_raw = body.get("machine_id")
        member_id_raw = body.get("member_id")
        start_date = body.get("start_date") or body.get("date")
        start_time_of_day = body.get("start_time") or body.get("start_time_of_day")
        end_time_of_day = body.get("end_time") or body.get("end_time_of_day")
        recurrence_rule = body.get("recurrence_rule") or body.get("rule")
        title = body.get("title")
        skip_conflicts = bool(body.get("skip_conflicts", False))

        if not machine_id_raw or not start_date or not start_time_of_day or not end_time_of_day or not recurrence_rule:
            return Response.json(
                {
                    "error": "Missing required fields: 'machine_id', 'start_date', 'start_time', 'end_time', 'recurrence_rule'.",
                    "code": "missing_params",
                },
                status_code=400,
            )

        try:
            machine_id = int(machine_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid machine_id."}, status_code=400)

        member_id: int
        if user_role == ROLE_MEMBER:
            user_member = get_member_by_user_id(req.user["id"], include_qualifications=False)
            if not user_member:
                return Response.json({"error": "User account is not linked to an active member profile."}, status_code=403)
            member_id = user_member["id"]
        else:
            if not member_id_raw:
                return Response.json({"error": "Field 'member_id' is required for staff bookings."}, status_code=400)
            try:
                member_id = int(member_id_raw)
            except (ValueError, TypeError):
                return Response.json({"error": "Invalid member_id."}, status_code=400)

        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            series_result = create_recurring_reservations(
                machine_id=machine_id,
                member_id=member_id,
                start_date=start_date,
                start_time_of_day=start_time_of_day,
                end_time_of_day=end_time_of_day,
                recurrence_rule=recurrence_rule,
                title=title,
                skip_conflicts=skip_conflicts,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "series": series_result}, status_code=201)
        except ReservationConflictError as e:
            return Response.json({
                "error": str(e),
                "code": "reservation_conflict",
                "conflicts": e.conflicts,
                "metadata": e.metadata,
            }, status_code=409)
        except (RecurrenceRuleError, ValueError) as e:
            return Response.json({"error": str(e), "code": "invalid_rule"}, status_code=400)
        except ReservationPermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except Exception as e:
            logger.error("Unexpected error creating recurring reservations: %s", e)
            return Response.json({"error": f"Failed to create recurring reservations: {e}"}, status_code=500)

    @router.get("/api/reservations/recurring/{group_id}")
    def api_get_recurring_series(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        group_id = req.route_params.get("group_id")
        if not group_id:
            return Response.json({"error": "Missing group_id."}, status_code=400)

        series = get_recurring_series(group_id)
        if not series:
            return Response.json({"error": f"Recurring series '{group_id}' not found."}, status_code=404)

        return Response.json({"success": True, "series": series})

    @router.post("/api/reservations/recurring/{group_id}/cancel")
    def api_cancel_recurring_series(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        group_id = req.route_params.get("group_id")
        if not group_id:
            return Response.json({"error": "Missing group_id."}, status_code=400)

        body = req.json() or {}
        reason = body.get("reason") or "Cancelled via API"
        future_only = bool(body.get("future_only", False))
        from_date = body.get("from_date")
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            cancel_result = cancel_recurring_series(
                recurrence_group_id=group_id,
                future_only=future_only,
                from_date=from_date,
                reason=reason,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "result": cancel_result})
        except ReservationNotFoundError as e:
            return Response.json({"error": str(e)}, status_code=404)
        except ReservationPermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except Exception as e:
            logger.error("Unexpected error cancelling recurring series %s: %s", group_id, e)
            return Response.json({"error": f"Failed to cancel recurring series: {e}"}, status_code=500)

    # -------------------------------------------------------------------------
    # REST API: Single Reservation Resource (Get, Update, Cancel)
    # -------------------------------------------------------------------------
    @router.get("/api/reservations/{id}")
    def api_get_reservation(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid reservation ID."}, status_code=400)

        res_obj = get_reservation_by_id(res_id, include_details=True)
        if not res_obj:
            return Response.json({"error": f"Reservation #{res_id} not found."}, status_code=404)

        return Response.json({"success": True, "reservation": res_obj})

    @router.put("/api/reservations/{id}")
    @router.patch("/api/reservations/{id}")
    def api_update_reservation(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid reservation ID."}, status_code=400)

        body = req.json() or {}
        machine_id = int(body["machine_id"]) if "machine_id" in body and body["machine_id"] is not None else None
        member_id = int(body["member_id"]) if "member_id" in body and body["member_id"] is not None else None
        start_time = body.get("start_time")
        end_time = body.get("end_time")
        title = body.get("title")
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            updated_obj = update_reservation(
                reservation_id=res_id,
                machine_id=machine_id,
                member_id=member_id,
                start_time=start_time,
                end_time=end_time,
                title=title,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "reservation": updated_obj})
        except ReservationNotFoundError as e:
            return Response.json({"error": str(e)}, status_code=404)
        except ReservationPermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except ReservationConflictError as e:
            return Response.json({
                "error": str(e),
                "code": "reservation_conflict",
                "conflicts": e.conflicts,
                "metadata": e.metadata,
            }, status_code=409)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Unexpected error updating reservation #%d: %s", res_id, e)
            return Response.json({"error": f"Failed to update reservation: {e}"}, status_code=500)

    @router.post("/api/reservations/{id}/cancel")
    @router.delete("/api/reservations/{id}")
    def api_cancel_reservation(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid reservation ID."}, status_code=400)

        body = req.json() or {}
        reason = body.get("reason") or req.query("reason") or "Cancelled via API"
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            cancelled_obj = cancel_reservation(
                reservation_id=res_id,
                reason=reason,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "reservation": cancelled_obj})
        except ReservationNotFoundError as e:
            return Response.json({"error": str(e)}, status_code=404)
        except ReservationPermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Unexpected error cancelling reservation #%d: %s", res_id, e)
            return Response.json({"error": f"Failed to cancel reservation: {e}"}, status_code=500)

    # -------------------------------------------------------------------------
    # REST API: Reservation Lifecycle Transitions (Check-In, Check-Out, Late, No-Show)
    # -------------------------------------------------------------------------
    @router.post("/api/reservations/{id}/check-in")
    def api_check_in_reservation(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid reservation ID."}, status_code=400)

        body = req.json() or {}
        check_in_time = body.get("actual_check_in_time") or body.get("check_in_time")
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            res_obj = check_in_reservation(
                reservation_id=res_id,
                actor_user=req.user,
                actual_check_in_time=check_in_time,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "reservation": res_obj})
        except ReservationNotFoundError as e:
            return Response.json({"error": str(e)}, status_code=404)
        except ReservationPermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except ReservationEligibilityError as e:
            return Response.json({"error": str(e), "code": "qualification_ineligible"}, status_code=409)
        except ReservationConflictError as e:
            return Response.json({
                "error": str(e),
                "code": "machine_unavailable",
                "conflicts": e.conflicts,
            }, status_code=409)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Unexpected error checking in reservation #%d: %s", res_id, e)
            return Response.json({"error": f"Failed to check in reservation: {e}"}, status_code=500)

    @router.post("/api/reservations/{id}/check-out")
    def api_check_out_reservation(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid reservation ID."}, status_code=400)

        body = req.json() or {}
        check_out_time = body.get("actual_check_out_time") or body.get("check_out_time")
        notes = body.get("notes")
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            res_obj = check_out_reservation(
                reservation_id=res_id,
                actor_user=req.user,
                actual_check_out_time=check_out_time,
                notes=notes,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "reservation": res_obj})
        except ReservationNotFoundError as e:
            return Response.json({"error": str(e)}, status_code=404)
        except ReservationPermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Unexpected error checking out reservation #%d: %s", res_id, e)
            return Response.json({"error": f"Failed to check out reservation: {e}"}, status_code=500)

    @router.post("/api/reservations/{id}/late")
    @router.post("/api/reservations/{id}/mark-late")
    def api_mark_reservation_late(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid reservation ID."}, status_code=400)

        body = req.json() or {}
        reason = body.get("reason")
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            res_obj = mark_reservation_late(
                reservation_id=res_id,
                actor_user=req.user,
                reason=reason,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "reservation": res_obj})
        except ReservationNotFoundError as e:
            return Response.json({"error": str(e)}, status_code=404)
        except ReservationPermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Unexpected error marking reservation #%d as late: %s", res_id, e)
            return Response.json({"error": f"Failed to mark reservation as late: {e}"}, status_code=500)

    @router.post("/api/reservations/{id}/no-show")
    @router.post("/api/reservations/{id}/mark-no-show")
    def api_mark_reservation_no_show(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid reservation ID."}, status_code=400)

        body = req.json() or {}
        reason = body.get("reason")
        promote_wl = bool(body.get("promote_waiting_list", True))
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            res_obj = mark_reservation_no_show(
                reservation_id=res_id,
                actor_user=req.user,
                reason=reason,
                promote_waiting_list=promote_wl,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "reservation": res_obj})
        except ReservationNotFoundError as e:
            return Response.json({"error": str(e)}, status_code=404)
        except ReservationPermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Unexpected error marking reservation #%d as no-show: %s", res_id, e)
            return Response.json({"error": f"Failed to mark reservation as no-show: {e}"}, status_code=500)

    @router.post("/api/reservations/lifecycle/scan")
    def api_scan_reservation_lifecycle(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        if not is_operator_or_admin(req.user):
            return Response.json({"error": "Only administrators and operators may trigger lifecycle scans."}, status_code=403)

        body = req.json() or {}
        curr_time = body.get("current_time")
        late_grace = int(body.get("late_grace_minutes", 15))
        noshow_grace = int(body.get("no_show_grace_minutes", 30))
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            result = process_overdue_reservations(
                current_time=curr_time,
                late_grace_minutes=late_grace,
                no_show_grace_minutes=noshow_grace,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "result": result})
        except Exception as e:
            logger.error("Unexpected error in reservation lifecycle scan: %s", e)
            return Response.json({"error": f"Failed to run lifecycle scan: {e}"}, status_code=500)

    # -------------------------------------------------------------------------
    # HTML View: Reservations List & Calendar View
    # -------------------------------------------------------------------------
    @router.get("/reservations")
    def html_reservations_list(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/reservations")

        user_role = get_user_role(req.user)
        is_staff = is_operator_or_admin(req.user)
        can_create = user_role != ROLE_VIEWER

        machine_filter = req.query("machine_id")
        status_filter = req.query("status") or "active"
        search_query = req.query("q", "").strip()
        selected_date = req.query("date") or today_rome_str()

        m_id = int(machine_filter) if machine_filter and machine_filter.isdigit() else None

        user_member = None
        if user_role == ROLE_MEMBER:
            user_member = get_member_by_user_id(req.user["id"], include_qualifications=False)

        reservations = list_reservations(
            machine_id=m_id,
            status=status_filter if status_filter != "all" else None,
            search=search_query if search_query else None,
            limit=200,
        )

        machines = query_all("SELECT id, code, name, capacity, state FROM machines WHERE state != 'retired' ORDER BY name ASC;")
        all_res = list_reservations(limit=500)
        total_count = len(all_res)
        active_count = sum(1 for r in all_res if r.get("is_active"))
        cancelled_count = sum(1 for r in all_res if r.get("status") == "cancelled")

        csrf_val = getattr(req, "csrf_token", "")

        flash_success = req.query("success")
        flash_error = req.query("error")

        machine_options = ['<option value="">All Machines</option>']
        for m in machines:
            sel = " selected" if str(m["id"]) == machine_filter else ""
            machine_options.append(f'<option value="{m["id"]}"{sel}>{escape_html(m["name"])} ({escape_html(m["code"])})</option>')

        status_options = [
            ('<option value="active"' + (' selected' if status_filter == 'active' else '') + '>Active & Confirmed</option>'),
            ('<option value="all"' + (' selected' if status_filter == 'all' else '') + '>All Bookings</option>'),
            ('<option value="confirmed"' + (' selected' if status_filter == 'confirmed' else '') + '>Confirmed Only</option>'),
            ('<option value="checked_in"' + (' selected' if status_filter == 'checked_in' else '') + '>Checked In</option>'),
            ('<option value="checked_out"' + (' selected' if status_filter == 'checked_out' else '') + '>Checked Out</option>'),
            ('<option value="late"' + (' selected' if status_filter == 'late' else '') + '>Late Arrival</option>'),
            ('<option value="no_show"' + (' selected' if status_filter == 'no_show' else '') + '>No-Show</option>'),
            ('<option value="cancelled"' + (' selected' if status_filter == 'cancelled' else '') + '>Cancelled Only</option>'),
        ]

        table_rows = []
        for r in reservations:
            st = r.get("status", "confirmed")
            if st == "confirmed":
                badge_class = "badge-success"
            elif st == "checked_in":
                badge_class = "badge-success"
            elif st == "checked_out":
                badge_class = "badge-info"
            elif st == "late":
                badge_class = "badge-warning"
            elif st == "no_show":
                badge_class = "badge-secondary"
            elif st == "cancelled":
                badge_class = "badge-danger"
            else:
                badge_class = "badge-info"
            st_label = st.replace("_", " ").title()

            is_own = user_member and user_member["id"] == r["member_id"]
            can_edit_row = is_staff or is_own

            actions_html = [
                f'<a href="/reservations/{r["id"]}" class="btn btn-sm btn-outline-primary" style="padding: 0.2rem 0.5rem; font-size: 0.8rem;">Details</a>'
            ]
            if can_edit_row and st in ("pending", "confirmed"):
                actions_html.append(
                    f'<a href="/reservations/{r["id"]}/edit" class="btn btn-sm btn-outline-secondary" style="padding: 0.2rem 0.5rem; font-size: 0.8rem;">Edit</a>'
                )

            table_rows.append(f"""
            <tr>
                <td><strong>#{r['id']}</strong></td>
                <td>
                    <strong><a href="/reservations/{r['id']}" style="color: inherit; text-decoration: none;">{escape_html(r.get('title') or 'Reservation')}</a></strong><br>
                    <small class="text-muted">{escape_html(r.get('category_name') or '')}</small>
                </td>
                <td>
                    <span style="font-weight: 500;">{escape_html(r['machine_name'])}</span><br>
                    <small class="text-muted"><code>{escape_html(r['machine_code'])}</code></small>
                </td>
                <td>
                    <span>{escape_html(r['member_name'])}</span><br>
                    <small class="text-muted"><code>{escape_html(r['member_number'])}</code></small>
                </td>
                <td>
                    <span>{escape_html(r['formatted_start'])}</span><br>
                    <small class="text-muted">to {escape_html(r['formatted_end'])} ({r['duration_minutes']} min)</small>
                </td>
                <td><span class="badge {badge_class}">{escape_html(st_label)}</span></td>
                <td><div style="display: flex; gap: 0.3rem;">{''.join(actions_html)}</div></td>
            </tr>
            """)

        rows_content = "\n".join(table_rows) if table_rows else "<tr><td colspan='7' class='text-center text-muted' style='padding: 2rem;'>No reservations match the specified filters.</td></tr>"

        content = f"""
        <div class="page-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
            <div>
                <h1>Reservations &amp; Workshop Schedule</h1>
                <p class="text-muted">Manage single and recurring makerspace bookings with real-time conflict prevention across Europe/Rome timezone.</p>
            </div>
            <div style="display: flex; gap: 0.5rem;">
                <a href="/availability" class="btn btn-outline-primary">Availability Explorer</a>
                {f'<a href="/reservations/new" class="btn btn-outline-primary">+ Single Booking</a><a href="/reservations/recurring" class="btn btn-primary">+ Recurring Series</a>' if can_create else ''}
            </div>
        </div>

        {f'<div class="alert alert-success">{escape_html(flash_success)}</div>' if flash_success else ''}
        {f'<div class="alert alert-danger">{escape_html(flash_error)}</div>' if flash_error else ''}

        <div class="metrics-row" style="margin-bottom: 1.5rem;">
            <div class="metric-card">
                <span class="metric-label">Total Reservations</span>
                <span class="metric-value">{total_count}</span>
                <span class="metric-subtext">Historical System Records</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Active / Confirmed</span>
                <span class="metric-value" style="color: #16a34a;">{active_count}</span>
                <span class="metric-subtext">Upcoming Workshop Slots</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Cancelled</span>
                <span class="metric-value" style="color: #dc2626;">{cancelled_count}</span>
                <span class="metric-subtext">Released Capacity</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Matching Filters</span>
                <span class="metric-value" style="color: #2563eb;">{len(reservations)}</span>
                <span class="metric-subtext">Currently Displayed</span>
            </div>
        </div>

        <div class="card" style="margin-bottom: 1.5rem;">
            <div class="card-header">
                <h3 class="card-title">Filter Reservations</h3>
            </div>
            <div class="card-body">
                <form method="GET" action="/reservations" style="display: flex; gap: 1rem; align-items: flex-end; flex-wrap: wrap;">
                    <div class="form-group" style="flex: 1; min-width: 200px; margin-bottom: 0;">
                        <label for="f_machine">Machine:</label>
                        <select name="machine_id" id="f_machine" class="form-control" onchange="this.form.submit()">
                            {''.join(machine_options)}
                        </select>
                    </div>
                    <div class="form-group" style="width: 180px; margin-bottom: 0;">
                        <label for="f_status">Status:</label>
                        <select name="status" id="f_status" class="form-control" onchange="this.form.submit()">
                            {''.join(status_options)}
                        </select>
                    </div>
                    <div class="form-group" style="flex: 1; min-width: 180px; margin-bottom: 0;">
                        <label for="f_q">Search:</label>
                        <input type="text" name="q" id="f_q" class="form-control" placeholder="Search title, member, machine..." value="{escape_html(search_query)}">
                    </div>
                    <div style="display: flex; gap: 0.5rem;">
                        <button type="submit" class="btn btn-secondary">Filter</button>
                        <a href="/reservations" class="btn btn-outline-secondary">Reset</a>
                    </div>
                </form>
            </div>
        </div>

        <div class="card">
            <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
                <h3 class="card-title">Reservation Bookings</h3>
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table table-hover">
                        <thead>
                            <tr>
                                <th style="width: 70px;">ID</th>
                                <th>Title &amp; Category</th>
                                <th>Machine</th>
                                <th>Member</th>
                                <th>Schedule Window</th>
                                <th style="width: 120px;">Status</th>
                                <th style="width: 140px;">Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {rows_content}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        """

        html_out = render_page("Reservations", content, user=req.user, active_nav="reservations", csrf_token=csrf_val)
        return Response.html(html_out)

    # -------------------------------------------------------------------------
    # HTML View: Create Reservation Form (GET & POST)
    # -------------------------------------------------------------------------
    def _render_new_reservation_form(
        req: Request,
        form_data: Dict[str, Any],
        error_msg: str = "",
        conflict_details: Optional[List[Dict[str, Any]]] = None,
    ) -> Response:
        user = req.user
        user_role = get_user_role(user)
        is_staff = is_operator_or_admin(user)

        machines = query_all("SELECT id, code, name, capacity, state, operating_hours_start, operating_hours_end, hourly_rate_cents FROM machines WHERE state != 'retired' ORDER BY name ASC;")
        members = list_members(status="active") if is_staff else []

        user_member = get_member_by_user_id(user["id"], include_qualifications=True) if user_role == ROLE_MEMBER else None

        sel_machine_id = str(form_data.get("machine_id") or req.query("machine_id") or (machines[0]["id"] if machines else ""))
        sel_member_id = str(form_data.get("member_id") or (user_member["id"] if user_member else (members[0]["id"] if members else "")))

        default_date = form_data.get("date") or req.query("date") or today_rome_str()
        default_start_time = form_data.get("start_time") or req.query("start_time") or "10:00"
        default_end_time = form_data.get("end_time") or req.query("end_time") or "12:00"
        title_val = form_data.get("title") or req.query("title") or ""

        if "T" in default_start_time:
            parts = default_start_time.split("T")
            default_date = parts[0]
            default_start_time = parts[1][:5]
        if "T" in default_end_time:
            parts = default_end_time.split("T")
            default_end_time = parts[1][:5]

        machine_options = []
        for m in machines:
            sel = " selected" if str(m["id"]) == sel_machine_id else ""
            st_text = f" - Cap: {m['capacity']}"
            if m["state"] != "available":
                st_text += f" ({m['state'].replace('_', ' ')})"
            rate_text = f" - €{m['hourly_rate_cents']/100.0:.2f}/hr"
            machine_options.append(f'<option value="{m["id"]}"{sel}>{escape_html(m["name"])} ({escape_html(m["code"])}){st_text}{rate_text}</option>')

        member_select_html = ""
        if is_staff:
            member_options = []
            for mem in members:
                sel = " selected" if str(mem["id"]) == sel_member_id else ""
                member_options.append(f'<option value="{mem["id"]}"{sel}>{escape_html(mem["full_name"])} ({escape_html(mem["member_number"])})</option>')
            member_select_html = f"""
            <div class="form-group">
                <label for="member_id"><strong>Member Profile:</strong></label>
                <select name="member_id" id="member_id" class="form-control" required>
                    {''.join(member_options)}
                </select>
                <small class="text-muted">Select the makerspace member for whom this reservation is booked.</small>
            </div>
            """
        else:
            member_select_html = f"""
            <div class="form-group">
                <label><strong>Reserving Member:</strong></label>
                <input type="hidden" name="member_id" value="{user_member['id'] if user_member else ''}">
                <input type="text" class="form-control" value="{escape_html(user_member['full_name'] if user_member else user.get('full_name'))} ({escape_html(user_member['member_number'] if user_member else 'Unlinked')})" disabled>
                <small class="text-muted">Your active member profile is automatically assigned to this reservation.</small>
            </div>
            """

        csrf_val = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_val)

        conflict_alert = ""
        if error_msg:
            conflict_alert = f"""
            <div class="alert alert-danger" style="margin-bottom: 1.5rem;">
                <h4 style="margin-top: 0; margin-bottom: 0.5rem;">Booking Rejected: Conflict Detected</h4>
                <p style="margin-bottom: 0;">{escape_html(error_msg)}</p>
            </div>
            """

        content = f"""
        <div class="page-header" style="margin-bottom: 1.5rem;">
            <h1>Create Machine Reservation</h1>
            <p class="text-muted">Schedule workshop machine usage with automated conflict resolution, qualification verification, and operating hours checks.</p>
        </div>

        <div style="display: flex; gap: 0.5rem; margin-bottom: 1.5rem;">
            <a href="/reservations/new" class="btn btn-primary">Single Booking</a>
            <a href="/reservations/recurring" class="btn btn-outline-primary">Recurring Series Booking</a>
        </div>

        {conflict_alert}

        <div class="card" style="max-width: 800px;">
            <div class="card-header">
                <h3 class="card-title">Reservation Details</h3>
            </div>
            <div class="card-body">
                <form method="POST" action="/reservations/new">
                    {csrf_field}

                    <div class="form-group">
                        <label for="machine_id"><strong>Select Machine:</strong></label>
                        <select name="machine_id" id="machine_id" class="form-control" required>
                            {''.join(machine_options)}
                        </select>
                    </div>

                    {member_select_html}

                    <div class="form-group">
                        <label for="title"><strong>Reservation Title / Project Name:</strong></label>
                        <input type="text" name="title" id="title" class="form-control" placeholder="e.g. Laser engraving acrylic enclosure prototypes" value="{escape_html(title_val)}" required>
                    </div>

                    <div style="display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 1rem; margin-bottom: 1rem;">
                        <div class="form-group" style="margin-bottom: 0;">
                            <label for="res_date"><strong>Date:</strong></label>
                            <input type="date" name="date" id="res_date" class="form-control" value="{escape_html(default_date)}" required>
                        </div>
                        <div class="form-group" style="margin-bottom: 0;">
                            <label for="start_time"><strong>Start Time:</strong></label>
                            <input type="time" name="start_time" id="start_time" class="form-control" value="{escape_html(default_start_time)}" required>
                        </div>
                        <div class="form-group" style="margin-bottom: 0;">
                            <label for="end_time"><strong>End Time:</strong></label>
                            <input type="time" name="end_time" id="end_time" class="form-control" value="{escape_html(default_end_time)}" required>
                        </div>
                    </div>

                    <div class="alert alert-info" style="font-size: 0.85rem; margin-top: 1rem; margin-bottom: 1.5rem;">
                        <strong>Workshop Rules:</strong> Operating hours are strictly 08:00 to 22:00 (Europe/Rome). Reservations cannot cross midnight. Minimum booking duration is 15 minutes. Required qualifications must remain valid throughout the booking.
                    </div>

                    <div style="display: flex; gap: 1rem; align-items: center;">
                        <button type="submit" class="btn btn-primary">Confirm & Create Reservation</button>
                        <a href="/reservations" class="btn btn-secondary">Cancel</a>
                    </div>
                </form>
            </div>
        </div>
        """

        html_out = render_page("New Reservation", content, user=user, active_nav="reservations", csrf_token=csrf_val)
        return Response.html(html_out)

    @router.get("/reservations/new")
    def html_new_reservation_get(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/reservations/new")

        user_role = get_user_role(req.user)
        if user_role == ROLE_VIEWER:
            return Response.html(
                render_page("Access Denied", "<div class='alert alert-danger'>Viewers have read-only access and cannot create reservations.</div>", user=req.user, active_nav="reservations"),
                status_code=403,
            )

        return _render_new_reservation_form(req, {})

    @router.post("/reservations/new")
    def html_new_reservation_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/reservations/new")

        user_role = get_user_role(req.user)
        if user_role == ROLE_VIEWER:
            return Response.html(
                render_page("Access Denied", "<div class='alert alert-danger'>Viewers have read-only access and cannot create reservations.</div>", user=req.user, active_nav="reservations"),
                status_code=403,
            )

        form_data = req.form()
        machine_id_raw = form_data.get("machine_id")
        member_id_raw = form_data.get("member_id")
        date_str = form_data.get("date")
        start_time_str = form_data.get("start_time")
        end_time_str = form_data.get("end_time")
        title = form_data.get("title", "").strip()

        if not machine_id_raw or not date_str or not start_time_str or not end_time_str:
            return _render_new_reservation_form(req, form_data, error_msg="Please fill out all mandatory fields (machine, date, start time, end time).")

        try:
            machine_id = int(machine_id_raw)
        except (ValueError, TypeError):
            return _render_new_reservation_form(req, form_data, error_msg="Invalid machine selected.")

        member_id: int
        if user_role == ROLE_MEMBER:
            user_member = get_member_by_user_id(req.user["id"], include_qualifications=False)
            if not user_member:
                return _render_new_reservation_form(req, form_data, error_msg="Your user account is not linked to an active member profile.")
            member_id = user_member["id"]
        else:
            if not member_id_raw:
                return _render_new_reservation_form(req, form_data, error_msg="Please select a member profile.")
            try:
                member_id = int(member_id_raw)
            except (ValueError, TypeError):
                return _render_new_reservation_form(req, form_data, error_msg="Invalid member ID.")

        full_start_iso = f"{date_str.strip()}T{start_time_str.strip()}:00"
        full_end_iso = f"{date_str.strip()}T{end_time_str.strip()}:00"
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            res_obj = create_reservation(
                machine_id=machine_id,
                member_id=member_id,
                start_time=full_start_iso,
                end_time=full_end_iso,
                title=title,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.redirect(f"/reservations/{res_obj['id']}?success=Reservation+created+successfully")
        except ReservationConflictError as e:
            return _render_new_reservation_form(req, form_data, error_msg=str(e), conflict_details=e.conflicts)
        except (ValueError, ReservationPermissionError) as e:
            return _render_new_reservation_form(req, form_data, error_msg=str(e))
        except Exception as e:
            logger.error("Unexpected error in reservation submission: %s", e)
            return _render_new_reservation_form(req, form_data, error_msg=f"Unexpected error: {e}")

    # -------------------------------------------------------------------------
    # HTML View: Recurring Reservation Form (GET & POST)
    # -------------------------------------------------------------------------
    def _render_recurring_reservation_form(
        req: Request,
        form_data: Dict[str, Any],
        error_msg: str = "",
        conflict_details: Optional[List[Dict[str, Any]]] = None,
    ) -> Response:
        user = req.user
        user_role = get_user_role(user)
        is_staff = is_operator_or_admin(user)

        machines = query_all("SELECT id, code, name, capacity, state, operating_hours_start, operating_hours_end, hourly_rate_cents FROM machines WHERE state != 'retired' ORDER BY name ASC;")
        members = list_members(status="active") if is_staff else []
        user_member = get_member_by_user_id(user["id"], include_qualifications=True) if user_role == ROLE_MEMBER else None

        sel_machine_id = str(form_data.get("machine_id") or req.query("machine_id") or (machines[0]["id"] if machines else ""))
        sel_member_id = str(form_data.get("member_id") or (user_member["id"] if user_member else (members[0]["id"] if members else "")))

        default_date = form_data.get("date") or req.query("date") or today_rome_str()
        default_start_time = form_data.get("start_time") or req.query("start_time") or "10:00"
        default_end_time = form_data.get("end_time") or req.query("end_time") or "12:00"
        title_val = form_data.get("title") or req.query("title") or ""

        freq_val = form_data.get("frequency") or "weekly"
        selected_weekdays = req.form_list("weekdays") or (form_data.get("weekdays") if isinstance(form_data.get("weekdays"), list) else (form_data.get("weekdays", "").split(",") if form_data.get("weekdays") else ["MO"]))
        if not selected_weekdays:
            selected_weekdays = ["MO"]

        end_type_val = form_data.get("end_type") or "count"
        count_val = form_data.get("count") or "4"
        until_val = form_data.get("until") or ""
        conflict_policy = form_data.get("conflict_policy") or "strict"

        machine_options = []
        for m in machines:
            sel = " selected" if str(m["id"]) == sel_machine_id else ""
            st_text = f" - Cap: {m['capacity']}"
            if m["state"] != "available":
                st_text += f" ({m['state'].replace('_', ' ')})"
            rate_text = f" - €{m['hourly_rate_cents']/100.0:.2f}/hr"
            machine_options.append(f'<option value="{m["id"]}"{sel}>{escape_html(m["name"])} ({escape_html(m["code"])}){st_text}{rate_text}</option>')

        member_select_html = ""
        if is_staff:
            member_options = []
            for mem in members:
                sel = " selected" if str(mem["id"]) == sel_member_id else ""
                member_options.append(f'<option value="{mem["id"]}"{sel}>{escape_html(mem["full_name"])} ({escape_html(mem["member_number"])})</option>')
            member_select_html = f"""
            <div class="form-group">
                <label for="rec_member_id"><strong>Member Profile:</strong></label>
                <select name="member_id" id="rec_member_id" class="form-control" required>
                    {''.join(member_options)}
                </select>
                <small class="text-muted">Select member for whom this recurring schedule is booked.</small>
            </div>
            """
        else:
            member_select_html = f"""
            <div class="form-group">
                <label><strong>Reserving Member:</strong></label>
                <input type="hidden" name="member_id" value="{user_member['id'] if user_member else ''}">
                <input type="text" class="form-control" value="{escape_html(user_member['full_name'] if user_member else user.get('full_name'))} ({escape_html(user_member['member_number'] if user_member else 'Unlinked')})" disabled>
                <small class="text-muted">Your active member profile is automatically assigned.</small>
            </div>
            """

        csrf_val = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_val)

        conflict_alert = ""
        if error_msg:
            conflict_alert = f"""
            <div class="alert alert-danger" style="margin-bottom: 1.5rem;">
                <h4 style="margin-top: 0; margin-bottom: 0.5rem;">Recurring Booking Error</h4>
                <p style="margin-bottom: 0;">{escape_html(error_msg)}</p>
            </div>
            """

        days_checkboxes = []
        for code, name in [("MO", "Monday"), ("TU", "Tuesday"), ("WE", "Wednesday"), ("TH", "Thursday"), ("FR", "Friday"), ("SA", "Saturday"), ("SU", "Sunday")]:
            checked = " checked" if code in selected_weekdays else ""
            days_checkboxes.append(f"""
            <label style="display: inline-flex; align-items: center; gap: 0.3rem; margin-right: 1rem; font-weight: normal; cursor: pointer;">
                <input type="checkbox" name="weekdays" value="{code}"{checked}> {name[:3]}
            </label>
            """)

        content = f"""
        <div class="page-header" style="margin-bottom: 1.5rem;">
            <h1>Create Recurring Reservation Series</h1>
            <p class="text-muted">Schedule repeating makerspace sessions with Europe/Rome Daylight Saving Time preservation and conflict checking.</p>
        </div>

        <div style="display: flex; gap: 0.5rem; margin-bottom: 1.5rem;">
            <a href="/reservations/new" class="btn btn-outline-primary">Single Booking</a>
            <a href="/reservations/recurring" class="btn btn-primary">Recurring Series Booking</a>
        </div>

        {conflict_alert}

        <div class="card" style="max-width: 800px;">
            <div class="card-header">
                <h3 class="card-title">Recurring Series Configuration</h3>
            </div>
            <div class="card-body">
                <form method="POST" action="/reservations/recurring">
                    {csrf_field}

                    <div class="form-group">
                        <label for="rec_machine_id"><strong>Select Machine:</strong></label>
                        <select name="machine_id" id="rec_machine_id" class="form-control" required>
                            {''.join(machine_options)}
                        </select>
                    </div>

                    {member_select_html}

                    <div class="form-group">
                        <label for="rec_title"><strong>Series Title / Project Name:</strong></label>
                        <input type="text" name="title" id="rec_title" class="form-control" placeholder="e.g. Weekly Fabrication &amp; Enclosure Assembly" value="{escape_html(title_val)}" required>
                    </div>

                    <div style="display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 1rem; margin-bottom: 1rem;">
                        <div class="form-group" style="margin-bottom: 0;">
                            <label for="rec_start_date"><strong>First Occurrence Date:</strong></label>
                            <input type="date" name="date" id="rec_start_date" class="form-control" value="{escape_html(default_date)}" required>
                        </div>
                        <div class="form-group" style="margin-bottom: 0;">
                            <label for="rec_start_time"><strong>Start Time:</strong></label>
                            <input type="time" name="start_time" id="rec_start_time" class="form-control" value="{escape_html(default_start_time)}" required>
                        </div>
                        <div class="form-group" style="margin-bottom: 0;">
                            <label for="rec_end_time"><strong>End Time:</strong></label>
                            <input type="time" name="end_time" id="rec_end_time" class="form-control" value="{escape_html(default_end_time)}" required>
                        </div>
                    </div>

                    <div class="form-group" style="margin-top: 1rem;">
                        <label for="rec_freq"><strong>Recurrence Frequency:</strong></label>
                        <select name="frequency" id="rec_freq" class="form-control">
                            <option value="weekly"{' selected' if freq_val == 'weekly' else ''}>Weekly</option>
                            <option value="biweekly"{' selected' if freq_val == 'biweekly' else ''}>Bi-weekly (Every 2 Weeks)</option>
                            <option value="daily"{' selected' if freq_val == 'daily' else ''}>Daily</option>
                            <option value="monthly"{' selected' if freq_val == 'monthly' else ''}>Monthly</option>
                        </select>
                    </div>

                    <div class="form-group">
                        <label><strong>Repeat on Weekdays:</strong></label><br>
                        <div style="display: flex; flex-wrap: wrap; margin-top: 0.3rem;">
                            {''.join(days_checkboxes)}
                        </div>
                        <small class="text-muted">Select the days of the week on which bookings will occur.</small>
                    </div>

                    <div class="form-group" style="margin-top: 1rem;">
                        <label><strong>Series End Condition:</strong></label>
                        <div style="display: flex; gap: 1.5rem; margin-top: 0.3rem;">
                            <label style="font-weight: normal; cursor: pointer;">
                                <input type="radio" name="end_type" value="count"{' checked' if end_type_val == 'count' else ''}> Fixed Number of Occurrences:
                                <input type="number" name="count" class="form-control" style="display: inline-block; width: 80px; margin-left: 0.5rem;" min="1" max="52" value="{escape_html(str(count_val))}">
                            </label>
                            <label style="font-weight: normal; cursor: pointer;">
                                <input type="radio" name="end_type" value="until"{' checked' if end_type_val == 'until' else ''}> Repeat Until Date:
                                <input type="date" name="until" class="form-control" style="display: inline-block; width: 160px; margin-left: 0.5rem;" value="{escape_html(until_val)}">
                            </label>
                        </div>
                    </div>

                    <div class="form-group" style="margin-top: 1rem;">
                        <label><strong>Conflict Policy:</strong></label>
                        <div style="display: flex; gap: 1.5rem; margin-top: 0.3rem;">
                            <label style="font-weight: normal; cursor: pointer;">
                                <input type="radio" name="conflict_policy" value="strict"{' checked' if conflict_policy == 'strict' else ''}> <strong>Strict:</strong> Reject entire series if any occurrence has a conflict
                            </label>
                            <label style="font-weight: normal; cursor: pointer;">
                                <input type="radio" name="conflict_policy" value="skip"{' checked' if conflict_policy == 'skip' else ''}> <strong>Flexible:</strong> Book available dates and skip conflicting dates
                            </label>
                        </div>
                    </div>

                    <div class="alert alert-info" style="font-size: 0.85rem; margin-top: 1rem; margin-bottom: 1.5rem;">
                        <strong>Europe/Rome Timezone &amp; Daylight Saving Time (DST):</strong> All occurrences strictly preserve local wall-clock hours (e.g. 10:00 to 12:00 Rome time) across seasonal clock changes between Central European Time (CET) and Central European Summer Time (CEST).
                    </div>

                    <div style="display: flex; gap: 1rem; align-items: center;">
                        <button type="submit" class="btn btn-primary">Create Recurring Reservation Series</button>
                        <a href="/reservations" class="btn btn-secondary">Cancel</a>
                    </div>
                </form>
            </div>
        </div>
        """

        html_out = render_page("New Recurring Reservation", content, user=user, active_nav="reservations", csrf_token=csrf_val)
        return Response.html(html_out)

    @router.get("/reservations/recurring")
    def html_recurring_reservation_get(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/reservations/recurring")

        user_role = get_user_role(req.user)
        if user_role == ROLE_VIEWER:
            return Response.html(
                render_page("Access Denied", "<div class='alert alert-danger'>Viewers have read-only access and cannot create recurring reservations.</div>", user=req.user, active_nav="reservations"),
                status_code=403,
            )

        return _render_recurring_reservation_form(req, {})

    @router.post("/reservations/recurring")
    def html_recurring_reservation_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/reservations/recurring")

        user_role = get_user_role(req.user)
        if user_role == ROLE_VIEWER:
            return Response.html(
                render_page("Access Denied", "<div class='alert alert-danger'>Viewers have read-only access and cannot create recurring reservations.</div>", user=req.user, active_nav="reservations"),
                status_code=403,
            )

        form_data = req.form()
        machine_id_raw = form_data.get("machine_id")
        member_id_raw = form_data.get("member_id")
        date_str = form_data.get("date")
        start_time_str = form_data.get("start_time")
        end_time_str = form_data.get("end_time")
        title = form_data.get("title", "").strip()

        freq = form_data.get("frequency") or "weekly"
        weekdays = req.form_list("weekdays")
        end_type = form_data.get("end_type") or "count"
        count_raw = form_data.get("count")
        until_raw = form_data.get("until")
        conflict_policy = form_data.get("conflict_policy") or "strict"
        skip_conflicts = (conflict_policy == "skip")

        if not machine_id_raw or not date_str or not start_time_str or not end_time_str:
            return _render_recurring_reservation_form(req, form_data, error_msg="Please fill out all mandatory fields.")

        try:
            machine_id = int(machine_id_raw)
        except (ValueError, TypeError):
            return _render_recurring_reservation_form(req, form_data, error_msg="Invalid machine selected.")

        member_id: int
        if user_role == ROLE_MEMBER:
            user_member = get_member_by_user_id(req.user["id"], include_qualifications=False)
            if not user_member:
                return _render_recurring_reservation_form(req, form_data, error_msg="Your user account is not linked to an active member profile.")
            member_id = user_member["id"]
        else:
            if not member_id_raw:
                return _render_recurring_reservation_form(req, form_data, error_msg="Please select a member profile.")
            try:
                member_id = int(member_id_raw)
            except (ValueError, TypeError):
                return _render_recurring_reservation_form(req, form_data, error_msg="Invalid member ID.")

        rule_dict: Dict[str, Any] = {
            "frequency": freq,
            "weekdays": weekdays if weekdays else ["MO"],
        }
        if end_type == "until" and until_raw and until_raw.strip():
            rule_dict["until"] = until_raw.strip()
        else:
            try:
                rule_dict["count"] = int(count_raw) if count_raw and count_raw.isdigit() else 4
            except ValueError:
                rule_dict["count"] = 4

        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            series_result = create_recurring_reservations(
                machine_id=machine_id,
                member_id=member_id,
                start_date=date_str,
                start_time_of_day=start_time_str,
                end_time_of_day=end_time_str,
                recurrence_rule=rule_dict,
                title=title,
                skip_conflicts=skip_conflicts,
                actor_user=req.user,
                ip_address=client_ip,
            )
            created_count = series_result.get("created_count", 0)
            first_res = series_result["created_reservations"][0] if series_result.get("created_reservations") else None
            redirect_target = f"/reservations/{first_res['id']}" if first_res else "/reservations"
            return Response.redirect(f"{redirect_target}?success=Recurring+series+created+successfully+({created_count}+occurrences)")
        except ReservationConflictError as e:
            return _render_recurring_reservation_form(req, form_data, error_msg=str(e), conflict_details=e.conflicts)
        except (RecurrenceRuleError, ValueError, ReservationPermissionError) as e:
            return _render_recurring_reservation_form(req, form_data, error_msg=str(e))
        except Exception as e:
            logger.error("Unexpected error creating recurring series: %s", e)
            return _render_recurring_reservation_form(req, form_data, error_msg=f"Unexpected error: {e}")

    @router.post("/reservations/recurring/{group_id}/cancel")
    def html_cancel_recurring_series_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/reservations")

        group_id = req.route_params.get("group_id")
        if not group_id:
            return Response.redirect("/reservations?error=Missing+recurrence+group+ID")

        form_data = req.form()
        reason = form_data.get("reason") or "Recurring series cancelled by user"
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            result = cancel_recurring_series(
                recurrence_group_id=group_id,
                future_only=False,
                reason=reason,
                actor_user=req.user,
                ip_address=client_ip,
            )
            cnt = result.get("cancelled_count", 0)
            return Response.redirect(f"/reservations?success=Cancelled+entire+recurring+series+({cnt}+occurrences+released)")
        except Exception as e:
            logger.error("Error cancelling recurring series %s: %s", group_id, e)
            return Response.redirect(f"/reservations?error={escape_html(str(e))}")

    # -------------------------------------------------------------------------
    # HTML View: Single Reservation Detail Page
    # -------------------------------------------------------------------------
    @router.get("/reservations/{id}")
    def html_reservation_detail(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=" + req.path)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.html(
                render_page("Invalid ID", "<div class='alert alert-danger'>Invalid reservation ID.</div>", user=req.user, active_nav="reservations"),
                status_code=400,
            )

        res_obj = get_reservation_by_id(res_id, include_details=True)
        if not res_obj:
            return Response.html(
                render_page("Not Found", "<div class='alert alert-danger'>Reservation not found.</div>", user=req.user, active_nav="reservations"),
                status_code=404,
            )

        user = req.user
        user_role = get_user_role(user)
        is_staff = is_operator_or_admin(user)
        user_member = get_member_by_user_id(user["id"], include_qualifications=False) if user_role == ROLE_MEMBER else None
        is_own = user_member and user_member["id"] == res_obj["member_id"]

        st = res_obj["status"]

        can_check_in = (is_staff or is_own) and st in ("confirmed", "late")
        can_check_out = (is_staff or is_own) and st == "checked_in"
        can_mark_late = (is_staff or is_own) and st == "confirmed"
        can_mark_no_show = is_staff and st in ("confirmed", "late")
        can_edit = (is_staff or is_own) and st in ("pending", "confirmed")
        can_cancel = (is_staff or is_own) and st in ("pending", "confirmed", "late")

        csrf_val = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_val)

        flash_success = req.query("success")
        flash_error = req.query("error")

        if st == "confirmed":
            badge_class = "badge-success"
        elif st == "checked_in":
            badge_class = "badge-success"
        elif st == "checked_out":
            badge_class = "badge-info"
        elif st == "late":
            badge_class = "badge-warning"
        elif st == "no_show":
            badge_class = "badge-secondary"
        elif st == "cancelled":
            badge_class = "badge-danger"
        else:
            badge_class = "badge-info"

        charge_info = res_obj.get("estimated_charge", {})
        est_cents = charge_info.get("estimated_total_cents", 0)

        q_snap = res_obj.get("qualification_snapshot")
        if q_snap:
            qual_html = f"""
            <span class="badge badge-success">Certified</span>
            <strong>{escape_html(q_snap['qualification_name'])}</strong>
            <small class="text-muted">(Issued: {escape_html(q_snap['issue_date'])}, Expires: {escape_html(q_snap.get('expiry_date') or 'Never')})</small>
            """
        else:
            qual_html = "<span class='badge badge-info'>General Machine</span> <small class='text-muted'>No specialized qualification required.</small>"

        cancellation_block = ""
        if res_obj["status"] == "cancelled":
            cancellation_block = f"""
            <div class="alert alert-danger" style="margin-top: 1.5rem;">
                <h4 style="margin-top: 0; margin-bottom: 0.5rem;">Reservation Cancelled</h4>
                <p style="margin-bottom: 0.3rem;"><strong>Reason:</strong> {escape_html(res_obj.get('cancellation_reason') or 'No reason specified')}</p>
                <small class="text-muted">Cancelled at: {escape_html(res_obj.get('cancelled_at') or '')}</small>
            </div>
            """
        elif res_obj["status"] == "no_show":
            cancellation_block = f"""
            <div class="alert alert-secondary" style="margin-top: 1.5rem;">
                <h4 style="margin-top: 0; margin-bottom: 0.5rem;">Marked as No-Show</h4>
                <p style="margin-bottom: 0.3rem;"><strong>Details:</strong> {escape_html(res_obj.get('cancellation_reason') or 'Member did not arrive for booking.')}</p>
            </div>
            """

        action_buttons = []
        if can_check_in:
            action_buttons.append(f"""
            <button type="button" class="btn btn-success" onclick="document.getElementById('checkin-modal').style.display='block';">&#10003; Check In</button>
            """)
        if can_check_out:
            action_buttons.append(f"""
            <button type="button" class="btn btn-primary" onclick="document.getElementById('checkout-modal').style.display='block';">&#8677; Check Out</button>
            """)
        if can_mark_late:
            action_buttons.append(f"""
            <button type="button" class="btn btn-outline-warning" onclick="document.getElementById('late-modal').style.display='block';">Report Late</button>
            """)
        if can_mark_no_show:
            action_buttons.append(f"""
            <button type="button" class="btn btn-outline-secondary" onclick="document.getElementById('noshow-modal').style.display='block';">Mark No-Show</button>
            """)
        if can_edit:
            action_buttons.append(f'<a href="/reservations/{res_id}/edit" class="btn btn-outline-primary">Edit Booking</a>')
        if can_cancel:
            action_buttons.append(f"""
            <button type="button" class="btn btn-outline-danger" onclick="document.getElementById('cancel-modal').style.display='block';">Cancel Reservation</button>
            """)

        recurrence_series_block = ""
        cancel_series_modal = ""
        if res_obj.get("recurrence_series"):
            r_series = res_obj["recurrence_series"]
            group_id = r_series["group_id"]
            occ_rows = []
            for occ in r_series["occurrences"]:
                is_curr = occ["id"] == res_obj["id"]
                o_st = occ["status"]
                o_badge = "badge-success" if o_st in ("confirmed", "checked_in") else ("badge-danger" if o_st == "cancelled" else "badge-info")
                highlight = "background: #f1f5f9; font-weight: 600;" if is_curr else ""
                link_or_text = f"<strong>#{occ['id']} (Current Booking)</strong>" if is_curr else f"<a href='/reservations/{occ['id']}'>#{occ['id']} {escape_html(occ.get('title') or '')}</a>"
                occ_rows.append(f"""
                <tr style="{highlight}">
                    <td>{link_or_text}</td>
                    <td>{escape_html(occ['formatted_start'])} - {escape_html(occ['formatted_end'])}</td>
                    <td><span class="badge {o_badge}">{escape_html(o_st.replace('_', ' ').title())}</span></td>
                </tr>
                """)

            cancel_series_btn = ""
            if can_cancel:
                cancel_series_btn = f"""
                <button type="button" class="btn btn-sm btn-outline-danger" onclick="document.getElementById('cancel-series-modal').style.display='block';">Cancel Entire Series</button>
                """

            recurrence_series_block = f"""
            <div class="card" style="margin-bottom: 1.5rem;">
                <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <h3 class="card-title">Recurring Series Schedule</h3>
                        <small class="text-muted">{escape_html(res_obj.get('recurrence_summary') or '')} &bull; Occurrence {r_series['current_index']} of {r_series['total_occurrences']}</small>
                    </div>
                    <div>
                        {cancel_series_btn}
                    </div>
                </div>
                <div class="card-body">
                    <p style="font-size: 0.85rem; color: #475569; margin-bottom: 0.8rem;">
                        <strong>Isolated Occurrence Policy:</strong> Editing or cancelling this single booking applies strictly to #{res_id}. All other occurrences in this series remain untouched.
                    </p>
                    <div class="table-responsive">
                        <table class="table table-sm">
                            <thead>
                                <tr>
                                    <th>Booking</th>
                                    <th>Time Window (Europe/Rome)</th>
                                    <th>Status</th>
                                </tr>
                            </thead>
                            <tbody>
                                {''.join(occ_rows)}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
            """

            if can_cancel:
                cancel_series_modal = f"""
                <!-- Cancel Entire Series Modal -->
                <div id="cancel-series-modal" style="display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5);">
                    <div style="background: white; width: 90%; max-width: 500px; margin: 10% auto; padding: 1.5rem; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                        <h3 style="margin-top: 0; color: #dc2626;">Cancel Entire Recurring Series</h3>
                        <p class="text-muted">Are you sure you want to cancel all active bookings in this recurring series (<strong>{r_series['total_occurrences']} occurrences</strong>)? All reserved slots will be released immediately.</p>
                        <form method="POST" action="/reservations/recurring/{group_id}/cancel">
                            {csrf_field}
                            <div class="form-group">
                                <label for="series_cancel_reason"><strong>Cancellation Reason:</strong></label>
                                <input type="text" name="reason" id="series_cancel_reason" class="form-control" placeholder="e.g. Project finished early, series schedule changed..." required>
                            </div>
                            <div style="display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1.5rem;">
                                <button type="button" class="btn btn-secondary" onclick="document.getElementById('cancel-series-modal').style.display='none';">Dismiss</button>
                                <button type="submit" class="btn btn-danger">Confirm Cancel Entire Series</button>
                            </div>
                        </form>
                    </div>
                </div>
                """

        actual_timing_rows = []
        if res_obj.get("actual_check_in"):
            actual_timing_rows.append(f"""
            <div>
                <span class="text-muted" style="font-size: 0.85rem;">Actual Check-In:</span>
                <div style="font-weight: 600; color: #16a34a;">{escape_html(format_display(res_obj['actual_check_in']))}</div>
            </div>
            """)
        if res_obj.get("actual_check_out"):
            actual_timing_rows.append(f"""
            <div>
                <span class="text-muted" style="font-size: 0.85rem;">Actual Check-Out:</span>
                <div style="font-weight: 600; color: #2563eb;">{escape_html(format_display(res_obj['actual_check_out']))}</div>
            </div>
            """)

        content = f"""
        <div class="page-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
            <div>
                <h1>Reservation #{res_id}: {escape_html(res_obj.get('title') or 'Machine Booking')}</h1>
                <p class="text-muted">Logged in ForgeDesk Makerspace Schedule &bull; Timezone: Europe/Rome</p>
            </div>
            <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                <a href="/reservations" class="btn btn-secondary">&larr; Back to Schedule</a>
                {''.join(action_buttons)}
            </div>
        </div>

        {f'<div class="alert alert-success">{escape_html(flash_success)}</div>' if flash_success else ''}
        {f'<div class="alert alert-danger">{escape_html(flash_error)}</div>' if flash_error else ''}

        <div style="display: grid; grid-template-columns: 2fr 1fr; gap: 1.5rem;">
            <div>
                {recurrence_series_block}

                <div class="card" style="margin-bottom: 1.5rem;">
                    <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
                        <h3 class="card-title">Schedule &amp; Timing</h3>
                        <span class="badge {badge_class}">{escape_html(st.replace('_', ' ').title())}</span>
                    </div>
                    <div class="card-body">
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.5rem;">
                            <div>
                                <span class="text-muted" style="font-size: 0.85rem;">Scheduled Start:</span>
                                <div style="font-size: 1.1rem; font-weight: 600;">{escape_html(res_obj['formatted_start'])}</div>
                            </div>
                            <div>
                                <span class="text-muted" style="font-size: 0.85rem;">Scheduled End:</span>
                                <div style="font-size: 1.1rem; font-weight: 600;">{escape_html(res_obj['formatted_end'])}</div>
                            </div>
                        </div>

                        {f'<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.5rem; padding: 0.8rem; background: #f8fafc; border-radius: 6px;">' + "".join(actual_timing_rows) + '</div>' if actual_timing_rows else ''}

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div>
                                <span class="text-muted" style="font-size: 0.85rem;">Duration:</span>
                                <div><strong>{res_obj['duration_minutes']} minutes</strong> ({charge_info.get('duration_hours', 0):.2f} hours)</div>
                            </div>
                            <div>
                                <span class="text-muted" style="font-size: 0.85rem;">Booking Created:</span>
                                <div><small>{escape_html(res_obj['formatted_created_at'])}</small></div>
                            </div>
                        </div>

                        {cancellation_block}
                    </div>
                </div>

                <div class="card">
                    <div class="card-header">
                        <h3 class="card-title">Machine &amp; Member Information</h3>
                    </div>
                    <div class="card-body">
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem;">
                            <div>
                                <h4 style="margin-top: 0; font-size: 1rem; color: #475569;">Machine Details</h4>
                                <p style="margin-bottom: 0.4rem;">
                                    <strong><a href="/machines/{res_obj['machine_id']}" style="color: inherit;">{escape_html(res_obj['machine_name'])}</a></strong><br>
                                    <code>{escape_html(res_obj['machine_code'])}</code> &bull; <small>{escape_html(res_obj.get('category_name') or '')}</small>
                                </p>
                                <p style="margin-bottom: 0.4rem;">
                                    <span class="text-muted">Capacity:</span> {res_obj['machine_capacity']} concurrent units<br>
                                    <span class="text-muted">Operating Hours:</span> {escape_html(res_obj['operating_hours_start'])} - {escape_html(res_obj['operating_hours_end'])}
                                </p>
                                <p style="margin-bottom: 0;">
                                    <span class="text-muted">Required Qualification:</span><br>
                                    {qual_html}
                                </p>
                            </div>
                            <div>
                                <h4 style="margin-top: 0; font-size: 1rem; color: #475569;">Member Details</h4>
                                <p style="margin-bottom: 0.4rem;">
                                    <strong><a href="/members/{res_obj['member_id']}" style="color: inherit;">{escape_html(res_obj['member_name'])}</a></strong><br>
                                    <code>{escape_html(res_obj['member_number'])}</code> &bull; <span class="badge badge-success">{escape_html(res_obj['membership_status'].title())}</span>
                                </p>
                                <p style="margin-bottom: 0.4rem;">
                                    <span class="text-muted">Email:</span> {escape_html(res_obj['member_email'])}<br>
                                    <span class="text-muted">Phone:</span> {escape_html(res_obj.get('member_phone') or 'None')}
                                </p>
                                <p style="margin-bottom: 0;">
                                    <span class="text-muted">Booked By:</span> {escape_html(res_obj.get('created_by_name') or res_obj.get('created_by_username') or 'System')}
                                </p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <div>
                {f'''
                <div class="card" style="margin-bottom: 1.5rem; border: 2px solid #16a34a;">
                    <div class="card-header" style="background: #f0fdf4; display: flex; justify-content: space-between; align-items: center;">
                        <h3 class="card-title" style="color: #166534;">Finalized Usage Charge</h3>
                        <span class="badge badge-success">Billed #{res_obj['usage_charge']['id']}</span>
                    </div>
                    <div class="card-body">
                        <div style="font-size: 1.8rem; font-weight: 700; color: #16a34a; margin-bottom: 0.5rem;">
                            €{res_obj['usage_charge']['final_charge_cents']/100.0:.2f}
                        </div>
                        <p class="text-muted" style="font-size: 0.85rem; margin-bottom: 0.8rem;">
                            Base: €{res_obj['usage_charge']['base_charge_cents']/100.0:.2f} &bull; Status: {escape_html(res_obj['usage_charge']['status'].capitalize())}
                        </p>
                        <a href="/charges/{res_obj['usage_charge']['id']}" class="btn btn-sm btn-outline-success" style="width: 100%; text-align: center;">View Billing Statement &rarr;</a>
                    </div>
                </div>
                ''' if res_obj.get('usage_charge') else f'''
                <div class="card" style="margin-bottom: 1.5rem;">
                    <div class="card-header">
                        <h3 class="card-title">Estimated Pricing</h3>
                    </div>
                    <div class="card-body">
                        <div style="font-size: 1.8rem; font-weight: 700; color: #2563eb; margin-bottom: 0.5rem;">
                            €{est_cents/100.0:.2f}
                        </div>
                        <p class="text-muted" style="font-size: 0.85rem; margin-bottom: 1rem;">
                            Calculated based on {res_obj['duration_minutes']} min usage and machine rate table.
                        </p>
                        <ul style="padding-left: 1.2rem; font-size: 0.85rem; margin: 0;">
                            <li>Standard rate: €{charge_info.get('hourly_rate_cents', 0)/100.0:.2f}/hr</li>
                            <li>Peak rate: €{charge_info.get('peak_hourly_rate_cents', 0)/100.0:.2f}/hr</li>
                            <li>Minimum charge: €{charge_info.get('minimum_charge_cents', 0)/100.0:.2f}</li>
                        </ul>
                    </div>
                </div>
                '''}

                <div class="card">
                    <div class="card-header">
                        <h3 class="card-title">Integrity &amp; Audit</h3>
                    </div>
                    <div class="card-body" style="font-size: 0.85rem;">
                        <p style="margin-bottom: 0.5rem;">
                            <strong>Transaction ID:</strong> <code>res-{res_id}</code><br>
                            <strong>Status:</strong> {escape_html(st.upper())}<br>
                            <strong>Immutable Ledger:</strong> Enforced
                        </p>
                        <a href="/audit?object_type=reservation&object_id={res_id}" class="btn btn-sm btn-outline-secondary" style="width: 100%; text-align: center;">View Audit History</a>
                    </div>
                </div>
            </div>
        </div>

        <!-- Check-In Modal -->
        <div id="checkin-modal" style="display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5);">
            <div style="background: white; width: 90%; max-width: 500px; margin: 10% auto; padding: 1.5rem; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <h3 style="margin-top: 0; color: #16a34a;">Check In for Reservation #{res_id}</h3>
                <p class="text-muted">Confirm member check-in on <strong>{escape_html(res_obj['machine_name'])}</strong>. Member qualification validity and active membership will be verified at check-in time.</p>
                <form method="POST" action="/reservations/{res_id}/check-in">
                    {csrf_field}
                    <div style="display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1.5rem;">
                        <button type="button" class="btn btn-secondary" onclick="document.getElementById('checkin-modal').style.display='none';">Dismiss</button>
                        <button type="submit" class="btn btn-success">Confirm Check-In</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Check-Out Modal -->
        <div id="checkout-modal" style="display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5);">
            <div style="background: white; width: 90%; max-width: 500px; margin: 10% auto; padding: 1.5rem; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <h3 style="margin-top: 0; color: #2563eb;">Check Out from Reservation #{res_id}</h3>
                <p class="text-muted">Finalize usage for <strong>{escape_html(res_obj['member_name'])}</strong> on <strong>{escape_html(res_obj['machine_name'])}</strong>. The machine slot will be released and usage charges logged.</p>
                <form method="POST" action="/reservations/{res_id}/check-out">
                    {csrf_field}
                    <div class="form-group">
                        <label for="checkout_notes"><strong>Usage Notes / Operation Summary:</strong></label>
                        <input type="text" name="notes" id="checkout_notes" class="form-control" placeholder="Optional notes regarding session or machine status">
                    </div>
                    <div style="display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1.5rem;">
                        <button type="button" class="btn btn-secondary" onclick="document.getElementById('checkout-modal').style.display='none';">Dismiss</button>
                        <button type="submit" class="btn btn-primary">Confirm Check-Out</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Late Arrival Modal -->
        <div id="late-modal" style="display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5);">
            <div style="background: white; width: 90%; max-width: 500px; margin: 10% auto; padding: 1.5rem; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <h3 style="margin-top: 0; color: #d97706;">Report Late Arrival for #{res_id}</h3>
                <p class="text-muted">Mark this reservation as late. The booking remains held for the member during the arrival grace period.</p>
                <form method="POST" action="/reservations/{res_id}/mark-late">
                    {csrf_field}
                    <div class="form-group">
                        <label for="late_reason"><strong>Late Notice / Reason:</strong></label>
                        <input type="text" name="reason" id="late_reason" class="form-control" placeholder="e.g. Delayed in transit, arriving in 15 min" required>
                    </div>
                    <div style="display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1.5rem;">
                        <button type="button" class="btn btn-secondary" onclick="document.getElementById('late-modal').style.display='none';">Dismiss</button>
                        <button type="submit" class="btn btn-warning">Confirm Mark Late</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- No-Show Modal -->
        <div id="noshow-modal" style="display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5);">
            <div style="background: white; width: 90%; max-width: 500px; margin: 10% auto; padding: 1.5rem; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <h3 style="margin-top: 0; color: #64748b;">Mark Reservation #{res_id} as No-Show</h3>
                <p class="text-muted">Mark member as absent. This releases the machine slot immediately and enables waiting list promotion.</p>
                <form method="POST" action="/reservations/{res_id}/mark-no-show">
                    {csrf_field}
                    <div class="form-group">
                        <label for="noshow_reason"><strong>No-Show Note:</strong></label>
                        <input type="text" name="reason" id="noshow_reason" class="form-control" placeholder="e.g. Member did not arrive within grace window">
                    </div>
                    <div style="display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1.5rem;">
                        <button type="button" class="btn btn-secondary" onclick="document.getElementById('noshow-modal').style.display='none';">Dismiss</button>
                        <button type="submit" class="btn btn-danger">Confirm No-Show</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Cancel Confirmation Modal -->
        <div id="cancel-modal" style="display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5);">
            <div style="background: white; width: 90%; max-width: 500px; margin: 10% auto; padding: 1.5rem; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <h3 style="margin-top: 0;">Cancel Reservation #{res_id}</h3>
                <p class="text-muted">Are you sure you want to cancel this single occurrence? The machine slot will be released immediately.</p>
                <form method="POST" action="/reservations/{res_id}/cancel">
                    {csrf_field}
                    <div class="form-group">
                        <label for="cancel_reason"><strong>Cancellation Reason:</strong></label>
                        <input type="text" name="reason" id="cancel_reason" class="form-control" placeholder="e.g. Schedule conflict, project postponed..." required>
                    </div>
                    <div style="display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1.5rem;">
                        <button type="button" class="btn btn-secondary" onclick="document.getElementById('cancel-modal').style.display='none';">Dismiss</button>
                        <button type="submit" class="btn btn-danger">Confirm Cancellation</button>
                    </div>
                </form>
            </div>
        </div>

        {cancel_series_modal}
        """

        html_out = render_page(f"Reservation #{res_id}", content, user=req.user, active_nav="reservations", csrf_token=csrf_val)
        return Response.html(html_out)

    # -------------------------------------------------------------------------
    # HTML View: Edit Reservation Form (GET & POST)
    # -------------------------------------------------------------------------
    def _render_edit_reservation_form(
        req: Request,
        res_id: int,
        form_data: Dict[str, Any],
        error_msg: str = "",
    ) -> Response:
        existing = get_reservation_by_id(res_id, include_details=True)
        if not existing:
            return Response.html(render_page("Not Found", "<div class='alert alert-danger'>Reservation not found.</div>", user=req.user, active_nav="reservations"), status_code=404)

        user = req.user
        user_role = get_user_role(user)
        is_staff = is_operator_or_admin(user)
        user_member = get_member_by_user_id(user["id"], include_qualifications=False) if user_role == ROLE_MEMBER else None
        is_own = user_member and user_member["id"] == existing["member_id"]

        if not (is_staff or is_own):
            return Response.html(render_page("Access Denied", "<div class='alert alert-danger'>You do not have permission to edit this reservation.</div>", user=req.user, active_nav="reservations"), status_code=403)

        if existing["status"] in ("cancelled", "checked_out", "no_show"):
            return Response.html(render_page("Cannot Edit", f"<div class='alert alert-danger'>Cannot edit a reservation with status '{existing['status']}'.</div>", user=req.user, active_nav="reservations"), status_code=400)

        machines = query_all("SELECT id, code, name, capacity, state FROM machines WHERE state != 'retired' ORDER BY name ASC;")
        members = list_members(status="active") if is_staff else []

        sel_machine_id = str(form_data.get("machine_id") or existing["machine_id"])
        sel_member_id = str(form_data.get("member_id") or existing["member_id"])
        title_val = form_data.get("title") if "title" in form_data else existing["title"]

        s_dt = parse_datetime(form_data.get("start_time") or existing["start_time"])
        e_dt = parse_datetime(form_data.get("end_time") or existing["end_time"])
        default_date = form_data.get("date") or str(s_dt.date())
        default_start_time = form_data.get("start_time_part") or s_dt.strftime("%H:%M")
        default_end_time = form_data.get("end_time_part") or e_dt.strftime("%H:%M")

        machine_options = []
        for m in machines:
            sel = " selected" if str(m["id"]) == sel_machine_id else ""
            machine_options.append(f'<option value="{m["id"]}"{sel}>{escape_html(m["name"])} ({escape_html(m["code"])})</option>')

        member_select_html = ""
        if is_staff:
            member_options = []
            for mem in members:
                sel = " selected" if str(mem["id"]) == sel_member_id else ""
                member_options.append(f'<option value="{mem["id"]}"{sel}>{escape_html(mem["full_name"])} ({escape_html(mem["member_number"])})</option>')
            member_select_html = f"""
            <div class="form-group">
                <label for="edit_member_id"><strong>Member Profile:</strong></label>
                <select name="member_id" id="edit_member_id" class="form-control" required>
                    {''.join(member_options)}
                </select>
            </div>
            """
        else:
            member_select_html = f"""
            <div class="form-group">
                <label><strong>Reserving Member:</strong></label>
                <input type="hidden" name="member_id" value="{existing['member_id']}">
                <input type="text" class="form-control" value="{escape_html(existing['member_name'])} ({escape_html(existing['member_number'])})" disabled>
            </div>
            """

        csrf_val = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_val)

        conflict_alert = ""
        if error_msg:
            conflict_alert = f"""
            <div class="alert alert-danger" style="margin-bottom: 1.5rem;">
                <h4 style="margin-top: 0; margin-bottom: 0.5rem;">Modification Error</h4>
                <p style="margin-bottom: 0;">{escape_html(error_msg)}</p>
            </div>
            """

        content = f"""
        <div class="page-header" style="margin-bottom: 1.5rem;">
            <h1>Edit Reservation #{res_id}</h1>
            <p class="text-muted">Modify machine, schedule window, or title with self-excluding conflict validation.</p>
        </div>

        {conflict_alert}

        <div class="card" style="max-width: 800px;">
            <div class="card-header">
                <h3 class="card-title">Update Reservation</h3>
            </div>
            <div class="card-body">
                <form method="POST" action="/reservations/{res_id}/edit">
                    {csrf_field}

                    <div class="form-group">
                        <label for="edit_machine_id"><strong>Machine:</strong></label>
                        <select name="machine_id" id="edit_machine_id" class="form-control" required>
                            {''.join(machine_options)}
                        </select>
                    </div>

                    {member_select_html}

                    <div class="form-group">
                        <label for="edit_title"><strong>Reservation Title:</strong></label>
                        <input type="text" name="title" id="edit_title" class="form-control" value="{escape_html(title_val)}" required>
                    </div>

                    <div style="display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 1rem; margin-bottom: 1.5rem;">
                        <div class="form-group" style="margin-bottom: 0;">
                            <label for="edit_date"><strong>Date:</strong></label>
                            <input type="date" name="date" id="edit_date" class="form-control" value="{escape_html(default_date)}" required>
                        </div>
                        <div class="form-group" style="margin-bottom: 0;">
                            <label for="edit_start_time"><strong>Start Time:</strong></label>
                            <input type="time" name="start_time" id="edit_start_time" class="form-control" value="{escape_html(default_start_time)}" required>
                        </div>
                        <div class="form-group" style="margin-bottom: 0;">
                            <label for="edit_end_time"><strong>End Time:</strong></label>
                            <input type="time" name="end_time" id="edit_end_time" class="form-control" value="{escape_html(default_end_time)}" required>
                        </div>
                    </div>

                    <div style="display: flex; gap: 1rem; align-items: center;">
                        <button type="submit" class="btn btn-primary">Save Changes</button>
                        <a href="/reservations/{res_id}" class="btn btn-secondary">Cancel</a>
                    </div>
                </form>
            </div>
        </div>
        """

        html_out = render_page(f"Edit Reservation #{res_id}", content, user=req.user, active_nav="reservations", csrf_token=csrf_val)
        return Response.html(html_out)

    @router.get("/reservations/{id}/edit")
    def html_edit_reservation_get(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=" + req.path)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.html(render_page("Invalid ID", "<div class='alert alert-danger'>Invalid reservation ID.</div>", user=req.user, active_nav="reservations"), status_code=400)

        return _render_edit_reservation_form(req, res_id, {})

    @router.post("/reservations/{id}/edit")
    def html_edit_reservation_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=" + req.path)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.html(render_page("Invalid ID", "<div class='alert alert-danger'>Invalid reservation ID.</div>", user=req.user, active_nav="reservations"), status_code=400)

        form_data = req.form()
        machine_id_raw = form_data.get("machine_id")
        member_id_raw = form_data.get("member_id")
        date_str = form_data.get("date")
        start_time_str = form_data.get("start_time")
        end_time_str = form_data.get("end_time")
        title = form_data.get("title", "").strip()

        if not machine_id_raw or not date_str or not start_time_str or not end_time_str:
            return _render_edit_reservation_form(req, res_id, form_data, error_msg="All fields are required.")

        try:
            machine_id = int(machine_id_raw)
        except (ValueError, TypeError):
            return _render_edit_reservation_form(req, res_id, form_data, error_msg="Invalid machine selected.")

        member_id = int(member_id_raw) if member_id_raw and member_id_raw.isdigit() else None
        full_start_iso = f"{date_str.strip()}T{start_time_str.strip()}:00"
        full_end_iso = f"{date_str.strip()}T{end_time_str.strip()}:00"
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            updated_obj = update_reservation(
                reservation_id=res_id,
                machine_id=machine_id,
                member_id=member_id,
                start_time=full_start_iso,
                end_time=full_end_iso,
                title=title,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.redirect(f"/reservations/{res_id}?success=Reservation+updated+successfully")
        except ReservationConflictError as e:
            return _render_edit_reservation_form(req, res_id, form_data, error_msg=str(e))
        except (ValueError, ReservationPermissionError) as e:
            return _render_edit_reservation_form(req, res_id, form_data, error_msg=str(e))
        except Exception as e:
            logger.error("Unexpected error in reservation edit: %s", e)
            return _render_edit_reservation_form(req, res_id, form_data, error_msg=f"Unexpected error: {e}")

    # -------------------------------------------------------------------------
    # HTML View: Cancel Reservation Form Action
    # -------------------------------------------------------------------------
    @router.post("/reservations/{id}/cancel")
    def html_cancel_reservation_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=" + req.path)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.redirect(f"/reservations?error=Invalid+reservation+ID")

        form_data = req.form()
        reason = form_data.get("reason") or "Cancelled by user"
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            cancelled_obj = cancel_reservation(
                reservation_id=res_id,
                reason=reason,
                actor_user=req.user,
                ip_address=client_ip,
            )
            promoted = cancelled_obj.get("promoted_waiting_list_entry")
            if promoted:
                msg = f"Reservation #{res_id} cancelled. Waiting list entry #{promoted['promoted_entry_id']} for {promoted['member_name']} was automatically promoted to Reservation #{promoted['promoted_reservation_id']}!"
                return Response.redirect(f"/reservations/{res_id}?success={escape_html(msg)}")
            return Response.redirect(f"/reservations/{res_id}?success=Reservation+cancelled+successfully")
        except Exception as e:
            return Response.redirect(f"/reservations/{res_id}?error={escape_html(str(e))}")

    # -------------------------------------------------------------------------
    # HTML View: Check-In, Check-Out, Late Arrival & No-Show Form Actions
    # -------------------------------------------------------------------------
    @router.post("/reservations/{id}/check-in")
    def html_check_in_reservation_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=" + req.path)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.redirect(f"/reservations?error=Invalid+reservation+ID")

        form_data = req.form()
        check_in_time = form_data.get("check_in_time") or None
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            check_in_reservation(
                reservation_id=res_id,
                actor_user=req.user,
                actual_check_in_time=check_in_time,
                ip_address=client_ip,
            )
            return Response.redirect(f"/reservations/{res_id}?success=Check-in+completed+successfully.+Workshop+session+started.")
        except Exception as e:
            return Response.redirect(f"/reservations/{res_id}?error={escape_html(str(e))}")

    @router.post("/reservations/{id}/check-out")
    def html_check_out_reservation_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=" + req.path)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.redirect(f"/reservations?error=Invalid+reservation+ID")

        form_data = req.form()
        notes = form_data.get("notes") or None
        check_out_time = form_data.get("check_out_time") or None
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            checked_out_obj = check_out_reservation(
                reservation_id=res_id,
                actor_user=req.user,
                actual_check_out_time=check_out_time,
                notes=notes,
                ip_address=client_ip,
            )
            duration_mins = checked_out_obj.get("actual_usage_minutes", 0)
            return Response.redirect(f"/reservations/{res_id}?success=Check-out+completed+successfully.+Total+usage:+{duration_mins}+minutes.")
        except Exception as e:
            return Response.redirect(f"/reservations/{res_id}?error={escape_html(str(e))}")

    @router.post("/reservations/{id}/mark-late")
    def html_mark_late_reservation_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=" + req.path)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.redirect(f"/reservations?error=Invalid+reservation+ID")

        form_data = req.form()
        reason = form_data.get("reason") or "Late arrival reported"
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            mark_reservation_late(
                reservation_id=res_id,
                actor_user=req.user,
                reason=reason,
                ip_address=client_ip,
            )
            return Response.redirect(f"/reservations/{res_id}?success=Reservation+marked+as+late.+Grace+period+in+effect.")
        except Exception as e:
            return Response.redirect(f"/reservations/{res_id}?error={escape_html(str(e))}")

    @router.post("/reservations/{id}/mark-no-show")
    def html_mark_no_show_reservation_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=" + req.path)

        res_id_raw = req.route_params.get("id")
        try:
            res_id = int(res_id_raw)
        except (ValueError, TypeError):
            return Response.redirect(f"/reservations?error=Invalid+reservation+ID")

        form_data = req.form()
        reason = form_data.get("reason") or "Member did not arrive"
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            noshow_obj = mark_reservation_no_show(
                reservation_id=res_id,
                actor_user=req.user,
                reason=reason,
                promote_waiting_list=True,
                ip_address=client_ip,
            )
            promoted = noshow_obj.get("promoted_waiting_list_entry")
            if promoted:
                msg = f"Reservation #{res_id} marked as no-show. Machine slot released and waiting list entry #{promoted['promoted_entry_id']} for {promoted['member_name']} was automatically promoted to Reservation #{promoted['promoted_reservation_id']}!"
                return Response.redirect(f"/reservations/{res_id}?success={escape_html(msg)}")
            return Response.redirect(f"/reservations/{res_id}?success=Reservation+marked+as+no-show.+Machine+slot+released.")
        except Exception as e:
            return Response.redirect(f"/reservations/{res_id}?error={escape_html(str(e))}")

    # -------------------------------------------------------------------------
    # HTML View: Machine Availability Explorer & Timeline (from S11)
    # -------------------------------------------------------------------------
    @router.get("/availability")
    def html_availability_view(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/availability")

        machines = query_all(
            """
            SELECT m.id, m.code, m.name, m.capacity, m.state, m.operating_hours_start, m.operating_hours_end,
                   c.name AS category_name
            FROM machines m
            LEFT JOIN machine_categories c ON c.id = m.category_id
            WHERE m.state != 'retired'
            ORDER BY m.name ASC;
            """
        )

        selected_machine_id = req.query("machine_id")
        selected_date = req.query("date") or today_rome_str()

        machine_obj = None
        timeline_data = None
        error_msg = ""

        if selected_machine_id:
            try:
                m_id = int(selected_machine_id)
                machine_obj = query_one("SELECT * FROM machines WHERE id = ?;", (m_id,))
                if machine_obj:
                    timeline_data = generate_timeline_slots(machine_id=m_id, date_str=selected_date)
            except Exception as e:
                error_msg = str(e)
        elif machines:
            try:
                default_m = machines[0]
                machine_obj = default_m
                selected_machine_id = str(default_m["id"])
                timeline_data = generate_timeline_slots(machine_id=default_m["id"], date_str=selected_date)
            except Exception as e:
                error_msg = str(e)

        machine_options = []
        for m in machines:
            sel = " selected" if str(m["id"]) == str(selected_machine_id) else ""
            machine_options.append(
                f'<option value="{m["id"]}"{sel}>{escape_html(m["name"])} ({escape_html(m["code"])}) - Cap: {m["capacity"]}</option>'
            )

        slots_html = ""
        if timeline_data and timeline_data.get("slots"):
            slot_rows = []
            for s in timeline_data["slots"]:
                status_class = "badge-success" if s["is_open"] else "badge-danger"
                status_label = s["status"].replace("_", " ").title()
                cap_info = f"{s['booked_count']}/{s['total_capacity']} booked ({s['remaining_capacity']} free)"

                res_details = ""
                if s["reservations"]:
                    res_items = [
                        f"<li><strong>#{r['id']}</strong> {escape_html(r.get('title') or 'Reservation')} ({escape_html(r['member_name'])})</li>"
                        for r in s["reservations"]
                    ]
                    res_details = f"<ul style='margin: 0; padding-left: 1.2rem; font-size: 0.85rem;'>{''.join(res_items)}</ul>"

                mw_details = ""
                if s["maintenance_windows"]:
                    mw_items = [
                        f"<li style='color: #d97706;'><strong>Maintenance #{mw['id']}</strong>: {escape_html(mw['title'])}</li>"
                        for mw in s["maintenance_windows"]
                    ]
                    mw_details = f"<ul style='margin: 0; padding-left: 1.2rem; font-size: 0.85rem;'>{''.join(mw_items)}</ul>"

                slot_rows.append(f"""
                <tr>
                    <td><strong>{escape_html(s['time_label'])}</strong></td>
                    <td><span class="badge {status_class}">{escape_html(status_label)}</span></td>
                    <td>{escape_html(cap_info)}</td>
                    <td>{res_details}{mw_details if mw_details else ('<span class="text-muted">None</span>' if not res_details else '')}</td>
                </tr>
                """)
            slots_html = "\n".join(slot_rows)
        else:
            slots_html = "<tr><td colspan='4' class='text-center text-muted'>No timeline data available for selected date/machine.</td></tr>"

        csrf_val = getattr(req, "csrf_token", "")

        content = f"""
        <div class="page-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
            <div>
                <h1>Machine Availability & Schedule Timeline</h1>
                <p class="text-muted">Inspect capacity, operating hours, active reservations, and maintenance windows across Europe/Rome timezone.</p>
            </div>
            <div>
                <a href="/reservations" class="btn btn-primary">Go to Reservations Calendar</a>
            </div>
        </div>

        <div class="card" style="margin-bottom: 1.5rem;">
            <div class="card-header">
                <h3 class="card-title">Select Machine & Date</h3>
            </div>
            <div class="card-body">
                <form method="GET" action="/availability" style="display: flex; gap: 1rem; align-items: flex-end; flex-wrap: wrap;">
                    <div class="form-group" style="flex: 1; min-width: 240px; margin-bottom: 0;">
                        <label for="machine_select">Machine:</label>
                        <select name="machine_id" id="machine_select" class="form-control" onchange="this.form.submit()">
                            {''.join(machine_options)}
                        </select>
                    </div>
                    <div class="form-group" style="width: 180px; margin-bottom: 0;">
                        <label for="date_input">Date:</label>
                        <input type="date" name="date" id="date_input" class="form-control" value="{escape_html(selected_date)}" onchange="this.form.submit()">
                    </div>
                    <div>
                        <button type="submit" class="btn btn-secondary">Refresh Timeline</button>
                    </div>
                </form>
            </div>
        </div>

        {f'<div class="alert alert-danger">{escape_html(error_msg)}</div>' if error_msg else ''}

        <div class="card">
            <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
                <h3 class="card-title">
                    Daily Schedule Timeline: {escape_html(machine_obj['name'] if machine_obj else '')} ({escape_html(selected_date)})
                </h3>
                {f'<span class="badge badge-info">Capacity: {machine_obj["capacity"]} units &bull; Hours: {machine_obj["operating_hours_start"]} - {machine_obj["operating_hours_end"]}</span>' if machine_obj else ''}
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table table-hover">
                        <thead>
                            <tr>
                                <th style="width: 180px;">Time Window</th>
                                <th style="width: 150px;">Status</th>
                                <th style="width: 220px;">Capacity Utilization</th>
                                <th>Active Bookings & Maintenance</th>
                            </tr>
                        </thead>
                        <tbody>
                            {slots_html}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        """

        html_out = render_page("Machine Availability", content, user=req.user, active_nav="reservations", csrf_token=csrf_val)
        return Response.html(html_out)

    # -------------------------------------------------------------------------
    # REST API: Waiting List Collection (List & Join)
    # -------------------------------------------------------------------------
    @router.get("/api/waiting-list")
    def api_list_waiting_list(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        user_role = get_user_role(req.user)
        machine_id_raw = req.query("machine_id")
        member_id_raw = req.query("member_id")
        status = req.query("status")
        start_date = req.query("start_date") or req.query("from_date")
        end_date = req.query("end_date") or req.query("to_date")
        limit_raw = req.query("limit") or "100"
        offset_raw = req.query("offset") or "0"

        machine_id = int(machine_id_raw) if machine_id_raw and machine_id_raw.isdigit() else None
        limit = min(max(1, int(limit_raw) if limit_raw.isdigit() else 100), 500)
        offset = max(0, int(offset_raw) if offset_raw.isdigit() else 0)

        member_id: Optional[int] = None
        if member_id_raw and member_id_raw.isdigit():
            member_id = int(member_id_raw)
        elif user_role == ROLE_MEMBER:
            # Members can filter for own or all, but default to own if requested
            pass

        results = list_waiting_list_entries(
            machine_id=machine_id,
            member_id=member_id,
            status=status,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )

        return Response.json({
            "success": True,
            "count": len(results),
            "waiting_list": results,
        })

    @router.post("/api/waiting-list")
    def api_join_waiting_list(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        user_role = get_user_role(req.user)
        if user_role == ROLE_VIEWER:
            return Response.json({"error": "Viewers have read-only access and cannot join the waiting list."}, status_code=403)

        body = req.json() or {}
        machine_id_raw = body.get("machine_id")
        member_id_raw = body.get("member_id")
        desired_start = body.get("desired_start_time") or body.get("start_time")
        desired_end = body.get("desired_end_time") or body.get("end_time")

        if not machine_id_raw or not desired_start or not desired_end:
            return Response.json(
                {
                    "error": "Missing required fields: 'machine_id', 'desired_start_time', 'desired_end_time'.",
                    "code": "missing_params",
                },
                status_code=400,
            )

        try:
            machine_id = int(machine_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid machine_id."}, status_code=400)

        member_id: int
        if user_role == ROLE_MEMBER:
            user_member = get_member_by_user_id(req.user["id"], include_qualifications=False)
            if not user_member:
                return Response.json({"error": "User account is not linked to an active member profile."}, status_code=403)
            member_id = user_member["id"]
        else:
            if not member_id_raw:
                return Response.json({"error": "Field 'member_id' is required for staff entries."}, status_code=400)
            try:
                member_id = int(member_id_raw)
            except (ValueError, TypeError):
                return Response.json({"error": "Invalid member_id."}, status_code=400)

        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            entry_obj = create_waiting_list_entry(
                machine_id=machine_id,
                member_id=member_id,
                desired_start_time=desired_start,
                desired_end_time=desired_end,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "entry": entry_obj}, status_code=201)
        except WaitingListEligibilityError as e:
            return Response.json({
                "error": str(e),
                "code": "waiting_list_ineligible",
                "conflicts": e.conflicts,
            }, status_code=409)
        except WaitingListPermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Unexpected error joining waiting list: %s", e)
            return Response.json({"error": f"Failed to join waiting list: {e}"}, status_code=500)

    # -------------------------------------------------------------------------
    # REST API: Single Waiting List Resource (Get, Cancel, Promote, Eligibility)
    # -------------------------------------------------------------------------
    @router.get("/api/waiting-list/{id}")
    def api_get_waiting_list_entry(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        entry_id_raw = req.route_params.get("id")
        try:
            entry_id = int(entry_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid waiting list entry ID."}, status_code=400)

        entry_obj = get_waiting_list_entry_by_id(entry_id, include_details=True)
        if not entry_obj:
            return Response.json({"error": f"Waiting list entry #{entry_id} not found."}, status_code=404)

        return Response.json({"success": True, "entry": entry_obj})

    @router.post("/api/waiting-list/{id}/cancel")
    @router.delete("/api/waiting-list/{id}")
    def api_cancel_waiting_list_entry(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        entry_id_raw = req.route_params.get("id")
        try:
            entry_id = int(entry_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid waiting list entry ID."}, status_code=400)

        body = req.json() or {}
        reason = body.get("reason") or req.query("reason") or "Cancelled via API"
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            cancelled_obj = cancel_waiting_list_entry(
                entry_id=entry_id,
                reason=reason,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "entry": cancelled_obj})
        except WaitingListNotFoundError as e:
            return Response.json({"error": str(e)}, status_code=404)
        except WaitingListPermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Unexpected error cancelling waiting list entry #%d: %s", entry_id, e)
            return Response.json({"error": f"Failed to cancel waiting list entry: {e}"}, status_code=500)

    @router.post("/api/waiting-list/{id}/promote")
    def api_promote_waiting_list_entry(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        entry_id_raw = req.route_params.get("id")
        try:
            entry_id = int(entry_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid waiting list entry ID."}, status_code=400)

        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            promote_result = promote_waiting_list_entry_by_id(
                entry_id=entry_id,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.json({"success": True, "result": promote_result})
        except WaitingListNotFoundError as e:
            return Response.json({"error": str(e)}, status_code=404)
        except (WaitingListPermissionError, WaitingListEligibilityError) as e:
            return Response.json({"error": str(e)}, status_code=403 if isinstance(e, WaitingListPermissionError) else 409)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Unexpected error promoting waiting list entry #%d: %s", entry_id, e)
            return Response.json({"error": f"Failed to promote waiting list entry: {e}"}, status_code=500)

    @router.get("/api/waiting-list/{id}/eligibility")
    def api_check_waiting_list_eligibility(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        entry_id_raw = req.route_params.get("id")
        try:
            entry_id = int(entry_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid waiting list entry ID."}, status_code=400)

        entry_obj = get_waiting_list_entry_by_id(entry_id, include_details=True)
        if not entry_obj:
            return Response.json({"error": f"Waiting list entry #{entry_id} not found."}, status_code=404)

        is_eligible, msg, conflicts = evaluate_entry_eligibility(entry_obj)
        return Response.json({
            "success": True,
            "entry_id": entry_id,
            "is_eligible": is_eligible,
            "message": msg,
            "conflicts": conflicts,
        })

    # -------------------------------------------------------------------------
    # HTML View: Waiting List Management
    # -------------------------------------------------------------------------
    @router.get("/waiting-list")
    def html_waiting_list_view(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/waiting-list")

        user_role = get_user_role(req.user)
        is_staff = is_operator_or_admin(req.user)
        can_join = user_role != ROLE_VIEWER

        status_tab = req.query("status") or "waiting"
        machine_filter = req.query("machine_id")
        m_id = int(machine_filter) if machine_filter and machine_filter.isdigit() else None

        user_member = None
        if user_role == ROLE_MEMBER:
            user_member = get_member_by_user_id(req.user["id"], include_qualifications=False)

        # Query entries based on tab
        filter_status = None if status_tab == "all" else status_tab
        entries = list_waiting_list_entries(
            machine_id=m_id,
            status=filter_status,
            limit=200,
        )

        machines = query_all("SELECT id, code, name FROM machines WHERE state != 'retired' ORDER BY name ASC;")
        machine_options = ['<option value="">-- All Machines --</option>']
        for m in machines:
            sel = " selected" if str(m["id"]) == str(machine_filter or "") else ""
            machine_options.append(f'<option value="{m["id"]}"{sel}>{escape_html(m["name"])} ({escape_html(m["code"])})</option>')

        csrf_val = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_val)

        # Tab bar counts
        all_active = len(list_waiting_list_entries(status="waiting", machine_id=m_id))
        all_promoted = len(list_waiting_list_entries(status="promoted", machine_id=m_id))
        all_total = len(list_waiting_list_entries(machine_id=m_id))

        tab_active_class = lambda t: "btn-primary" if status_tab == t else "btn-outline-secondary"

        entry_rows = []
        for e in entries:
            st = e["status"]
            badge_class = "badge-warning" if st == "waiting" else ("badge-success" if st == "promoted" else "badge-secondary")
            
            is_own = user_member and user_member["id"] == e["member_id"]
            can_cancel_this = (is_staff or is_own) and st == "waiting"
            can_promote_this = is_staff and st == "waiting"

            promoted_link = "-"
            if e.get("promoted_reservation_id"):
                promoted_link = f'<a href="/reservations/{e["promoted_reservation_id"]}" class="badge badge-success">Reservation #{e["promoted_reservation_id"]} &rarr;</a>'

            actions_html = []
            if can_promote_this:
                actions_html.append(f"""
                <form method="POST" action="/waiting-list/{e['id']}/promote" style="display:inline;" onsubmit="return confirm('Promote this waiting list entry into a confirmed reservation now?');">
                    {csrf_field}
                    <button type="submit" class="btn btn-sm btn-outline-success" title="Manually promote entry to reservation">Promote</button>
                </form>
                """)
            if can_cancel_this:
                actions_html.append(f"""
                <form method="POST" action="/waiting-list/{e['id']}/cancel" style="display:inline;" onsubmit="return confirm('Remove this entry from the waiting list?');">
                    {csrf_field}
                    <button type="submit" class="btn btn-sm btn-outline-danger" title="Cancel waiting list entry">Cancel</button>
                </form>
                """)

            entry_rows.append(f"""
            <tr>
                <td><strong>#{e['id']}</strong></td>
                <td>
                    <strong>{escape_html(e['machine_name'])}</strong>
                    <div style="font-size: 0.8rem; color: #64748b;">{escape_html(e['machine_code'])}</div>
                </td>
                <td>
                    <strong>{escape_html(e['member_name'])}</strong>
                    <div style="font-size: 0.8rem; color: #64748b;">{escape_html(e['member_number'])}</div>
                </td>
                <td>
                    <div><strong>{escape_html(e['desired_start_time_display'])}</strong></div>
                    <div style="font-size: 0.8rem; color: #64748b;">to {escape_html(e['desired_end_time_display'])} ({e['duration_minutes']} min)</div>
                </td>
                <td><span class="badge {badge_class}">{escape_html(st.title())}</span></td>
                <td>{promoted_link}</td>
                <td style="font-size: 0.85rem; color: #64748b;">{escape_html(e['formatted_created_at'])}</td>
                <td style="white-space: nowrap;">{' '.join(actions_html) if actions_html else '<span class="text-muted">-</span>'}</td>
            </tr>
            """)

        table_body = "".join(entry_rows) if entry_rows else "<tr><td colspan='8' class='text-center text-muted' style='padding: 2rem;'>No waiting list entries found for this view.</td></tr>"

        flash_success = req.query("success")
        flash_error = req.query("error")

        content = f"""
        <div class="page-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
            <div>
                <h1>Machine Waiting Lists</h1>
                <p class="text-muted">Queue requests when machines are busy. The first eligible member is automatically promoted upon reservation cancellation.</p>
            </div>
            <div style="display: flex; gap: 0.5rem;">
                {f'<a href="/waiting-list/new" class="btn btn-primary">+ Join Waiting List</a>' if can_join else ''}
                <a href="/reservations" class="btn btn-secondary">Reservations Calendar</a>
            </div>
        </div>

        {f'<div class="alert alert-success">{escape_html(flash_success)}</div>' if flash_success else ''}
        {f'<div class="alert alert-danger">{escape_html(flash_error)}</div>' if flash_error else ''}

        <div class="card" style="margin-bottom: 1.5rem;">
            <div class="card-body" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
                <div style="display: flex; gap: 0.5rem;">
                    <a href="/waiting-list?status=waiting{f'&machine_id={m_id}' if m_id else ''}" class="btn btn-sm {tab_active_class('waiting')}">
                        Active Queue ({all_active})
                    </a>
                    <a href="/waiting-list?status=promoted{f'&machine_id={m_id}' if m_id else ''}" class="btn btn-sm {tab_active_class('promoted')}">
                        Promoted ({all_promoted})
                    </a>
                    <a href="/waiting-list?status=all{f'&machine_id={m_id}' if m_id else ''}" class="btn btn-sm {tab_active_class('all')}">
                        All Records ({all_total})
                    </a>
                </div>
                <form method="GET" action="/waiting-list" style="display: flex; gap: 0.5rem; align-items: center;">
                    <input type="hidden" name="status" value="{escape_html(status_tab)}">
                    <select name="machine_id" class="form-control form-control-sm" onchange="this.form.submit()">
                        {''.join(machine_options)}
                    </select>
                </form>
            </div>
        </div>

        <div class="card">
            <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
                <h3 class="card-title">Waiting Queue Entries ({len(entries)})</h3>
                <span class="badge badge-info">FIFO Promotion &bull; Atomic Lock Protection</span>
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table table-hover">
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Machine</th>
                                <th>Member</th>
                                <th>Desired Slot</th>
                                <th>Status</th>
                                <th>Promoted Booking</th>
                                <th>Requested At</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {table_body}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        """

        html_out = render_page("Waiting Lists", content, user=req.user, active_nav="waiting-list", csrf_token=csrf_val)
        return Response.html(html_out)

    # -------------------------------------------------------------------------
    # HTML View: Join Waiting List Form (GET & POST)
    # -------------------------------------------------------------------------
    def _render_waiting_list_form(
        req: Request,
        form_data: Dict[str, Any],
        error_msg: str = "",
        conflict_details: Optional[List[Dict[str, Any]]] = None,
    ) -> Response:
        user = req.user
        user_role = get_user_role(user)
        is_staff = is_operator_or_admin(user)

        machines = query_all("SELECT id, code, name, capacity, state FROM machines WHERE state != 'retired' ORDER BY name ASC;")
        members = list_members(status="active") if is_staff else []

        sel_machine_id = str(form_data.get("machine_id") or "")
        sel_member_id = str(form_data.get("member_id") or "")
        date_val = form_data.get("date") or today_rome_str()
        start_time_val = form_data.get("start_time") or "10:00"
        end_time_val = form_data.get("end_time") or "12:00"

        machine_options = ['<option value="">-- Choose a Machine --</option>']
        for m in machines:
            sel = " selected" if str(m["id"]) == sel_machine_id else ""
            machine_options.append(f'<option value="{m["id"]}"{sel}>{escape_html(m["name"])} ({escape_html(m["code"])})</option>')

        member_select_html = ""
        if is_staff:
            member_options = ['<option value="">-- Select Member --</option>']
            for mem in members:
                sel = " selected" if str(mem["id"]) == sel_member_id else ""
                member_options.append(f'<option value="{mem["id"]}"{sel}>{escape_html(mem["full_name"])} ({escape_html(mem["member_number"])})</option>')
            member_select_html = f"""
            <div class="form-group">
                <label for="wl_member_id"><strong>Member Profile:</strong></label>
                <select name="member_id" id="wl_member_id" class="form-control" required>
                    {''.join(member_options)}
                </select>
                <small class="text-muted">Staff may place any active member on the waiting list.</small>
            </div>
            """
        else:
            user_member = get_member_by_user_id(user["id"], include_qualifications=False) if user else None
            mem_name = user_member["full_name"] if user_member else user.get("username", "Member")
            mem_num = user_member["member_number"] if user_member else "Self"
            member_select_html = f"""
            <div class="form-group">
                <label><strong>Reserving Member:</strong></label>
                <input type="text" class="form-control" value="{escape_html(mem_name)} ({escape_html(mem_num)})" disabled>
                <small class="text-muted">You are joining the waiting list for your own account.</small>
            </div>
            """

        csrf_val = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_val)

        conflict_alert = ""
        if error_msg:
            conflict_alert = f"""
            <div class="alert alert-danger" style="margin-bottom: 1.5rem;">
                <h4 style="margin-top: 0; margin-bottom: 0.5rem;">Cannot Join Waiting List</h4>
                <p style="margin-bottom: 0;">{escape_html(error_msg)}</p>
            </div>
            """

        content = f"""
        <div class="page-header" style="margin-bottom: 1.5rem;">
            <h1>Join Machine Waiting List</h1>
            <p class="text-muted">Queue for a machine slot. When a conflicting reservation is cancelled, eligible entries are promoted atomically in FIFO order.</p>
        </div>

        {conflict_alert}

        <div class="card" style="max-width: 650px; margin: 0 auto;">
            <div class="card-header">
                <h3 class="card-title">Desired Machine &amp; Time Slot</h3>
            </div>
            <div class="card-body">
                <form method="POST" action="/waiting-list/new">
                    {csrf_field}

                    <div class="form-group">
                        <label for="wl_machine_id"><strong>Machine:</strong></label>
                        <select name="machine_id" id="wl_machine_id" class="form-control" required>
                            {''.join(machine_options)}
                        </select>
                    </div>

                    {member_select_html}

                    <div class="form-group">
                        <label for="wl_date"><strong>Desired Date (Europe/Rome):</strong></label>
                        <input type="date" name="date" id="wl_date" class="form-control" value="{escape_html(date_val)}" required>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                        <div class="form-group">
                            <label for="wl_start_time"><strong>Start Time:</strong></label>
                            <input type="time" name="start_time" id="wl_start_time" class="form-control" value="{escape_html(start_time_val)}" required>
                        </div>
                        <div class="form-group">
                            <label for="wl_end_time"><strong>End Time:</strong></label>
                            <input type="time" name="end_time" id="wl_end_time" class="form-control" value="{escape_html(end_time_val)}" required>
                        </div>
                    </div>

                    <div style="display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1.5rem;">
                        <a href="/waiting-list" class="btn btn-secondary">Cancel</a>
                        <button type="submit" class="btn btn-primary">Join Waiting List</button>
                    </div>
                </form>
            </div>
        </div>
        """

        html_out = render_page("Join Waiting List", content, user=req.user, active_nav="waiting-list", csrf_token=csrf_val)
        return Response.html(html_out)

    @router.get("/waiting-list/new")
    def html_join_waiting_list_get(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/waiting-list/new")

        user_role = get_user_role(req.user)
        if user_role == ROLE_VIEWER:
            return Response.html(
                render_page("Access Denied", "<div class='alert alert-danger'>Viewers have read-only access.</div>", user=req.user, active_nav="waiting-list"),
                status_code=403,
            )

        form_data = {
            "machine_id": req.query("machine_id"),
            "member_id": req.query("member_id"),
            "date": req.query("date") or today_rome_str(),
            "start_time": req.query("start_time") or "10:00",
            "end_time": req.query("end_time") or "12:00",
        }
        return _render_waiting_list_form(req, form_data)

    @router.post("/waiting-list/new")
    def html_join_waiting_list_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/waiting-list/new")

        user_role = get_user_role(req.user)
        if user_role == ROLE_VIEWER:
            return Response.html(
                render_page("Access Denied", "<div class='alert alert-danger'>Viewers have read-only access.</div>", user=req.user, active_nav="waiting-list"),
                status_code=403,
            )

        form_data = req.form()
        machine_id_raw = form_data.get("machine_id")
        member_id_raw = form_data.get("member_id")
        date_str = form_data.get("date")
        start_time_str = form_data.get("start_time")
        end_time_str = form_data.get("end_time")

        if not machine_id_raw or not date_str or not start_time_str or not end_time_str:
            return _render_waiting_list_form(req, form_data, error_msg="Please fill out all required fields.")

        try:
            machine_id = int(machine_id_raw)
        except (ValueError, TypeError):
            return _render_waiting_list_form(req, form_data, error_msg="Invalid machine selected.")

        member_id: int
        if user_role == ROLE_MEMBER:
            user_member = get_member_by_user_id(req.user["id"], include_qualifications=False)
            if not user_member:
                return _render_waiting_list_form(req, form_data, error_msg="Your user account is not linked to an active member profile.")
            member_id = user_member["id"]
        else:
            if not member_id_raw:
                return _render_waiting_list_form(req, form_data, error_msg="Please select a member profile.")
            try:
                member_id = int(member_id_raw)
            except (ValueError, TypeError):
                return _render_waiting_list_form(req, form_data, error_msg="Invalid member ID.")

        start_iso = f"{date_str}T{start_time_str}:00"
        end_iso = f"{date_str}T{end_time_str}:00"
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            entry_obj = create_waiting_list_entry(
                machine_id=machine_id,
                member_id=member_id,
                desired_start_time=start_iso,
                desired_end_time=end_iso,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.redirect(f"/waiting-list?success=Successfully+joined+the+waiting+list+(Entry+#{entry_obj['id']})")
        except WaitingListEligibilityError as e:
            return _render_waiting_list_form(req, form_data, error_msg=str(e), conflict_details=e.conflicts)
        except (ValueError, WaitingListPermissionError) as e:
            return _render_waiting_list_form(req, form_data, error_msg=str(e))
        except Exception as e:
            logger.error("Unexpected error joining waiting list: %s", e)
            return _render_waiting_list_form(req, form_data, error_msg=f"Unexpected error: {e}")

    @router.post("/waiting-list/{id}/cancel")
    def html_cancel_waiting_list_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/waiting-list")

        entry_id_raw = req.route_params.get("id")
        try:
            entry_id = int(entry_id_raw)
        except (ValueError, TypeError):
            return Response.redirect(f"/waiting-list?error=Invalid+waiting+list+entry+ID")

        form_data = req.form()
        reason = form_data.get("reason") or "Cancelled by user"
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            cancel_waiting_list_entry(
                entry_id=entry_id,
                reason=reason,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.redirect(f"/waiting-list?success=Waiting+list+entry+#{entry_id}+cancelled")
        except Exception as e:
            return Response.redirect(f"/waiting-list?error={escape_html(str(e))}")

    @router.post("/waiting-list/{id}/promote")
    def html_promote_waiting_list_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=/waiting-list")

        entry_id_raw = req.route_params.get("id")
        try:
            entry_id = int(entry_id_raw)
        except (ValueError, TypeError):
            return Response.redirect(f"/waiting-list?error=Invalid+waiting+list+entry+ID")

        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            result = promote_waiting_list_entry_by_id(
                entry_id=entry_id,
                actor_user=req.user,
                ip_address=client_ip,
            )
            new_res_id = result.get("reservation_id")
            return Response.redirect(f"/reservations/{new_res_id}?success=Waiting+list+entry+#{entry_id}+promoted+to+Reservation+#{new_res_id}!")
        except Exception as e:
            return Response.redirect(f"/waiting-list?error={escape_html(str(e))}")


# Alias for backward compatibility
register_availability_routes = register_reservation_routes

