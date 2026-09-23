"""Comprehensive Test Suite for ForgeDesk Reservation Check-In, Check-Out, Late Arrival & No-Show (Step S15)."""

import datetime
import json
import unittest
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from app import create_application_router
from forgedesk.auth.service import authenticate_user, create_user_session
from forgedesk.config import SECRET_KEY
from forgedesk.core.http import Request, Response
from forgedesk.db.connection import get_connection, query_all, query_one
from forgedesk.db.migrations import apply_migrations, reset_database
from forgedesk.db.seed import seed_database
from forgedesk.machines.service import create_machine, create_maintenance_window
from forgedesk.members.service import create_member, grant_qualification
from forgedesk.reservations.service import (
    ReservationConflictError,
    ReservationEligibilityError,
    ReservationNotFoundError,
    ReservationPermissionError,
    cancel_reservation,
    check_in_reservation,
    check_out_reservation,
    create_reservation,
    get_reservation_by_id,
    mark_reservation_late,
    mark_reservation_no_show,
    process_overdue_reservations,
)
from forgedesk.reservations.waiting_list import create_waiting_list_entry
from forgedesk.utils.crypto import generate_csrf_token
from forgedesk.utils.datetime_tz import format_iso, now_rome, now_rome_iso, parse_datetime, today_rome_str


