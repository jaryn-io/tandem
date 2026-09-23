"""Comprehensive test suite for ForgeDesk Availability, Interval Concurrency, Capacity, and Historical Preservation (Step S11)."""

import datetime
import json
import unittest
from typing import Any, Dict, Optional

from app import create_application_router
from forgedesk.auth.service import authenticate_user, create_user_session
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.db.connection import get_connection, query_all, query_one, transaction
from forgedesk.db.migrations import apply_migrations, reset_database
from forgedesk.db.seed import seed_database
from forgedesk.machines.service import (
    change_machine_state,
    create_machine,
    create_maintenance_window,
    delete_machine,
    get_machine_by_code,
    retire_machine,
)
from forgedesk.members.service import (
    create_member,
    grant_qualification,
    list_machine_categories,
    list_members,
    update_member,
)
from forgedesk.reservations.availability import (
    calculate_concurrent_utilization,
    check_machine_availability,
    find_interval_conflicts,
    generate_timeline_slots,
    get_batch_machine_availability,
    get_historical_reservations_for_machine,
    validate_member_reservation_eligibility,
    validate_reservation_timing,
)
from forgedesk.reservations.handlers import register_availability_routes
from forgedesk.utils.datetime_tz import (
    format_iso,
    now_rome,
    now_rome_iso,
    parse_datetime,
    today_rome_str,
)


