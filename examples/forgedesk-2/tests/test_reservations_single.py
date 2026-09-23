"""Comprehensive Test Suite for ForgeDesk Single Reservations, Editing, Cancellation & Temporal Qualifications (Step S12)."""

import datetime
import json
import unittest
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from app import create_application_router
from forgedesk.auth.service import authenticate_user, create_user_session
from forgedesk.core.http import Request, Response
from forgedesk.db.connection import get_connection, query_all, query_one
from forgedesk.db.migrations import apply_migrations, reset_database
from forgedesk.db.seed import seed_database
from forgedesk.machines.service import create_machine, create_maintenance_window
from forgedesk.members.service import create_member, grant_qualification, list_members
from forgedesk.reservations.availability import check_machine_availability, find_interval_conflicts
from forgedesk.reservations.service import (
    ReservationConflictError,
    ReservationNotFoundError,
    ReservationPermissionError,
    cancel_reservation,
    create_reservation,
    estimate_reservation_charge,
    get_reservation_by_id,
    list_reservations,
    update_reservation,
)
from forgedesk.utils.datetime_tz import format_iso, now_rome, now_rome_iso, parse_datetime, today_rome_str


class TestSingleReservations(unittest.TestCase):
    """Test suite for Step S12 single reservations, edits, cancellations, qualifications, and RBAC."""

    @classmethod
    def setUpClass(cls):
        """Reset and seed database once for test class."""
        reset_database()
        apply_migrations()
        seed_database(force=True)

        cls.router = create_application_router()

        # Demo accounts
        cls.admin_user = authenticate_user("admin", "admin123")
        cls.admin_session = create_user_session(cls.admin_user["id"])

        cls.operator_user = authenticate_user("operator", "operator123")
        cls.operator_session = create_user_session(cls.operator_user["id"])

        cls.member_alice = authenticate_user("member_alice", "member123")
        cls.alice_session = create_user_session(cls.member_alice["id"])

        cls.member_bob = authenticate_user("member_bob", "member123")
        cls.bob_session = create_user_session(cls.member_bob["id"])

        cls.viewer_user = authenticate_user("viewer", "viewer123")
        cls.viewer_session = create_user_session(cls.viewer_user["id"])

        # Fetch Alice and Bob member records
        alice_mem = query_one("SELECT * FROM members WHERE user_id = ?;", (cls.member_alice["id"],))
        cls.alice_member_id = alice_mem["id"]

        bob_mem = query_one("SELECT * FROM members WHERE user_id = ?;", (cls.member_bob["id"],))
        cls.bob_member_id = bob_mem["id"]

    def _make_request(
        self,
        method: str,
        path: str,
        bearer_token: Optional[str] = None,
        body: Optional[Any] = None,
        content_type: Optional[str] = None,
        query_params: Optional[Dict[str, str]] = None,
        client_ip: str = "127.0.0.1",
    ) -> Response:
        headers = {}
        if bearer_token:
            headers["authorization"] = f"Bearer {bearer_token}"
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

        full_path = path
        if query_params:
            full_path = f"{path}?{urlencode(query_params)}"

        req = Request(
            method=method,
            path=full_path,
            headers=headers,
            body=body_bytes,
            client_address=(client_ip, 50000),
        )
        return self.router.dispatch(req)

    # -------------------------------------------------------------------------
    # 1. Single Reservation Creation
    # -------------------------------------------------------------------------
    def test_01_create_single_reservation_success(self):
        """Test successful single reservation creation with valid qualifications and timings."""
        today = today_rome_str()
        # Machine 1 is 3D Printer (Alice is qualified for category 1)
        start_time = f"{today}T10:00:00"
        end_time = f"{today}T12:00:00"

        res = create_reservation(
            machine_id=1,
            member_id=self.alice_member_id,
            start_time=start_time,
            end_time=end_time,
            title="Alice 3D Print Job",
            actor_user=self.member_alice,
        )

        self.assertIsNotNone(res)
        self.assertEqual(res["machine_id"], 1)
        self.assertEqual(res["member_id"], self.alice_member_id)
        self.assertEqual(res["status"], "confirmed")
        self.assertEqual(res["duration_minutes"], 120)
        self.assertEqual(res["title"], "Alice 3D Print Job")

        # Verify in database
        db_row = query_one("SELECT * FROM reservations WHERE id = ?;", (res["id"],))
        self.assertIsNotNone(db_row)
        self.assertEqual(db_row["status"], "confirmed")

        # Verify audit log entry
        audit = query_one("SELECT * FROM audit_log WHERE object_type = 'reservation' AND object_id = ? AND action = 'reservation:create';", (str(res["id"]),))
        self.assertIsNotNone(audit)
        self.assertEqual(audit["actor_name"], self.member_alice["full_name"])

    # -------------------------------------------------------------------------
    # 2. Temporal Qualification Validation on Creation
    # -------------------------------------------------------------------------
    def test_02_temporal_qualification_validation_on_creation(self):
        """Verify that missing or expired qualifications reject reservation with clear errors."""
        today = today_rome_str()
        # Machine 4 is CNC Mill (requires category 3 cnc_mills). Alice does NOT have CNC qualification.
        start_time = f"{today}T14:00:00"
        end_time = f"{today}T16:00:00"

        with self.assertRaises(ReservationConflictError) as ctx:
            create_reservation(
                machine_id=4,
                member_id=self.alice_member_id,
                start_time=start_time,
                end_time=end_time,
                title="CNC Mill Unauthorized Attempt",
                actor_user=self.member_alice,
            )
        self.assertIn("lacks the mandatory qualification", ctx.exception.message)

        # Grant qualification that expires *before* the reservation date
        # Create a new member with expired qualification
        new_mem_id = create_member(
            full_name="Daniel Expired",
            email="daniel.exp@example.com",
            actor_user=self.admin_user,
        )
        # Grant qualification expired yesterday
        yesterday_str = str((now_rome() - datetime.timedelta(days=1)).date())
        grant_qualification(
            member_id=new_mem_id,
            category_id=1,
            qualification_name="FDM Basic (Expired)",
            issue_date="2025-01-01",
            expiry_date=yesterday_str,
            actor_user=self.operator_user,
        )

        with self.assertRaises(ReservationConflictError) as ctx:
            create_reservation(
                machine_id=1,
                member_id=new_mem_id,
                start_time=f"{today}T15:00:00",
                end_time=f"{today}T16:30:00",
                title="Expired Qual Attempt",
                actor_user=self.operator_user,
            )
        self.assertIn("expires", ctx.exception.message.lower())

    # -------------------------------------------------------------------------
    # 3. Operating Hours and Multi-Day Boundaries
    # -------------------------------------------------------------------------
    def test_03_operating_hours_and_timing_rules(self):
        """Reservations outside 08:00-22:00 or across midnight must be rejected."""
        today = today_rome_str()
        # 1. Before opening (07:00 - 09:00)
        with self.assertRaises(ReservationConflictError):
            create_reservation(
                machine_id=1,
                member_id=self.alice_member_id,
                start_time=f"{today}T07:00:00",
                end_time=f"{today}T09:00:00",
                actor_user=self.member_alice,
            )

        # 2. After closing (21:00 - 23:00)
        with self.assertRaises(ReservationConflictError):
            create_reservation(
                machine_id=1,
                member_id=self.alice_member_id,
                start_time=f"{today}T21:00:00",
                end_time=f"{today}T23:00:00",
                actor_user=self.member_alice,
            )

        # 3. Crossing midnight
        tomorrow = str((now_rome() + datetime.timedelta(days=1)).date())
        with self.assertRaises(ReservationConflictError) as ctx:
            create_reservation(
                machine_id=1,
                member_id=self.alice_member_id,
                start_time=f"{today}T21:00:00",
                end_time=f"{tomorrow}T09:00:00",
                actor_user=self.member_alice,
            )
        self.assertIn("multiple calendar days", ctx.exception.message)

        # 4. Less than minimum 15 mins (e.g. 5 min)
        with self.assertRaises(ReservationConflictError) as ctx:
            create_reservation(
                machine_id=1,
                member_id=self.alice_member_id,
                start_time=f"{today}T10:00:00",
                end_time=f"{today}T10:05:00",
                actor_user=self.member_alice,
            )
        self.assertIn("at least 15 minutes", ctx.exception.message)

    # -------------------------------------------------------------------------
    # 4. Capacity and Overlapping Conflicts
    # -------------------------------------------------------------------------
    def test_04_capacity_and_interval_overlaps(self):
        """Test interval overlap rejection on single capacity and sweep-line multi-capacity."""
        # Create a dedicated machine for conflict testing (Capacity 1)
        m_single = create_machine(
            code="TEST-CAP1",
            name="Test Single Cap",
            category_id=1,
            capacity=1,
            actor=self.admin_user,
        )

        today = today_rome_str()
        # Booking A: 13:00 to 15:00
        res_a = create_reservation(
            machine_id=m_single["id"],
            member_id=self.alice_member_id,
            start_time=f"{today}T13:00:00",
            end_time=f"{today}T15:00:00",
            title="Booking A",
            actor_user=self.operator_user,
        )
        self.assertIsNotNone(res_a)

        # Overlapping attempts on m_single:
        # 1. Exact overlap (13:00 - 15:00)
        with self.assertRaises(ReservationConflictError):
            create_reservation(
                machine_id=m_single["id"],
                member_id=self.bob_member_id,
                start_time=f"{today}T13:00:00",
                end_time=f"{today}T15:00:00",
                actor_user=self.operator_user,
            )

        # 2. Left overlap (12:30 - 13:30)
        with self.assertRaises(ReservationConflictError):
            create_reservation(
                machine_id=m_single["id"],
                member_id=self.bob_member_id,
                start_time=f"{today}T12:30:00",
                end_time=f"{today}T13:30:00",
                actor_user=self.operator_user,
            )

        # 3. Right overlap (14:30 - 16:00)
        with self.assertRaises(ReservationConflictError):
            create_reservation(
                machine_id=m_single["id"],
                member_id=self.bob_member_id,
                start_time=f"{today}T14:30:00",
                end_time=f"{today}T16:00:00",
                actor_user=self.operator_user,
            )

        # 4. Enclosing overlap (12:00 - 16:00)
        with self.assertRaises(ReservationConflictError):
            create_reservation(
                machine_id=m_single["id"],
                member_id=self.bob_member_id,
                start_time=f"{today}T12:00:00",
                end_time=f"{today}T16:00:00",
                actor_user=self.operator_user,
            )

        # 5. Contiguous / abutting (11:00 - 13:00 and 15:00 - 17:00) must succeed
        res_before = create_reservation(
            machine_id=m_single["id"],
            member_id=self.bob_member_id,
            start_time=f"{today}T11:00:00",
            end_time=f"{today}T13:00:00",
            title="Abutting Before",
            actor_user=self.operator_user,
        )
        self.assertIsNotNone(res_before)

        res_after = create_reservation(
            machine_id=m_single["id"],
            member_id=self.bob_member_id,
            start_time=f"{today}T15:00:00",
            end_time=f"{today}T17:00:00",
            title="Abutting After",
            actor_user=self.operator_user,
        )
        self.assertIsNotNone(res_after)

    # -------------------------------------------------------------------------
    # 5. Maintenance Windows and Out-of-Service Incidents
    # -------------------------------------------------------------------------
    def test_05_maintenance_windows_blocking(self):
        """Maintenance windows must block conflicting reservations."""
        today = today_rome_str()
        m_maint = create_machine(
            code="TEST-MAINT-1",
            name="Test Maintenance Machine",
            category_id=1,
            capacity=2,
            actor=self.admin_user,
        )

        # Create scheduled maintenance window 14:00 to 17:00
        mw = create_maintenance_window(
            machine_id=m_maint["id"],
            title="Laser Calibration & Lens Cleaning",
            start_time=f"{today}T14:00:00",
            end_time=f"{today}T17:00:00",
            actor=self.operator_user,
        )

        # Booking during maintenance window (15:00 - 16:00) must be rejected
        with self.assertRaises(ReservationConflictError) as ctx:
            create_reservation(
                machine_id=m_maint["id"],
                member_id=self.alice_member_id,
                start_time=f"{today}T15:00:00",
                end_time=f"{today}T16:00:00",
                actor_user=self.operator_user,
            )
        self.assertIn("maintenance window", ctx.exception.message.lower())

        # Booking outside maintenance window (17:00 - 19:00) must succeed
        res_ok = create_reservation(
            machine_id=m_maint["id"],
            member_id=self.alice_member_id,
            start_time=f"{today}T17:00:00",
            end_time=f"{today}T19:00:00",
            actor_user=self.operator_user,
        )
        self.assertIsNotNone(res_ok)

    # -------------------------------------------------------------------------
    # 6. Edit Reservation & Self-Exclusion
    # -------------------------------------------------------------------------
    def test_06_edit_reservation_success_and_self_exclusion(self):
        """Editing an existing reservation should exclude self from conflict checks."""
        today = today_rome_str()
        m_edit = create_machine(
            code="TEST-EDIT-1",
            name="Test Edit Machine",
            category_id=1,
            capacity=1,
            actor=self.admin_user,
        )

        res = create_reservation(
            machine_id=m_edit["id"],
            member_id=self.alice_member_id,
            start_time=f"{today}T10:00:00",
            end_time=f"{today}T12:00:00",
            title="Original Title",
            actor_user=self.member_alice,
        )

        # 1. Update title and extend by 30 mins (10:00 - 12:30) on same machine
        # Self-exclusion ensures the 10:00-12:00 portion does not conflict with itself!
        updated = update_reservation(
            reservation_id=res["id"],
            title="Updated Title 3D",
            start_time=f"{today}T10:00:00",
            end_time=f"{today}T12:30:00",
            actor_user=self.member_alice,
        )

        self.assertEqual(updated["title"], "Updated Title 3D")
        self.assertEqual(updated["duration_minutes"], 150)
        self.assertTrue(updated["end_time"].startswith(f"{today}T12:30:00"))

        # Verify audit log for update
        audit_upd = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'reservation' AND object_id = ? AND action = 'reservation:update';",
            (str(res["id"]),),
        )
        self.assertIsNotNone(audit_upd)

    # -------------------------------------------------------------------------
    # 7. Edit Reservation Conflict Rejection
    # -------------------------------------------------------------------------
    def test_07_edit_reservation_conflict_rejected(self):
        """Updating a reservation to overlap with another reservation must be rejected."""
        today = today_rome_str()
        m_test = create_machine(
            code="TEST-EDIT-CONF",
            name="Test Edit Conflict Machine",
            category_id=1,
            capacity=1,
            actor=self.admin_user,
        )

        # Res 1: 10:00 - 12:00
        res1 = create_reservation(
            machine_id=m_test["id"],
            member_id=self.alice_member_id,
            start_time=f"{today}T10:00:00",
            end_time=f"{today}T12:00:00",
            actor_user=self.operator_user,
        )
        # Res 2: 14:00 - 16:00
        res2 = create_reservation(
            machine_id=m_test["id"],
            member_id=self.bob_member_id,
            start_time=f"{today}T14:00:00",
            end_time=f"{today}T16:00:00",
            actor_user=self.operator_user,
        )

        # Try to edit Res 2 to move to 11:00 - 13:00 (overlaps with Res 1)
        with self.assertRaises(ReservationConflictError):
            update_reservation(
                reservation_id=res2["id"],
                start_time=f"{today}T11:00:00",
                end_time=f"{today}T13:00:00",
                actor_user=self.operator_user,
            )

        # Res 2 timing should remain untouched in DB
        res2_db = get_reservation_by_id(res2["id"])
        self.assertTrue(res2_db["start_time"].startswith(f"{today}T14:00:00"))

    # -------------------------------------------------------------------------
    # 8. Cancel Reservation & Slot Release
    # -------------------------------------------------------------------------
    def test_08_cancel_reservation_releases_capacity(self):
        """Cancelling a reservation releases the slot immediately for new bookings."""
        today = today_rome_str()
        m_cancel = create_machine(
            code="TEST-CANCEL-1",
            name="Test Cancel Machine",
            category_id=1,
            capacity=1,
            actor=self.admin_user,
        )

        res = create_reservation(
            machine_id=m_cancel["id"],
            member_id=self.alice_member_id,
            start_time=f"{today}T14:00:00",
            end_time=f"{today}T16:00:00",
            title="Will Be Cancelled",
            actor_user=self.member_alice,
        )

        # Bob cannot book while Alice's reservation is active
        with self.assertRaises(ReservationConflictError):
            create_reservation(
                machine_id=m_cancel["id"],
                member_id=self.bob_member_id,
                start_time=f"{today}T14:00:00",
                end_time=f"{today}T16:00:00",
                actor_user=self.operator_user,
            )

        # Alice cancels her reservation
        cancelled = cancel_reservation(
            reservation_id=res["id"],
            reason="Project changed, releasing machine",
            actor_user=self.member_alice,
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNotNone(cancelled["cancelled_at"])
        self.assertEqual(cancelled["cancellation_reason"], "Project changed, releasing machine")

        # Now Bob can successfully book the exact same slot!
        bob_res = create_reservation(
            machine_id=m_cancel["id"],
            member_id=self.bob_member_id,
            start_time=f"{today}T14:00:00",
            end_time=f"{today}T16:00:00",
            title="Bob Takes Released Slot",
            actor_user=self.operator_user,
        )
        self.assertIsNotNone(bob_res)
        self.assertEqual(bob_res["status"], "confirmed")

        # Attempting to edit or cancel the cancelled reservation raises an error
        with self.assertRaises(ValueError):
            cancel_reservation(res["id"], actor_user=self.member_alice)

        with self.assertRaises(ValueError):
            update_reservation(res["id"], title="New Title", actor_user=self.member_alice)

    # -------------------------------------------------------------------------
    # 9. Estimation of Usage Charges
    # -------------------------------------------------------------------------
    def test_09_estimate_reservation_charges(self):
        """Verify standard, peak rate, and minimum charge calculations."""
        today = today_rome_str()
        # Machine 2 is Bambu Lab: hourly_rate=300 (€3.00), peak=400 (€4.00), min=150 (€1.50), peak_hours 17:00-21:00
        # 1. 1-hour off-peak booking (10:00 - 11:00) -> 300 cents (€3.00)
        est1 = estimate_reservation_charge(machine_id=2, start_time=f"{today}T10:00:00", end_time=f"{today}T11:00:00")
        self.assertEqual(est1["estimated_total_cents"], 300)

        # 2. 1-hour peak booking (18:00 - 19:00) -> 400 cents (€4.00)
        est2 = estimate_reservation_charge(machine_id=2, start_time=f"{today}T18:00:00", end_time=f"{today}T19:00:00")
        self.assertEqual(est2["estimated_total_cents"], 400)

        # 3. 15-minute off-peak booking (10:00 - 10:15) -> calculated: 300/4 = 75 cents -> minimum charge 150 cents
        est3 = estimate_reservation_charge(machine_id=2, start_time=f"{today}T10:00:00", end_time=f"{today}T10:15:00")
        self.assertEqual(est3["estimated_total_cents"], 150)

    # -------------------------------------------------------------------------
    # 10. Role-Based Access Control (RBAC) Enforcement
    # -------------------------------------------------------------------------
    def test_10_rbac_rules_for_reservations(self):
        """Verify role permissions for Admin, Operator, Member, and Viewer."""
        today = today_rome_str()
        # 1. Viewer cannot create reservation (raises PermissionError)
        with self.assertRaises(ReservationPermissionError):
            create_reservation(
                machine_id=1,
                member_id=self.alice_member_id,
                start_time=f"{today}T18:00:00",
                end_time=f"{today}T19:00:00",
                actor_user=self.viewer_user,
            )

        # 2. Alice (Member) cannot book for Bob (Member)
        with self.assertRaises(ReservationPermissionError):
            create_reservation(
                machine_id=1,
                member_id=self.bob_member_id,
                start_time=f"{today}T18:00:00",
                end_time=f"{today}T19:00:00",
                actor_user=self.member_alice,
            )

        # 3. Alice creates her own reservation
        alice_res = create_reservation(
            machine_id=1,
            member_id=self.alice_member_id,
            start_time=f"{today}T18:00:00",
            end_time=f"{today}T19:00:00",
            actor_user=self.member_alice,
        )

        # 4. Bob cannot edit or cancel Alice's reservation
        with self.assertRaises(ReservationPermissionError):
            update_reservation(
                reservation_id=alice_res["id"],
                title="Hacked by Bob",
                actor_user=self.member_bob,
            )

        with self.assertRaises(ReservationPermissionError):
            cancel_reservation(
                reservation_id=alice_res["id"],
                reason="Malicious cancel",
                actor_user=self.member_bob,
            )

        # 5. Operator and Admin CAN edit/cancel Alice's reservation
        op_updated = update_reservation(
            reservation_id=alice_res["id"],
            title="Operator Adjusted Title",
            actor_user=self.operator_user,
        )
        self.assertEqual(op_updated["title"], "Operator Adjusted Title")

        admin_cancel = cancel_reservation(
            reservation_id=alice_res["id"],
            reason="Admin maintenance override",
            actor_user=self.admin_user,
        )
        self.assertEqual(admin_cancel["status"], "cancelled")

    # -------------------------------------------------------------------------
    # 11. REST API CRUD Operations
    # -------------------------------------------------------------------------
    def test_11_rest_api_crud_operations(self):
        """Test full REST API lifecycle (/api/reservations) with status codes and payloads."""
        future_date = str((now_rome() + datetime.timedelta(days=2)).date())
        start_time = f"{future_date}T10:00:00"
        end_time = f"{future_date}T11:30:00"

        # 1. Member POST /api/reservations (auto-binds member_id)
        resp = self._make_request(
            "POST",
            "/api/reservations",
            bearer_token=self.alice_session,
            body={
                "machine_id": 1,
                "start_time": start_time,
                "end_time": end_time,
                "title": "API Created Booking",
            },
        )
        self.assertEqual(resp.status_code, 201)
        data = json.loads(resp.body)
        self.assertTrue(data["success"])
        res_id = data["reservation"]["id"]
        self.assertEqual(data["reservation"]["status"], "confirmed")

        # 2. GET /api/reservations
        resp_list = self._make_request(
            "GET",
            "/api/reservations",
            bearer_token=self.alice_session,
            query_params={"machine_id": "1"},
        )
        self.assertEqual(resp_list.status_code, 200)
        list_data = json.loads(resp_list.body)
        self.assertTrue(any(r["id"] == res_id for r in list_data["reservations"]))

        # 3. GET /api/reservations/{id}
        resp_get = self._make_request(
            "GET",
            f"/api/reservations/{res_id}",
            bearer_token=self.alice_session,
        )
        self.assertEqual(resp_get.status_code, 200)
        get_data = json.loads(resp_get.body)
        self.assertEqual(get_data["reservation"]["title"], "API Created Booking")

        # 4. PUT /api/reservations/{id}
        resp_put = self._make_request(
            "PUT",
            f"/api/reservations/{res_id}",
            bearer_token=self.alice_session,
            body={
                "title": "API Updated Booking Title",
                "start_time": f"{future_date}T10:00:00",
                "end_time": f"{future_date}T12:00:00",
            },
        )
        self.assertEqual(resp_put.status_code, 200)
        put_data = json.loads(resp_put.body)
        self.assertEqual(put_data["reservation"]["title"], "API Updated Booking Title")
        self.assertEqual(put_data["reservation"]["duration_minutes"], 120)

        # 5. POST /api/reservations with conflict returns 409
        resp_conf = self._make_request(
            "POST",
            "/api/reservations",
            bearer_token=self.operator_session,
            body={
                "machine_id": 1,
                "member_id": self.bob_member_id,
                "start_time": f"{future_date}T10:30:00",
                "end_time": f"{future_date}T11:30:00",
                "title": "Conflicting API Booking",
            },
        )
        self.assertEqual(resp_conf.status_code, 409)
        conf_data = json.loads(resp_conf.body)
        self.assertEqual(conf_data["code"], "reservation_conflict")

        # 6. POST /api/reservations/{id}/cancel
        resp_cancel = self._make_request(
            "POST",
            f"/api/reservations/{res_id}/cancel",
            bearer_token=self.alice_session,
            body={"reason": "Cancelled via REST API test"},
        )
        self.assertEqual(resp_cancel.status_code, 200)
        cancel_data = json.loads(resp_cancel.body)
        self.assertEqual(cancel_data["reservation"]["status"], "cancelled")

    # -------------------------------------------------------------------------
    # 12. HTML Views & Form Value Preservation
    # -------------------------------------------------------------------------
    def test_12_html_views_and_form_preservation(self):
        """Test HTML view rendering, form error alerts, and field value preservation."""
        today = today_rome_str()

        # 1. GET /reservations (calendar / list view)
        resp_list = self._make_request(
            "GET",
            "/reservations",
            bearer_token=self.alice_session,
        )
        self.assertEqual(resp_list.status_code, 200)
        self.assertIn("Reservations &amp; Workshop Schedule", resp_list.body.decode("utf-8"))

        # 2. GET /reservations/new
        resp_new = self._make_request(
            "GET",
            "/reservations/new",
            bearer_token=self.operator_session,
            query_params={"date": today, "title": "Prefilled Project"},
        )
        self.assertEqual(resp_new.status_code, 200)
        body_text = resp_new.body.decode("utf-8")
        self.assertIn("Create Machine Reservation", body_text)
        self.assertIn("Prefilled Project", body_text)

        # 3. POST /reservations/new with conflict preserves entered values and displays explanation
        block_res = create_reservation(
            machine_id=1,
            member_id=self.alice_member_id,
            start_time=f"{today}T19:00:00",
            end_time=f"{today}T21:00:00",
            title="Blocking Slot",
            actor_user=self.operator_user,
        )

        resp_fail = self._make_request(
            "POST",
            "/reservations/new",
            bearer_token=self.operator_session,
            content_type="application/x-www-form-urlencoded",
            body={
                "machine_id": "1",
                "member_id": str(self.bob_member_id),
                "date": today,
                "start_time": "19:30",
                "end_time": "20:30",
                "title": "My Custom Form Value To Preserve",
            },
        )
        self.assertEqual(resp_fail.status_code, 200)
        fail_html = resp_fail.body.decode("utf-8")
        self.assertIn("Booking Rejected: Conflict Detected", fail_html)
        self.assertIn("My Custom Form Value To Preserve", fail_html)
        self.assertIn("19:30", fail_html)
        self.assertIn("20:30", fail_html)

        # 4. GET /reservations/{id} detail view
        resp_detail = self._make_request(
            "GET",
            f"/reservations/{block_res['id']}",
            bearer_token=self.operator_session,
        )
        self.assertEqual(resp_detail.status_code, 200)
        detail_html = resp_detail.body.decode("utf-8")
        self.assertIn("Blocking Slot", detail_html)
        self.assertIn("Estimated Pricing", detail_html)
        self.assertIn("Schedule &amp; Timing", detail_html)


if __name__ == "__main__":
    unittest.main()