class TestReservationsLifecycle(unittest.TestCase):
    """Test suite for Step S15 reservation lifecycle state transitions and temporal checks."""

    @classmethod
    def setUpClass(cls):
        """Reset and seed database once for test class."""
        reset_database()
        apply_migrations()
        seed_database(force=True)

        cls.router = create_application_router()

        # Authenticate demo accounts
        cls.admin_user = authenticate_user("admin", "admin123")
        cls.admin_session = create_user_session(cls.admin_user["id"])

        cls.operator_user = authenticate_user("operator", "operator123")
        cls.operator_session = create_user_session(cls.operator_user["id"])

        cls.member_alice = authenticate_user("member_alice", "member123")
        cls.alice_session = create_user_session(cls.member_alice["id"])

        cls.member_bob = authenticate_user("member_bob", "member123")
        cls.bob_session = create_user_session(cls.member_bob["id"])

        cls.member_clara = authenticate_user("member_clara", "member123")
        cls.clara_session = create_user_session(cls.member_clara["id"])

        cls.viewer_user = authenticate_user("viewer", "viewer123")
        cls.viewer_session = create_user_session(cls.viewer_user["id"])

        # Fetch member records
        cls.alice_member = query_one("SELECT * FROM members WHERE user_id = ?;", (cls.member_alice["id"],))
        cls.bob_member = query_one("SELECT * FROM members WHERE user_id = ?;", (cls.member_bob["id"],))
        cls.clara_member = query_one("SELECT * FROM members WHERE user_id = ?;", (cls.member_clara["id"],))

        # Fetch test machines
        cls.fdm_printer = query_one("SELECT * FROM machines WHERE code = 'PRUSA-MK4-01';")
        cls.laser_cutter = query_one("SELECT * FROM machines WHERE code = 'EPILOG-FUSION-PRO';")

    def _make_request(
        self,
        method: str,
        path: str,
        bearer_token: Optional[str] = None,
        cookie_session: Optional[str] = None,
        body: Optional[Any] = None,
        content_type: Optional[str] = None,
        query_params: Optional[Dict[str, str]] = None,
        client_ip: str = "127.0.0.1",
    ) -> Response:
        headers = {}
        if bearer_token:
            headers["authorization"] = f"Bearer {bearer_token}"
        if cookie_session:
            headers["cookie"] = f"fd_session={cookie_session}"
        if content_type:
            headers["content-type"] = content_type

        body_bytes = b""
        if body is not None:
            if isinstance(body, str):
                body_bytes = body.encode("utf-8")
            elif isinstance(body, dict):
                if content_type == "application/x-www-form-urlencoded":
                    body_bytes = urlencode(body).encode("utf-8")
                else:
                    body_bytes = json.dumps(body).encode("utf-8")
                    if not content_type:
                        headers["content-type"] = "application/json"

        req_path = path
        if query_params:
            req_path = f"{path}?{urlencode(query_params)}"

        req = Request(
            method=method.upper(),
            path=req_path,
            headers=headers,
            body=body_bytes,
            client_address=(client_ip, 54321),
        )
        return self.router.dispatch(req)

    def test_01_check_in_happy_path(self):
        """Test successful reservation check-in and timestamp recording."""
        start_time = "2026-08-25T09:00:00+02:00"
        end_time = "2026-08-25T11:00:00+02:00"

        res = create_reservation(
            machine_id=self.fdm_printer["id"],
            member_id=self.alice_member["id"],
            start_time=start_time,
            end_time=end_time,
            title="3D Prototype Print",
            actor_user=self.member_alice,
        )
        self.assertEqual(res["status"], "confirmed")
        self.assertIsNone(res["actual_check_in"])

        # Check-in at start time by admin
        checked_in = check_in_reservation(
            reservation_id=res["id"],
            actor_user=self.admin_user,
            actual_check_in_time=start_time,
        )

        self.assertEqual(checked_in["status"], "checked_in")
        self.assertEqual(checked_in["actual_check_in"], start_time)

        # Verify DB record
        db_res = get_reservation_by_id(res["id"])
        self.assertEqual(db_res["status"], "checked_in")
        self.assertEqual(db_res["actual_check_in"], start_time)

        # Audit event verification
        audit = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'reservation' AND object_id = ? AND action = 'reservation:check_in';",
            (str(res["id"]),),
        )
        self.assertIsNotNone(audit)

        # Attempting check-in again raises ValueError
        with self.assertRaises(ValueError) as ctx:
            check_in_reservation(
                reservation_id=res["id"],
                actor_user=self.member_alice,
            )
        self.assertIn("already checked in", str(ctx.exception).lower())

    def test_02_check_in_qualification_expired_before_check_in(self):
        """Test critical brief requirement: qualification valid at booking but expired at check-in must be rejected."""
        dave_id = create_member(
            full_name="Dave Expiring",
            email="dave.expiring@makerspace.test",
            membership_status="active",
            membership_expiry="2026-12-31",
            actor_user=self.admin_user,
        )

        laser_cat = query_one("SELECT * FROM machine_categories WHERE name LIKE '%Laser%';")

        # Grant qualification valid until 2026-08-22
        grant_qualification(
            member_id=dave_id,
            category_id=laser_cat["id"],
            qualification_name="Laser Safety Basic",
            issue_date="2026-08-01",
            expiry_date="2026-08-22",
            actor_user=self.operator_user,
        )

        # Create reservation for 2026-08-20 (valid at creation time)
        booking_start = "2026-08-20T10:00:00+02:00"
        booking_end = "2026-08-20T12:00:00+02:00"

        res = create_reservation(
            machine_id=self.laser_cutter["id"],
            member_id=dave_id,
            start_time=booking_start,
            end_time=booking_end,
            title="Laser Acrylic Cut",
            actor_user=self.admin_user,
        )
        self.assertEqual(res["status"], "confirmed")

        # Now simulate check-in occurring on 2026-08-24 (after qualification expired on 2026-08-22)
        checkin_time_expired = "2026-08-24T10:00:00+02:00"

        with self.assertRaises(ReservationEligibilityError) as ctx:
            check_in_reservation(
                reservation_id=res["id"],
                actor_user=self.admin_user,
                actual_check_in_time=checkin_time_expired,
            )
        self.assertIn("expires on 2026-08-22", str(ctx.exception))

    def test_03_check_in_machine_unavailable_or_under_maintenance(self):
        """Test check-in fails when machine is in maintenance or out of service."""
        maint_machine = create_machine(
            code="MILL-MAINT-01",
            name="CNC Mill for Maintenance Test",
            category_id=self.fdm_printer["category_id"],
            capacity=1,
            hourly_rate_cents=1000,
        )

        start_time = "2026-09-16T10:00:00+02:00"
        end_time = "2026-09-16T12:00:00+02:00"

        res = create_reservation(
            machine_id=maint_machine["id"],
            member_id=self.alice_member["id"],
            start_time=start_time,
            end_time=end_time,
            title="CNC Milling Project",
            actor_user=self.member_alice,
        )

        # Set machine state to 'under_maintenance'
        conn = get_connection()
        conn.execute("UPDATE machines SET state = 'under_maintenance' WHERE id = ?;", (maint_machine["id"],))
        conn.commit()
        conn.close()

        with self.assertRaises(ReservationConflictError) as ctx:
            check_in_reservation(
                reservation_id=res["id"],
                actor_user=self.member_alice,
                actual_check_in_time=start_time,
            )
        self.assertIn("under maintenance", str(ctx.exception).lower())

    def test_04_check_out_happy_path_and_duration_calculation(self):
        """Test check-out sets status to checked_out, calculates duration and estimated charge."""
        start_time = "2026-08-26T14:00:00+02:00"
        end_time = "2026-08-26T17:00:00+02:00"

        res = create_reservation(
            machine_id=self.fdm_printer["id"],
            member_id=self.bob_member["id"],
            start_time=start_time,
            end_time=end_time,
            title="Bob's Check-Out Project",
            actor_user=self.member_bob,
        )

        # Check-in at 14:00 by admin
        check_in_reservation(
            reservation_id=res["id"],
            actor_user=self.admin_user,
            actual_check_in_time=start_time,
        )

        # Check-out at 15:45 (105 minutes duration) by admin
        check_out_time = "2026-08-26T15:45:00+02:00"
        checked_out = check_out_reservation(
            reservation_id=res["id"],
            actor_user=self.admin_user,
            actual_check_out_time=check_out_time,
            notes="Session completed normally, printer cleaned.",
        )

        self.assertEqual(checked_out["status"], "checked_out")
        self.assertEqual(checked_out["actual_check_out"], check_out_time)
        self.assertEqual(checked_out["actual_usage_minutes"], 105)
        self.assertGreater(checked_out["usage_charge_estimate"]["estimated_total_cents"], 0)

        # Check audit event
        audit = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'reservation' AND object_id = ? AND action = 'reservation:check_out';",
            (str(res["id"]),),
        )
        self.assertIsNotNone(audit)
        audit_details = json.loads(audit["details_json"])
        self.assertEqual(audit_details["duration_minutes"], 105)

        # Checking out a second time raises ValueError
        with self.assertRaises(ValueError) as ctx:
            check_out_reservation(
                reservation_id=res["id"],
                actor_user=self.member_bob,
            )
        self.assertIn("already checked out", str(ctx.exception).lower())

    def test_05_check_out_fails_if_not_checked_in(self):
        """Test check-out cannot be performed on a confirmed (non-checked-in) reservation."""
        start_time = "2026-09-18T10:00:00+02:00"
        end_time = "2026-09-18T12:00:00+02:00"

        res = create_reservation(
            machine_id=self.fdm_printer["id"],
            member_id=self.alice_member["id"],
            start_time=start_time,
            end_time=end_time,
            title="Alice Direct Checkout",
            actor_user=self.member_alice,
        )

        with self.assertRaises(ValueError) as ctx:
            check_out_reservation(
                reservation_id=res["id"],
                actor_user=self.member_alice,
            )
        self.assertIn("must be checked in first", str(ctx.exception).lower())

    def test_06_mark_late_and_late_check_in(self):
        """Test marking reservation late and subsequent check-in."""
        start_time = "2026-08-27T09:00:00+02:00"
        end_time = "2026-08-27T11:00:00+02:00"

        res = create_reservation(
            machine_id=self.fdm_printer["id"],
            member_id=self.alice_member["id"],
            start_time=start_time,
            end_time=end_time,
            title="Late Arrival Test",
            actor_user=self.member_alice,
        )

        # Mark late
        late_res = mark_reservation_late(
            reservation_id=res["id"],
            actor_user=self.member_alice,
            reason="Bus delay, arriving 20 min late",
        )
        self.assertEqual(late_res["status"], "late")

        # Check-in while late by operator
        checked_in = check_in_reservation(
            reservation_id=res["id"],
            actor_user=self.operator_user,
            actual_check_in_time="2026-08-27T09:25:00+02:00",
        )
        self.assertEqual(checked_in["status"], "checked_in")
        self.assertEqual(checked_in["actual_check_in"], "2026-08-27T09:25:00+02:00")

    def test_07_mark_no_show_and_waiting_list_promotion(self):
        """Test mark no-show transitions to no_show and automatically promotes waiting list entry."""
        start_time = "2026-09-20T10:00:00+02:00"
        end_time = "2026-09-20T12:00:00+02:00"

        # Alice creates reservation
        res = create_reservation(
            machine_id=self.fdm_printer["id"],
            member_id=self.alice_member["id"],
            start_time=start_time,
            end_time=end_time,
            title="Alice Slot to be No-Show",
            actor_user=self.member_alice,
        )

        # Bob joins waiting list for same slot
        wl_entry = create_waiting_list_entry(
            machine_id=self.fdm_printer["id"],
            member_id=self.bob_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.member_bob,
        )

        # Alice reservation is marked no-show by operator -> Bob promoted
        no_show_res = mark_reservation_no_show(
            reservation_id=res["id"],
            actor_user=self.operator_user,
            reason="Member did not show up within grace window",
            promote_waiting_list=True,
        )
        self.assertEqual(no_show_res["status"], "no_show")

        # Verify waiting list was promoted
        promoted_info = no_show_res.get("promoted_waiting_list_entry")
        self.assertIsNotNone(promoted_info)
        self.assertEqual(promoted_info["promoted_entry_id"], wl_entry["id"])

        # Check Bob's promoted reservation
        promoted_res = get_reservation_by_id(promoted_info["promoted_reservation_id"])
        self.assertEqual(promoted_res["member_id"], self.bob_member["id"])
        self.assertEqual(promoted_res["status"], "confirmed")

    def test_08_rbac_permissions(self):
        """Test RBAC rules: Members can only check in/out own; Viewers cannot; Staff can check in/out any."""
        start_time = "2026-08-28T10:00:00+02:00"
        end_time = "2026-08-28T12:00:00+02:00"

        # Alice creates reservation
        res = create_reservation(
            machine_id=self.fdm_printer["id"],
            member_id=self.alice_member["id"],
            start_time=start_time,
            end_time=end_time,
            title="RBAC Test Booking",
            actor_user=self.member_alice,
        )

        # Bob attempts to check in Alice's reservation -> PermissionError
        with self.assertRaises(ReservationPermissionError):
            check_in_reservation(
                reservation_id=res["id"],
                actor_user=self.member_bob,
            )

        # Viewer attempts to check in Alice's reservation -> PermissionError
        with self.assertRaises(ReservationPermissionError):
            check_in_reservation(
                reservation_id=res["id"],
                actor_user=self.viewer_user,
            )

        # Operator checks in Alice's reservation -> Success
        checked_in = check_in_reservation(
            reservation_id=res["id"],
            actor_user=self.operator_user,
            actual_check_in_time=start_time,
        )
        self.assertEqual(checked_in["status"], "checked_in")

        # Bob attempts to check out Alice's reservation -> PermissionError
        with self.assertRaises(ReservationPermissionError):
            check_out_reservation(
                reservation_id=res["id"],
                actor_user=self.member_bob,
            )

        # Operator checks out Alice's reservation -> Success
        checked_out = check_out_reservation(
            reservation_id=res["id"],
            actor_user=self.operator_user,
            actual_check_out_time="2026-08-28T11:30:00+02:00",
        )
        self.assertEqual(checked_out["status"], "checked_out")

    def test_09_rest_api_lifecycle_workflow(self):
        """Test complete lifecycle via REST API endpoints."""
        start_time = "2026-08-29T14:00:00+02:00"
        end_time = "2026-08-29T16:00:00+02:00"

        # 1. Create reservation via API
        resp = self._make_request(
            "POST",
            "/api/reservations",
            bearer_token=self.alice_session,
            body={
                "machine_id": self.fdm_printer["id"],
                "member_id": self.alice_member["id"],
                "start_time": start_time,
                "end_time": end_time,
                "title": "API Lifecycle Test",
            },
        )
        self.assertEqual(resp.status_code, 201)
        res_data = json.loads(resp.body.decode("utf-8"))["reservation"]
        res_id = res_data["id"]

        # 2. Mark Late via API
        resp = self._make_request(
            "POST",
            f"/api/reservations/{res_id}/late",
            bearer_token=self.alice_session,
            body={"reason": "Traffic delay on API"},
        )
        self.assertEqual(resp.status_code, 200)
        late_json = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(late_json["reservation"]["status"], "late")

        # 3. Check-In via API (Admin with manual timestamp override)
        resp = self._make_request(
            "POST",
            f"/api/reservations/{res_id}/check-in",
            bearer_token=self.admin_session,
            body={"actual_check_in_time": "2026-08-29T14:15:00+02:00"},
        )
        self.assertEqual(resp.status_code, 200)
        in_json = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(in_json["reservation"]["status"], "checked_in")
        self.assertEqual(in_json["reservation"]["actual_check_in"], "2026-08-29T14:15:00+02:00")

        # 4. Check-Out via API (Admin with manual timestamp override)
        resp = self._make_request(
            "POST",
            f"/api/reservations/{res_id}/check-out",
            bearer_token=self.admin_session,
            body={
                "actual_check_out_time": "2026-08-29T15:45:00+02:00",
                "notes": "Finished print via REST API",
            },
        )
        self.assertEqual(resp.status_code, 200)
        out_json = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(out_json["reservation"]["status"], "checked_out")
        self.assertEqual(out_json["reservation"]["actual_usage_minutes"], 90)

    def test_10_html_views_and_form_post_actions(self):
        """Test HTML view rendering and POST action endpoints with CSRF protection."""
        start_time = "2026-09-23T10:00:00+02:00"
        end_time = "2026-09-23T12:00:00+02:00"

        res = create_reservation(
            machine_id=self.fdm_printer["id"],
            member_id=self.alice_member["id"],
            start_time=start_time,
            end_time=end_time,
            title="HTML UI Lifecycle Booking",
            actor_user=self.member_alice,
        )

        # 1. GET detail view as Alice -> verify HTML buttons and CSRF token rendered
        resp = self._make_request(
            "GET",
            f"/reservations/{res['id']}",
            cookie_session=self.alice_session,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8")
        self.assertIn("Check In", html)
        self.assertIn("Report Late", html)
        self.assertIn('name="csrf_token"', html)

        # 2. POST without CSRF token must be rejected with 403 Forbidden
        resp_no_csrf = self._make_request(
            "POST",
            f"/reservations/{res['id']}/check-in",
            cookie_session=self.alice_session,
            content_type="application/x-www-form-urlencoded",
            body={"check_in_time": start_time},
        )
        self.assertEqual(resp_no_csrf.status_code, 403)
        self.assertIn("CSRF verification failed", resp_no_csrf.body.decode("utf-8"))

        # 3. POST with valid CSRF token -> success 303/302 redirect
        alice_csrf = generate_csrf_token(self.alice_session, SECRET_KEY)
        resp = self._make_request(
            "POST",
            f"/reservations/{res['id']}/check-in",
            cookie_session=self.alice_session,
            content_type="application/x-www-form-urlencoded",
            body={
                "check_in_time": start_time,
                "csrf_token": alice_csrf,
            },
        )
        self.assertIn(resp.status_code, (302, 303))
        self.assertIn(f"/reservations/{res['id']}", resp.headers.get("location", "") or resp.headers.get("Location", ""))

        # 4. GET detail view again -> Check Out button present, Actual Check-In timestamp shown
        resp = self._make_request(
            "GET",
            f"/reservations/{res['id']}",
            cookie_session=self.alice_session,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8")
        self.assertIn("Check Out", html)
        self.assertIn("Actual Check-In", html)

        # 5. POST /reservations/{id}/check-out form with valid CSRF -> 303/302 redirect
        resp = self._make_request(
            "POST",
            f"/reservations/{res['id']}/check-out",
            cookie_session=self.alice_session,
            content_type="application/x-www-form-urlencoded",
            body={
                "notes": "All cleaned up",
                "csrf_token": alice_csrf,
            },
        )
        self.assertIn(resp.status_code, (302, 303))

        # 6. Verify final status is checked_out and check_out timestamp is recorded
        res_after = get_reservation_by_id(res["id"])
        self.assertEqual(res_after["status"], "checked_out")
        self.assertIsNotNone(res_after["actual_check_out"])

    def test_11_process_overdue_reservations(self):
        """Test scanning and auto-transitioning overdue reservations."""
        # Create an overdue booking in the past
        past_start = "2026-08-01T09:00:00+02:00"
        past_end = "2026-08-01T11:00:00+02:00"

        res = create_reservation(
            machine_id=self.fdm_printer["id"],
            member_id=self.alice_member["id"],
            start_time=past_start,
            end_time=past_end,
            title="Overdue Past Reservation",
            actor_user=self.member_alice,
        )

        scan_result = process_overdue_reservations(
            current_time="2026-08-01T12:00:00+02:00",
            actor_user=self.admin_user,
        )
        self.assertIn(res["id"], scan_result["marked_no_show_ids"])

        db_res = get_reservation_by_id(res["id"])
        self.assertEqual(db_res["status"], "no_show")


if __name__ == "__main__":
    unittest.main()