class TestAvailabilityAndConflicts(unittest.TestCase):
    """Test suite for Step S11 availability engine, capacity, conflict detection, and historical preservation."""

    @classmethod
    def setUpClass(cls):
        """Reset and seed database once for test class."""
        reset_database()
        apply_migrations()
        seed_database(force=True)

        # Set up test router with full middleware and availability routes
        cls.router = create_application_router()

        # Authenticate sessions for each role
        cls.admin_user = authenticate_user("admin", "admin123")
        cls.admin_session = create_user_session(cls.admin_user["id"])

        cls.operator_user = authenticate_user("operator", "operator123")
        cls.operator_session = create_user_session(cls.operator_user["id"])

        cls.member_user = authenticate_user("member_alice", "member123")
        cls.member_session = create_user_session(cls.member_user["id"])

        cls.viewer_user = authenticate_user("viewer", "viewer123")
        cls.viewer_session = create_user_session(cls.viewer_user["id"])

    def _make_request(
        self,
        method: str,
        path: str,
        bearer_token: Optional[str] = None,
        query_params: Optional[Dict[str, str]] = None,
    ) -> Response:
        headers = {}
        if bearer_token:
            headers["authorization"] = f"Bearer {bearer_token}"
        full_path = path
        if query_params:
            from urllib.parse import urlencode
            full_path = f"{path}?{urlencode(query_params)}"
        req = Request(
            method=method,
            path=full_path,
            headers=headers,
            client_address=("127.0.0.1", 50000),
        )
        return self.router.dispatch(req)

    def test_01_operating_hours_boundaries(self):
        """Test daily operating hours validation in Europe/Rome."""
        now = now_rome()
        target_day = (now + datetime.timedelta(days=5)).date()

        # A. Normal valid operating hours (10:00 to 12:00 within 08:00 - 22:00)
        s1 = f"{target_day} 10:00:00"
        e1 = f"{target_day} 12:00:00"
        valid, msg, details = validate_reservation_timing(s1, e1, "08:00", "22:00")
        self.assertTrue(valid)
        self.assertEqual(details["duration_minutes"], 120)

        # B. Starts before operating hours (07:00 to 09:00)
        s2 = f"{target_day} 07:00:00"
        e2 = f"{target_day} 09:00:00"
        valid, msg, details = validate_reservation_timing(s2, e2, "08:00", "22:00")
        self.assertFalse(valid)
        self.assertEqual(details["error_type"], "operating_hours")

        # C. Ends after operating hours (21:00 to 22:30)
        s3 = f"{target_day} 21:00:00"
        e3 = f"{target_day} 22:30:00"
        valid, msg, details = validate_reservation_timing(s3, e3, "08:00", "22:00")
        self.assertFalse(valid)
        self.assertEqual(details["error_type"], "operating_hours")

        # D. Crosses calendar midnight (21:00 day 1 to 01:00 day 2)
        next_day = target_day + datetime.timedelta(days=1)
        s4 = f"{target_day} 21:00:00"
        e4 = f"{next_day} 01:00:00"
        valid, msg, details = validate_reservation_timing(s4, e4, "08:00", "22:00")
        self.assertFalse(valid)
        self.assertEqual(details["error_type"], "cross_day")

        # E. Start >= End
        valid, msg, details = validate_reservation_timing(e1, s1, "08:00", "22:00")
        self.assertFalse(valid)
        self.assertEqual(details["error_type"], "invalid_interval")

        # F. Duration below minimum (e.g. 5 minutes)
        s5 = f"{target_day} 10:00:00"
        e5 = f"{target_day} 10:05:00"
        valid, msg, details = validate_reservation_timing(s5, e5, "08:00", "22:00", min_duration_minutes=15)
        self.assertFalse(valid)
        self.assertEqual(details["error_type"], "duration_too_short")

    def test_02_single_capacity_interval_overlaps(self):
        """Test interval conflict detection for single-capacity machine across all overlap patterns."""
        bambu = get_machine_by_code("BAMBU-X1C-01")
        self.assertIsNotNone(bambu)
        machine_id = bambu["id"]

        now = now_rome()
        target_day = (now + datetime.timedelta(days=6)).date()

        # Existing reservation: [11:00, 13:00)
        res_start = f"{target_day} 11:00:00"
        res_end = f"{target_day} 13:00:00"

        with transaction() as conn:
            conn.execute(
                """
                INSERT INTO reservations (machine_id, member_id, title, start_time, end_time, status, created_at, updated_at)
                VALUES (?, 1, 'Overlap Test Booking', ?, ?, 'confirmed', ?, ?);
                """,
                (machine_id, res_start, res_end, now_rome_iso(), now_rome_iso()),
            )

        # 1. Exact overlap: [11:00, 13:00) -> Conflict
        avail, msg, conflicts, _ = check_machine_availability(machine_id, res_start, res_end)
        self.assertFalse(avail)
        self.assertTrue(any(c["type"] == "capacity_exceeded" for c in conflicts))

        # 2. Partial left overlap: [10:30, 11:30) -> Conflict
        avail, msg, conflicts, _ = check_machine_availability(
            machine_id, f"{target_day} 10:30:00", f"{target_day} 11:30:00"
        )
        self.assertFalse(avail)

        # 3. Partial right overlap: [12:30, 13:30) -> Conflict
        avail, msg, conflicts, _ = check_machine_availability(
            machine_id, f"{target_day} 12:30:00", f"{target_day} 13:30:00"
        )
        self.assertFalse(avail)

        # 4. Enclosing interval: [10:00, 14:00) -> Conflict
        avail, msg, conflicts, _ = check_machine_availability(
            machine_id, f"{target_day} 10:00:00", f"{target_day} 14:00:00"
        )
        self.assertFalse(avail)

        # 5. Enclosed interval: [11:30, 12:30) -> Conflict
        avail, msg, conflicts, _ = check_machine_availability(
            machine_id, f"{target_day} 11:30:00", f"{target_day} 12:30:00"
        )
        self.assertFalse(avail)

    def test_03_abutting_intervals_do_not_conflict(self):
        """Test that abutting/adjacent intervals (half-open [start, end)) do NOT conflict."""
        bambu = get_machine_by_code("BAMBU-X1C-01")
        machine_id = bambu["id"]

        now = now_rome()
        target_day = (now + datetime.timedelta(days=6)).date()
        # Note: existing booking from test_02 is [11:00, 13:00)

        # Immediately before: [09:00, 11:00) -> Should be AVAILABLE
        avail_before, msg_before, conflicts_before, meta_before = check_machine_availability(
            machine_id, f"{target_day} 09:00:00", f"{target_day} 11:00:00"
        )
        self.assertTrue(avail_before, f"Expected before-abutting interval to be available, got msg: {msg_before}")
        self.assertEqual(meta_before["available_capacity_slots"], 1)

        # Immediately after: [13:00, 15:00) -> Should be AVAILABLE
        avail_after, msg_after, conflicts_after, meta_after = check_machine_availability(
            machine_id, f"{target_day} 13:00:00", f"{target_day} 15:00:00"
        )
        self.assertTrue(avail_after, f"Expected after-abutting interval to be available, got msg: {msg_after}")
        self.assertEqual(meta_after["available_capacity_slots"], 1)

    def test_04_multi_capacity_concurrency_sweep_line(self):
        """Test multi-capacity machine (capacity=3) with sweep-line concurrent overlap calculations."""
        cats = list_machine_categories()
        cat_3d = next(c for c in cats if c["code"] == "3d_printers")

        # Create a machine with capacity = 3 (e.g. 3-station farm)
        farm = create_machine(
            code="MULTI-FARM-03",
            name="Prusa 3D Print Farm (3 Stations)",
            category_id=cat_3d["id"],
            capacity=3,
            state="available",
            operating_hours_start="08:00",
            operating_hours_end="22:00",
            actor=self.admin_user,
        )
        farm_id = farm["id"]

        now = now_rome()
        target_day = (now + datetime.timedelta(days=7)).date()

        # Slot times
        t_10_00 = f"{target_day} 10:00:00"
        t_11_00 = f"{target_day} 11:00:00"
        t_12_00 = f"{target_day} 12:00:00"
        t_10_30 = f"{target_day} 10:30:00"
        t_11_30 = f"{target_day} 11:30:00"

        # 0 bookings: checking [10:00, 12:00) -> Peak = 0, Available = 3
        avail, msg, conflicts, meta = check_machine_availability(farm_id, t_10_00, t_12_00)
        self.assertTrue(avail)
        self.assertEqual(meta["peak_concurrent_booked"], 0)
        self.assertEqual(meta["available_capacity_slots"], 3)

        # Insert Booking #1: [10:00, 12:00)
        with transaction() as conn:
            conn.execute(
                "INSERT INTO reservations (machine_id, member_id, title, start_time, end_time, status, created_at, updated_at) VALUES (?, 1, 'Booking 1', ?, ?, 'confirmed', ?, ?);",
                (farm_id, t_10_00, t_12_00, now_rome_iso(), now_rome_iso()),
            )

        # 1 booking: checking [10:00, 12:00) -> Peak = 1, Available = 2
        avail, msg, conflicts, meta = check_machine_availability(farm_id, t_10_00, t_12_00)
        self.assertTrue(avail)
        self.assertEqual(meta["peak_concurrent_booked"], 1)
        self.assertEqual(meta["available_capacity_slots"], 2)

        # Insert Booking #2: [10:30, 11:30) (staggered inside)
        with transaction() as conn:
            conn.execute(
                "INSERT INTO reservations (machine_id, member_id, title, start_time, end_time, status, created_at, updated_at) VALUES (?, 2, 'Booking 2', ?, ?, 'confirmed', ?, ?);",
                (farm_id, t_10_30, t_11_30, now_rome_iso(), now_rome_iso()),
            )

        # 2 overlapping bookings during [10:30, 11:30): checking [10:00, 12:00) -> Peak = 2, Available = 1
        avail, msg, conflicts, meta = check_machine_availability(farm_id, t_10_00, t_12_00)
        self.assertTrue(avail)
        self.assertEqual(meta["peak_concurrent_booked"], 2)
        self.assertEqual(meta["available_capacity_slots"], 1)

        # Insert Booking #3: [10:45, 11:15) (overlaps both Booking 1 & Booking 2)
        t_10_45 = f"{target_day} 10:45:00"
        t_11_15 = f"{target_day} 11:15:00"
        with transaction() as conn:
            conn.execute(
                "INSERT INTO reservations (machine_id, member_id, title, start_time, end_time, status, created_at, updated_at) VALUES (?, 3, 'Booking 3', ?, ?, 'confirmed', ?, ?);",
                (farm_id, t_10_45, t_11_15, now_rome_iso(), now_rome_iso()),
            )

        # Now during [10:45, 11:15), all 3 slots are occupied!
        # A 4th request overlapping [10:45, 11:15) -> Capacity Exceeded (3/3)
        avail, msg, conflicts, meta = check_machine_availability(farm_id, t_10_45, t_11_15)
        self.assertFalse(avail)
        self.assertEqual(meta["peak_concurrent_booked"], 3)
        self.assertEqual(meta["available_capacity_slots"], 0)
        self.assertIn("Capacity limit reached", msg)

        # But a request for [11:45, 12:45) where only Booking 1 is active (1/3 slots) -> AVAILABLE (2 slots free)
        t_11_45 = f"{target_day} 11:45:00"
        t_12_45 = f"{target_day} 12:45:00"
        avail_late, msg_late, conflicts_late, meta_late = check_machine_availability(farm_id, t_11_45, t_12_45)
        self.assertTrue(avail_late)
        self.assertEqual(meta_late["peak_concurrent_booked"], 1)
        self.assertEqual(meta_late["available_capacity_slots"], 2)

    def test_05_exclude_reservation_id_for_edits(self):
        """Test excluding a reservation ID when checking availability during modifications."""
        epilog = get_machine_by_code("EPILOG-FUSION-PRO")
        machine_id = epilog["id"]

        now = now_rome()
        target_day = (now + datetime.timedelta(days=8)).date()
        t_start = f"{target_day} 14:00:00"
        t_end = f"{target_day} 16:00:00"

        # Create reservation #R
        with transaction() as conn:
            cur = conn.execute(
                "INSERT INTO reservations (machine_id, member_id, title, start_time, end_time, status, created_at, updated_at) VALUES (?, 1, 'Self Edit Test', ?, ?, 'confirmed', ?, ?);",
                (machine_id, t_start, t_end, now_rome_iso(), now_rome_iso()),
            )
            res_id = cur.lastrowid

        # Without exclude: checking [14:30, 15:30) is UNAVAILABLE
        avail, msg, conflicts, _ = check_machine_availability(
            machine_id, f"{target_day} 14:30:00", f"{target_day} 15:30:00"
        )
        self.assertFalse(avail)

        # With exclude_reservation_id=res_id: checking [14:30, 15:30) is AVAILABLE
        avail_ex, msg_ex, conflicts_ex, _ = check_machine_availability(
            machine_id, f"{target_day} 14:30:00", f"{target_day} 15:30:00", exclude_reservation_id=res_id
        )
        self.assertTrue(avail_ex)
        self.assertEqual(len(conflicts_ex), 0)

    def test_06_maintenance_windows_blocking(self):
        """Test that scheduled and in-progress maintenance windows block availability, while completed do not."""
        bambu = get_machine_by_code("BAMBU-X1C-01")
        machine_id = bambu["id"]

        now = now_rome()
        target_day = (now + datetime.timedelta(days=9)).date()
        mw_start = f"{target_day} 09:00:00"
        mw_end = f"{target_day} 12:00:00"

        # 1. Create scheduled maintenance window
        mw = create_maintenance_window(
            machine_id=machine_id,
            title="Periodic Rod Lubrication",
            start_time=mw_start,
            end_time=mw_end,
            status="scheduled",
            actor=self.operator_user,
        )

        # Overlapping check [10:00, 11:00) -> BLOCKED
        avail, msg, conflicts, _ = check_machine_availability(
            machine_id, f"{target_day} 10:00:00", f"{target_day} 11:00:00"
        )
        self.assertFalse(avail)
        self.assertTrue(any(c["type"] == "maintenance_window" for c in conflicts))

        # 2. Mark window as cancelled -> Now AVAILABLE
        with transaction() as conn:
            conn.execute("UPDATE maintenance_windows SET status = 'cancelled' WHERE id = ?;", (mw["id"],))

        avail_canc, msg_canc, conflicts_canc, _ = check_machine_availability(
            machine_id, f"{target_day} 10:00:00", f"{target_day} 11:00:00"
        )
        self.assertTrue(avail_canc)

    def test_07_critical_incidents_out_of_service(self):
        """Test critical incidents taking a machine out of service block all new availability."""
        saw = get_machine_by_code("ALTENDORF-F45")
        self.assertIsNotNone(saw)
        machine_id = saw["id"]

        now = now_rome()
        target_day = (now + datetime.timedelta(days=10)).date()
        req_start = f"{target_day} 10:00:00"
        req_end = f"{target_day} 12:00:00"

        # Create active critical incident taking machine out of service
        with transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO incidents (machine_id, title, description, severity, status, takes_machine_out_of_service, created_at, updated_at)
                VALUES (?, 'Spindle Emergency Thermal Shutdown', 'Excessive vibration and thermal error in spindle bearing', 'critical', 'investigating', 1, ?, ?);
                """,
                (machine_id, now_rome_iso(), now_rome_iso()),
            )
            inc_id = cur.lastrowid

        # Availability check should be BLOCKED due to critical incident
        avail, msg, conflicts, meta = check_machine_availability(machine_id, req_start, req_end)
        self.assertFalse(avail)
        self.assertTrue(any(c["type"] == "critical_incident" for c in conflicts))
        self.assertIn("out of service", msg.lower())

        # Resolve incident -> takes_machine_out_of_service cleared / closed
        with transaction() as conn:
            conn.execute("UPDATE incidents SET status = 'resolved', takes_machine_out_of_service = 0 WHERE id = ?;", (inc_id,))

        # Now AVAILABLE
        avail_res, msg_res, conflicts_res, _ = check_machine_availability(machine_id, req_start, req_end)
        self.assertTrue(avail_res)

    def test_08_machine_state_enforcement(self):
        """Test machine state transitions (available, temporarily_unavailable, under_maintenance, retired)."""
        epilog = get_machine_by_code("EPILOG-FUSION-PRO")
        machine_id = epilog["id"]

        now = now_rome()
        target_day = (now + datetime.timedelta(days=11)).date()
        req_start = f"{target_day} 10:00:00"
        req_end = f"{target_day} 12:00:00"

        # 1. State = temporarily_unavailable -> Blocked
        change_machine_state(machine_id, "temporarily_unavailable", reason="Laser tube alignment test")
        avail, msg, conflicts, _ = check_machine_availability(machine_id, req_start, req_end)
        self.assertFalse(avail)
        self.assertTrue(any(c.get("state") == "temporarily_unavailable" for c in conflicts))

        # 2. State = under_maintenance -> Blocked
        change_machine_state(machine_id, "under_maintenance", reason="Emergency cleaning")
        avail, msg, conflicts, _ = check_machine_availability(machine_id, req_start, req_end)
        self.assertFalse(avail)
        self.assertTrue(any(c.get("state") == "under_maintenance" for c in conflicts))

        # 3. Restore to available -> OK
        change_machine_state(machine_id, "available", reason="Testing complete")
        avail, msg, conflicts, _ = check_machine_availability(machine_id, req_start, req_end)
        self.assertTrue(avail)

    def test_09_member_qualification_temporal_validation(self):
        """Test qualification validity over time: valid, missing, expired, and expiring before check-in."""
        # Create a new member
        mem_id = create_member(
            full_name="Alessia Conti",
            email="alessia.conti@makerspace.test",
            phone="+39 340 9988771",
            membership_status="active",
            membership_expiry="2027-12-31",
            actor_user=self.admin_user,
        )

        cats = list_machine_categories()
        cat_cnc = next(c for c in cats if c["code"] == "cnc_mills")
        mill = get_machine_by_code("HAAS-MINI-MILL")
        self.assertIsNotNone(mill)
        machine_id = mill["id"]

        target_date_str = "2026-10-15"
        req_start = f"{target_date_str} 10:00:00"
        req_end = f"{target_date_str} 12:00:00"

        # A. No qualification -> Rejected
        is_elig, elig_msg, elig_conflicts = validate_member_reservation_eligibility(
            member_id=mem_id, machine_id=machine_id, start_time=req_start, end_time=req_end
        )
        self.assertFalse(is_elig)
        self.assertTrue(any(c["type"] == "qualification_missing" for c in elig_conflicts))

        # B. Expired qualification (expired 2026-09-30, target reservation is 2026-10-15) -> Rejected
        grant_qualification(
            member_id=mem_id,
            category_id=cat_cnc["id"],
            qualification_name="CNC Mill Operator Level 1",
            issue_date="2026-01-01",
            expiry_date="2026-09-30",
            actor_user=self.admin_user,
        )

        is_elig, elig_msg, elig_conflicts = validate_member_reservation_eligibility(
            member_id=mem_id, machine_id=machine_id, start_time=req_start, end_time=req_end
        )
        self.assertFalse(is_elig)
        self.assertTrue(any(c["type"] == "qualification_expired" for c in elig_conflicts))

        # C. Re-certify qualification with valid future expiry (2027-01-01) -> Accepted
        grant_qualification(
            member_id=mem_id,
            category_id=cat_cnc["id"],
            qualification_name="CNC Mill Operator Level 1 (Recertified)",
            issue_date="2026-10-01",
            expiry_date="2027-01-01",
            actor_user=self.admin_user,
        )

        is_elig, elig_msg, elig_conflicts = validate_member_reservation_eligibility(
            member_id=mem_id, machine_id=machine_id, start_time=req_start, end_time=req_end
        )
        self.assertTrue(is_elig, f"Expected valid qualification eligibility, got: {elig_msg}")
        self.assertEqual(len(elig_conflicts), 0)

        # D. Target reservation in future where qualification is expired (2027-02-15) -> Rejected
        fut_start = "2027-02-15 10:00:00"
        fut_end = "2027-02-15 12:00:00"
        is_elig, elig_msg, elig_conflicts = validate_member_reservation_eligibility(
            member_id=mem_id, machine_id=machine_id, start_time=fut_start, end_time=fut_end
        )
        self.assertFalse(is_elig)
        self.assertTrue(any(c["type"] == "qualification_expired" for c in elig_conflicts))

    def test_10_membership_status_and_expiry_validation(self):
        """Test suspended or expired membership blocks availability even if qualifications exist."""
        mem_id = create_member(
            full_name="Roberto Mancini",
            email="roberto.mancini@makerspace.test",
            phone="+39 340 1234567",
            membership_status="active",
            membership_expiry="2026-12-31",
            actor_user=self.admin_user,
        )

        cats = list_machine_categories()
        cat_3d = next(c for c in cats if c["code"] == "3d_printers")
        bambu = get_machine_by_code("BAMBU-X1C-01")

        grant_qualification(
            member_id=mem_id,
            category_id=cat_3d["id"],
            qualification_name="3D Printing Safety Master",
            issue_date="2026-01-01",
            expiry_date="2027-01-01",
            actor_user=self.admin_user,
        )

        req_start = "2026-11-10 10:00:00"
        req_end = "2026-11-10 12:00:00"

        # 1. Active member -> Eligible
        is_elig, _, _ = validate_member_reservation_eligibility(mem_id, bambu["id"], req_start, req_end)
        self.assertTrue(is_elig)

        # 2. Suspend member -> Blocked
        update_member(mem_id, membership_status="suspended", actor_user=self.admin_user)
        is_elig, msg, conflicts = validate_member_reservation_eligibility(mem_id, bambu["id"], req_start, req_end)
        self.assertFalse(is_elig)
        self.assertTrue(any(c["type"] == "membership_status_invalid" for c in conflicts))

        # 3. Membership expired before reservation date -> Blocked
        update_member(mem_id, membership_status="active", membership_expiry="2026-10-31", actor_user=self.admin_user)
        is_elig, msg, conflicts = validate_member_reservation_eligibility(mem_id, bambu["id"], req_start, req_end)
        self.assertFalse(is_elig)
        self.assertTrue(any(c["type"] == "membership_expired" for c in conflicts))

    def test_11_timeline_slot_generation(self):
        """Test daily timeline slot generation with capacity, bookings, and maintenance markers."""
        bambu = get_machine_by_code("BAMBU-X1C-01")
        machine_id = bambu["id"]

        now = now_rome()
        target_day = (now + datetime.timedelta(days=12)).strftime("%Y-%m-%d")

        timeline = generate_timeline_slots(machine_id=machine_id, date_str=target_day, slot_duration_minutes=30)
        self.assertEqual(timeline["machine_id"], machine_id)
        self.assertEqual(timeline["date"], target_day)
        self.assertGreater(timeline["total_slots"], 0)

        # Between 08:00 and 22:00 (14 hours), 30 min slots = 28 slots
        self.assertEqual(timeline["total_slots"], 28)
        first_slot = timeline["slots"][0]
        self.assertEqual(first_slot["time_label"], "08:00 - 08:30")
        self.assertEqual(first_slot["total_capacity"], 1)

    def test_12_historical_reservations_preserved_on_retirement(self):
        """Test that retiring a machine preserves all historical reservations and blocks physical deletion."""
        cats = list_machine_categories()
        cat_wood = next(c for c in cats if c["code"] == "woodworking")

        # 1. Create a machine with historical reservations
        saw = create_machine(
            code="BANDSAW-HIST-01",
            name="Heavy Duty Laguna Bandsaw",
            category_id=cat_wood["id"],
            capacity=1,
            state="available",
            actor=self.admin_user,
        )
        saw_id = saw["id"]

        # Insert historical reservations (past, completed, cancelled)
        with transaction() as conn:
            conn.execute(
                """
                INSERT INTO reservations (machine_id, member_id, title, start_time, end_time, status, created_at, updated_at)
                VALUES (?, 1, 'Past Project Woodcut', '2026-01-10 10:00:00', '2026-01-10 12:00:00', 'checked_out', ?, ?),
                       (?, 2, 'Past Table Leg Shaping', '2026-02-15 14:00:00', '2026-02-15 16:00:00', 'checked_out', ?, ?);
                """,
                (saw_id, now_rome_iso(), now_rome_iso(), saw_id, now_rome_iso(), now_rome_iso()),
            )

        # 2. Attempt hard delete -> Must be rejected with clear explanation
        with self.assertRaises(ValueError) as ctx:
            delete_machine(saw_id, actor=self.admin_user)
        self.assertIn("historical reservation", str(ctx.exception).lower())

        # 3. Retire machine
        retired = retire_machine(saw_id, reason="Replaced with new 24-inch model", actor=self.admin_user)
        self.assertEqual(retired["state"], "retired")

        # 4. New reservations must be rejected on retired machine
        now = now_rome()
        fut_start = (now + datetime.timedelta(days=1)).replace(hour=10, minute=0, second=0).isoformat()
        fut_end = (now + datetime.timedelta(days=1)).replace(hour=12, minute=0, second=0).isoformat()

        avail, msg, conflicts, _ = check_machine_availability(saw_id, fut_start, fut_end)
        self.assertFalse(avail)
        self.assertIn("retired", msg.lower())

        # 5. Query historical reservations -> Completely preserved!
        hist = get_historical_reservations_for_machine(saw_id)
        self.assertEqual(hist["total_historical_reservations"], 2)
        self.assertEqual(len(hist["reservations"]), 2)
        self.assertEqual(hist["machine"]["state"], "retired")

    def test_13_batch_machine_availability_api(self):
        """Test scanning batch machine availability for a given time window."""
        now = now_rome()
        target_day = (now + datetime.timedelta(days=13)).date()
        s_time = f"{target_day} 14:00:00"
        e_time = f"{target_day} 16:00:00"

        batch = get_batch_machine_availability(start_time=s_time, end_time=e_time)
        self.assertGreater(len(batch), 0)
        for item in batch:
            self.assertIn("machine_id", item)
            self.assertIn("is_available", item)
            self.assertIn("capacity", item)

    def test_14_availability_rest_api_and_rbac(self):
        """Test REST API endpoints for availability checking, timelines, and RBAC."""
        bambu = get_machine_by_code("BAMBU-X1C-01")
        machine_id = bambu["id"]

        now = now_rome()
        target_day = (now + datetime.timedelta(days=14)).date()
        s_time = f"{target_day} 10:00:00"
        e_time = f"{target_day} 12:00:00"

        # 1. Unauthenticated request -> 401 Unauthorized
        resp_unauth = self._make_request(
            "GET",
            "/api/availability/check",
            query_params={"machine_id": str(machine_id), "start_time": s_time, "end_time": e_time},
        )
        self.assertEqual(resp_unauth.status_code, 401)

        # 2. Member checks availability -> 200 OK
        resp_mem = self._make_request(
            "GET",
            "/api/availability/check",
            bearer_token=self.member_session,
            query_params={"machine_id": str(machine_id), "start_time": s_time, "end_time": e_time},
        )
        self.assertEqual(resp_mem.status_code, 200)
        data_mem = json.loads(resp_mem.body.decode("utf-8"))
        self.assertTrue(data_mem["success"])
        self.assertIn("is_available", data_mem)

        # 3. Viewer checks timeline -> 200 OK
        resp_view_tl = self._make_request(
            "GET",
            "/api/availability/timeline",
            bearer_token=self.viewer_session,
            query_params={"machine_id": str(machine_id), "date": str(target_day)},
        )
        self.assertEqual(resp_view_tl.status_code, 200)
        data_tl = json.loads(resp_view_tl.body.decode("utf-8"))
        self.assertTrue(data_tl["success"])
        self.assertEqual(data_tl["timeline"]["machine_id"], machine_id)

        # 4. Operator checks batch machine availability -> 200 OK
        resp_op_batch = self._make_request(
            "GET",
            "/api/availability/machines",
            bearer_token=self.operator_session,
            query_params={"start_time": s_time, "end_time": e_time},
        )
        self.assertEqual(resp_op_batch.status_code, 200)
        data_batch = json.loads(resp_op_batch.body.decode("utf-8"))
        self.assertTrue(data_batch["success"])
        self.assertGreater(data_batch["total_machines"], 0)

        # 5. Historical reservations API -> 200 OK
        resp_hist = self._make_request(
            "GET",
            f"/api/machines/{machine_id}/historical-reservations",
            bearer_token=self.admin_session,
        )
        self.assertEqual(resp_hist.status_code, 200)
        data_hist = json.loads(resp_hist.body.decode("utf-8"))
        self.assertTrue(data_hist["success"])

    def test_15_html_availability_view(self):
        """Test HTML availability explorer rendering and authentication gate."""
        # Unauthenticated -> redirect to login
        req_unauth = Request(method="GET", path="/availability", headers={}, client_address=("127.0.0.1", 50000))
        resp_unauth = self.router.dispatch(req_unauth)
        self.assertIn(resp_unauth.status_code, (302, 303))
        self.assertIn("/auth/login", resp_unauth.headers.get("Location", ""))

        # Authenticated member -> 200 OK HTML
        bambu = get_machine_by_code("BAMBU-X1C-01")
        req_auth = Request(
            method="GET",
            path=f"/availability?machine_id={bambu['id']}",
            headers={"authorization": f"Bearer {self.member_session}"},
            client_address=("127.0.0.1", 50000),
        )
        resp_auth = self.router.dispatch(req_auth)
        self.assertEqual(resp_auth.status_code, 200)
        body_html = resp_auth.body.decode("utf-8")
        self.assertIn("Machine Availability & Schedule Timeline", body_html)
        self.assertIn("Daily Schedule Timeline", body_html)


if __name__ == "__main__":
    unittest.main()
