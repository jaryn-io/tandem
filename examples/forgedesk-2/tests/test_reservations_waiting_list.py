"""Comprehensive Test Suite for ForgeDesk Waiting List, Eligibility & Atomic Promotion (Step S14)."""

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
from forgedesk.members.service import create_member, grant_qualification
from forgedesk.reservations.service import (
    ReservationConflictError,
    cancel_reservation,
    create_reservation,
    get_reservation_by_id,
)
from forgedesk.reservations.waiting_list import (
    WaitingListEligibilityError,
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
from forgedesk.utils.datetime_tz import format_iso, now_rome, now_rome_iso, parse_datetime, today_rome_str


class TestReservationsWaitingList(unittest.TestCase):
    """Test suite for Step S14 waiting list lifecycle, temporal eligibility, and atomic promotion."""

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
    # 1. Waiting List Entry Creation & RBAC
    # -------------------------------------------------------------------------
    def test_01_create_waiting_list_entry_success(self):
        """Test that qualified members can join the waiting list for a future slot."""
        today = today_rome_str()
        # Machine 1 is 3D printer (Alice is qualified for 3D printers)
        start_time = f"{today}T14:00:00"
        end_time = f"{today}T16:00:00"

        entry = create_waiting_list_entry(
            machine_id=1,
            member_id=self.alice_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.member_alice,
        )

        self.assertIsNotNone(entry)
        self.assertEqual(entry["machine_id"], 1)
        self.assertEqual(entry["member_id"], self.alice_member["id"])
        self.assertEqual(entry["status"], "waiting")
        self.assertIsNone(entry["promoted_reservation_id"])
        self.assertEqual(entry["duration_minutes"], 120)

        # Verify audit log entry
        audit = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'waiting_list' AND object_id = ? AND action = 'waiting_list:join';",
            (str(entry["id"]),),
        )
        self.assertIsNotNone(audit)

    def test_02_member_cannot_add_other_member_to_waiting_list(self):
        """Members must not be allowed to place other members on the waiting list."""
        today = today_rome_str()
        start_time = f"{today}T16:00:00"
        end_time = f"{today}T18:00:00"

        with self.assertRaises(WaitingListPermissionError):
            create_waiting_list_entry(
                machine_id=1,
                member_id=self.bob_member["id"],  # Alice attempting to add Bob
                desired_start_time=start_time,
                desired_end_time=end_time,
                actor_user=self.member_alice,
            )

    def test_03_staff_can_add_any_member_to_waiting_list(self):
        """Administrators and Operators can place any qualified member on the waiting list."""
        today = today_rome_str()
        start_time = f"{today}T16:00:00"
        end_time = f"{today}T18:00:00"

        entry = create_waiting_list_entry(
            machine_id=1,
            member_id=self.bob_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.operator_user,
        )

        self.assertIsNotNone(entry)
        self.assertEqual(entry["member_id"], self.bob_member["id"])
        self.assertEqual(entry["status"], "waiting")

    def test_04_viewer_cannot_join_waiting_list(self):
        """Viewers have read-only access and cannot join waiting list."""
        today = today_rome_str()
        start_time = f"{today}T18:00:00"
        end_time = f"{today}T20:00:00"

        with self.assertRaises(WaitingListPermissionError):
            create_waiting_list_entry(
                machine_id=1,
                member_id=self.alice_member["id"],
                desired_start_time=start_time,
                desired_end_time=end_time,
                actor_user=self.viewer_user,
            )

    # -------------------------------------------------------------------------
    # 2. Temporal Qualification Validation on Waiting List
    # -------------------------------------------------------------------------
    def test_05_unqualified_member_rejected_from_waiting_list(self):
        """A member without required qualifications must be rejected from joining the waiting list."""
        today = today_rome_str()
        # Machine 4 is Haas Mini Mill (requires CNC category). Alice is not CNC certified.
        start_time = f"{today}T10:00:00"
        end_time = f"{today}T12:00:00"

        with self.assertRaises(WaitingListEligibilityError) as ctx:
            create_waiting_list_entry(
                machine_id=4,
                member_id=self.alice_member["id"],
                desired_start_time=start_time,
                desired_end_time=end_time,
                actor_user=self.member_alice,
            )
        self.assertIn("qualification", str(ctx.exception).lower())

    def test_06_expired_qualification_rejected_from_waiting_list(self):
        """A member whose qualification is expired at the desired date cannot join the waiting list."""
        today = today_rome_str()
        # Bob has an expired CNC qualification (expired 2026-07-15)
        start_time = f"{today}T10:00:00"
        end_time = f"{today}T12:00:00"

        with self.assertRaises(WaitingListEligibilityError) as ctx:
            create_waiting_list_entry(
                machine_id=4,
                member_id=self.bob_member["id"],
                desired_start_time=start_time,
                desired_end_time=end_time,
                actor_user=self.member_bob,
            )
        self.assertTrue("expire" in str(ctx.exception).lower())

    def test_07_duplicate_waiting_list_entry_rejected(self):
        """Duplicate waiting list requests for the same member and overlapping interval must be rejected."""
        today = today_rome_str()
        start_time = f"{today}T10:00:00"
        end_time = f"{today}T12:00:00"

        # Clara joins waiting list for Epilog Laser Cutter (Machine 3)
        entry1 = create_waiting_list_entry(
            machine_id=3,
            member_id=self.clara_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.member_clara,
        )
        self.assertIsNotNone(entry1)

        # Attempt duplicate entry
        with self.assertRaises(ValueError) as ctx:
            create_waiting_list_entry(
                machine_id=3,
                member_id=self.clara_member["id"],
                desired_start_time=f"{today}T11:00:00",  # Overlapping
                desired_end_time=f"{today}T13:00:00",
                actor_user=self.member_clara,
            )
        self.assertIn("already on the waiting list", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 3. Waiting List Entry Cancellation
    # -------------------------------------------------------------------------
    def test_08_cancel_waiting_list_entry_success(self):
        """Members can cancel their own waiting list entries."""
        today = today_rome_str()
        start_time = f"{today}T12:00:00"
        end_time = f"{today}T14:00:00"

        entry = create_waiting_list_entry(
            machine_id=3,
            member_id=self.clara_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.member_clara,
        )

        cancelled = cancel_waiting_list_entry(
            entry_id=entry["id"],
            reason="Plans changed",
            actor_user=self.member_clara,
        )

        self.assertEqual(cancelled["status"], "cancelled")
        db_entry = get_waiting_list_entry_by_id(entry["id"])
        self.assertEqual(db_entry["status"], "cancelled")

        # Verify audit log
        audit = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'waiting_list' AND object_id = ? AND action = 'waiting_list:cancel';",
            (str(entry["id"]),),
        )
        self.assertIsNotNone(audit)

    # -------------------------------------------------------------------------
    # 4. Atomic Promotion on Reservation Cancellation (Core Requirement)
    # -------------------------------------------------------------------------
    def test_09_atomic_promotion_on_reservation_cancellation(self):
        """When a reservation is cancelled, the first eligible waiting-list entry is promoted atomically."""
        tomorrow = (now_rome().date() + datetime.timedelta(days=2)).isoformat()
        start_time = f"{tomorrow}T10:00:00"
        end_time = f"{tomorrow}T12:00:00"

        # 1. Create a confirmed reservation for Alice on Machine 3 (Epilog Laser Cutter, capacity=1)
        alice_res = create_reservation(
            machine_id=3,
            member_id=self.alice_member["id"],
            start_time=start_time,
            end_time=end_time,
            title="Alice Laser Slot",
            actor_user=self.member_alice,
        )
        self.assertEqual(alice_res["status"], "confirmed")

        # 2. Clara (qualified for laser cutters) joins waiting list for that exact slot
        clara_wl = create_waiting_list_entry(
            machine_id=3,
            member_id=self.clara_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.member_clara,
        )
        self.assertEqual(clara_wl["status"], "waiting")
        self.assertIsNone(clara_wl["promoted_reservation_id"])

        # 3. Alice cancels her reservation
        cancel_result = cancel_reservation(
            reservation_id=alice_res["id"],
            reason="Alice unable to attend",
            actor_user=self.member_alice,
        )
        self.assertEqual(cancel_result["status"], "cancelled")

        # 4. Verify Clara's waiting list entry was promoted atomically
        promoted_info = cancel_result.get("promoted_waiting_list_entry")
        self.assertIsNotNone(promoted_info)
        self.assertEqual(promoted_info["promoted_entry_id"], clara_wl["id"])
        self.assertEqual(promoted_info["member_id"], self.clara_member["id"])

        # Check waiting list database state
        updated_wl = get_waiting_list_entry_by_id(clara_wl["id"])
        self.assertEqual(updated_wl["status"], "promoted")
        self.assertIsNotNone(updated_wl["promoted_reservation_id"])

        # Check newly created reservation for Clara
        new_res = get_reservation_by_id(updated_wl["promoted_reservation_id"])
        self.assertIsNotNone(new_res)
        self.assertEqual(new_res["member_id"], self.clara_member["id"])
        self.assertEqual(new_res["machine_id"], 3)
        self.assertEqual(new_res["status"], "confirmed")
        self.assertTrue(new_res["start_time"].startswith(start_time))
        self.assertTrue(new_res["end_time"].startswith(end_time))

        # Verify audit logs for promotion
        wl_audit = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'waiting_list' AND object_id = ? AND action = 'waiting_list:promoted';",
            (str(clara_wl["id"]),),
        )
        self.assertIsNotNone(wl_audit)

        res_audit = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'reservation' AND object_id = ? AND action = 'reservation:create_from_waiting_list';",
            (str(new_res["id"]),),
        )
        self.assertIsNotNone(res_audit)

    def test_10_fifo_order_priority_on_promotion(self):
        """When multiple eligible candidates are waiting, the earliest created entry (FIFO) is promoted."""
        slot_date = (now_rome().date() + datetime.timedelta(days=3)).isoformat()
        start_time = f"{slot_date}T14:00:00"
        end_time = f"{slot_date}T16:00:00"

        # 1. Create confirmed booking for Alice on 3D Printer (Machine 1)
        res1 = create_reservation(
            machine_id=1,
            member_id=self.alice_member["id"],
            start_time=start_time,
            end_time=end_time,
            title="Alice 3D Print",
            actor_user=self.member_alice,
        )

        # 2. Bob (qualified for 3D printer) joins waiting list first
        wl_bob = create_waiting_list_entry(
            machine_id=1,
            member_id=self.bob_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.member_bob,
        )

        # Grant Clara 3D printer qualification so she is also eligible
        grant_qualification(
            member_id=self.clara_member["id"],
            category_id=1,
            qualification_name="Standard 3D Printing",
            issue_date="2026-01-01",
            expiry_date=None,
            verified_by_user_id=self.admin_user["id"],
            actor_user=self.admin_user,
        )

        # 3. Clara joins waiting list second
        wl_clara = create_waiting_list_entry(
            machine_id=1,
            member_id=self.clara_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.member_clara,
        )

        # 4. Cancel Alice's reservation
        cancel_res = cancel_reservation(
            reservation_id=res1["id"],
            reason="Cancelled by Alice",
            actor_user=self.member_alice,
        )

        # 5. Bob must be promoted (earliest in FIFO queue)
        promoted = cancel_res.get("promoted_waiting_list_entry")
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted["promoted_entry_id"], wl_bob["id"])
        self.assertEqual(promoted["member_id"], self.bob_member["id"])

        # Check Bob's status is 'promoted' and Clara's status remains 'waiting'
        bob_db = get_waiting_list_entry_by_id(wl_bob["id"])
        clara_db = get_waiting_list_entry_by_id(wl_clara["id"])
        self.assertEqual(bob_db["status"], "promoted")
        self.assertEqual(clara_db["status"], "waiting")

    def test_11_ineligible_candidate_skipped_during_promotion(self):
        """A candidate whose qualification expired between joining the waiting list and cancellation is skipped."""
        slot_date = (now_rome().date() + datetime.timedelta(days=4)).isoformat()
        start_time = f"{slot_date}T10:00:00"
        end_time = f"{slot_date}T12:00:00"

        # Create temporary machine category and machine
        conn = get_connection()
        from forgedesk.db.connection import transaction
        with transaction(conn) as tx:
            cur = tx.execute("INSERT INTO machine_categories (code, name, created_at) VALUES ('special_lab', 'Special Lab', ?);", (now_rome_iso(),))
            cat_id = cur.lastrowid
            cur_m = tx.execute(
                """
                INSERT INTO machines (code, name, category_id, required_qualification_category_id, capacity, state, created_at, updated_at)
                VALUES ('SPECIAL-01', 'Special Machine', ?, ?, 1, 'available', ?, ?);
                """,
                (cat_id, cat_id, now_rome_iso(), now_rome_iso()),
            )
            spec_machine_id = cur_m.lastrowid

            # Give Alice qualification valid for slot
            tx.execute(
                """
                INSERT INTO qualifications (member_id, category_id, qualification_name, issue_date, expiry_date, created_at)
                VALUES (?, ?, 'Special Lab Cert', '2026-01-01', '2027-01-01', ?);
                """,
                (self.alice_member["id"], cat_id, now_rome_iso()),
            )
            # Give Bob qualification that expires BEFORE slot_date
            tx.execute(
                """
                INSERT INTO qualifications (member_id, category_id, qualification_name, issue_date, expiry_date, created_at)
                VALUES (?, ?, 'Special Lab Cert', '2026-01-01', '2026-01-10', ?);
                """,
                (self.bob_member["id"], cat_id, now_rome_iso()),
            )
            # Give Clara qualification valid for slot
            tx.execute(
                """
                INSERT INTO qualifications (member_id, category_id, qualification_name, issue_date, expiry_date, created_at)
                VALUES (?, ?, 'Special Lab Cert', '2026-01-01', '2027-01-01', ?);
                """,
                (self.clara_member["id"], cat_id, now_rome_iso()),
            )

        # 1. Create reservation for Alice
        res = create_reservation(
            machine_id=spec_machine_id,
            member_id=self.alice_member["id"],
            start_time=start_time,
            end_time=end_time,
            title="Alice Special Lab",
            actor_user=self.admin_user,
        )

        # 2. Insert Bob directly into waiting_list (simulating qualification expired after queueing)
        with transaction(conn) as tx:
            cur_wb = tx.execute(
                """
                INSERT INTO waiting_list (machine_id, member_id, desired_start_time, desired_end_time, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'waiting', ?, ?);
                """,
                (spec_machine_id, self.bob_member["id"], start_time, end_time, now_rome_iso(), now_rome_iso()),
            )
            bob_wl_id = cur_wb.lastrowid

        # 3. Clara joins waiting list after Bob
        clara_wl = create_waiting_list_entry(
            machine_id=spec_machine_id,
            member_id=self.clara_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.admin_user,
        )

        # 4. Cancel Alice's reservation
        cancel_res = cancel_reservation(
            reservation_id=res["id"],
            reason="Alice cancelled",
            actor_user=self.admin_user,
        )

        # 5. Bob should be skipped due to expired qualification, and Clara should be promoted!
        promoted = cancel_res.get("promoted_waiting_list_entry")
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted["promoted_entry_id"], clara_wl["id"])
        self.assertEqual(promoted["member_id"], self.clara_member["id"])

        bob_db = get_waiting_list_entry_by_id(bob_wl_id)
        self.assertEqual(bob_db["status"], "waiting")  # Skipped, remains waiting

    # -------------------------------------------------------------------------
    # 5. Manual Promotion by Admin / Operator
    # -------------------------------------------------------------------------
    def test_12_manual_promotion_by_operator(self):
        """Operators and Admins can manually promote an eligible waiting list entry."""
        slot_date = (now_rome().date() + datetime.timedelta(days=5)).isoformat()
        start_time = f"{slot_date}T10:00:00"
        end_time = f"{slot_date}T12:00:00"

        # Alice joins waiting list for an open slot on Machine 1
        entry = create_waiting_list_entry(
            machine_id=1,
            member_id=self.alice_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.member_alice,
        )

        # Operator manually promotes entry
        result = promote_waiting_list_entry_by_id(
            entry_id=entry["id"],
            actor_user=self.operator_user,
        )

        self.assertTrue(result["success"])
        self.assertIsNotNone(result["reservation_id"])

        db_entry = get_waiting_list_entry_by_id(entry["id"])
        self.assertEqual(db_entry["status"], "promoted")
        self.assertEqual(db_entry["promoted_reservation_id"], result["reservation_id"])

    def test_13_member_cannot_manually_promote_entry(self):
        """Regular members cannot manually promote waiting list entries."""
        slot_date = (now_rome().date() + datetime.timedelta(days=5)).isoformat()
        start_time = f"{slot_date}T14:00:00"
        end_time = f"{slot_date}T16:00:00"

        entry = create_waiting_list_entry(
            machine_id=1,
            member_id=self.alice_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.member_alice,
        )

        with self.assertRaises(WaitingListPermissionError):
            promote_waiting_list_entry_by_id(
                entry_id=entry["id"],
                actor_user=self.member_alice,
            )

    # -------------------------------------------------------------------------
    # 6. REST API Endpoints Testing
    # -------------------------------------------------------------------------
    def test_14_api_waiting_list_collection(self):
        """Test GET /api/waiting-list and POST /api/waiting-list REST endpoints."""
        slot_date = (now_rome().date() + datetime.timedelta(days=6)).isoformat()
        start_time = f"{slot_date}T09:00:00"
        end_time = f"{slot_date}T11:00:00"

        # POST /api/waiting-list with Alice's Bearer token
        resp = self._make_request(
            method="POST",
            path="/api/waiting-list",
            bearer_token=self.alice_session,
            body={
                "machine_id": 1,
                "desired_start_time": start_time,
                "desired_end_time": end_time,
            },
        )
        self.assertEqual(resp.status_code, 201)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(data.get("success"))
        entry_id = data["entry"]["id"]

        # GET /api/waiting-list
        get_resp = self._make_request(
            method="GET",
            path="/api/waiting-list",
            bearer_token=self.alice_session,
            query_params={"machine_id": "1", "status": "waiting"},
        )
        self.assertEqual(get_resp.status_code, 200)
        get_data = json.loads(get_resp.body.decode("utf-8"))
        self.assertTrue(get_data.get("success"))
        self.assertGreaterEqual(get_data.get("count", 0), 1)

        # GET /api/waiting-list/{id}
        single_resp = self._make_request(
            method="GET",
            path=f"/api/waiting-list/{entry_id}",
            bearer_token=self.alice_session,
        )
        self.assertEqual(single_resp.status_code, 200)
        single_data = json.loads(single_resp.body.decode("utf-8"))
        self.assertEqual(single_data["entry"]["id"], entry_id)

        # POST /api/waiting-list/{id}/cancel
        cancel_resp = self._make_request(
            method="POST",
            path=f"/api/waiting-list/{entry_id}/cancel",
            bearer_token=self.alice_session,
            body={"reason": "Cancelled via API test"},
        )
        self.assertEqual(cancel_resp.status_code, 200)
        cancel_data = json.loads(cancel_resp.body.decode("utf-8"))
        self.assertEqual(cancel_data["entry"]["status"], "cancelled")

    def test_15_api_waiting_list_eligibility_endpoint(self):
        """Test GET /api/waiting-list/{id}/eligibility endpoint."""
        slot_date = (now_rome().date() + datetime.timedelta(days=7)).isoformat()
        start_time = f"{slot_date}T09:00:00"
        end_time = f"{slot_date}T11:00:00"

        entry = create_waiting_list_entry(
            machine_id=1,
            member_id=self.alice_member["id"],
            desired_start_time=start_time,
            desired_end_time=end_time,
            actor_user=self.member_alice,
        )

        resp = self._make_request(
            method="GET",
            path=f"/api/waiting-list/{entry['id']}/eligibility",
            bearer_token=self.admin_session,
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(data.get("success"))
        self.assertTrue(data.get("is_eligible"))

    # -------------------------------------------------------------------------
    # 7. HTML Views & Form Workflows
    # -------------------------------------------------------------------------
    def test_16_html_waiting_list_views(self):
        """Test HTML view rendering for /waiting-list and /waiting-list/new."""
        # GET /waiting-list with cookie auth
        resp = self._make_request(
            method="GET",
            path="/waiting-list",
            bearer_token=self.operator_session,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8")
        self.assertIn("Machine Waiting Lists", html)
        self.assertIn("Active Queue", html)

        # GET /waiting-list/new
        resp_new = self._make_request(
            method="GET",
            path="/waiting-list/new",
            bearer_token=self.operator_session,
        )
        self.assertEqual(resp_new.status_code, 200)
        html_new = resp_new.body.decode("utf-8")
        self.assertIn("Join Machine Waiting List", html_new)


if __name__ == "__main__":
    unittest.main()
