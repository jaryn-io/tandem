"""Comprehensive Test Suite for Recurring Reservations, Europe/Rome DST, and Isolated Occurrence Editing (Step S13)."""

import datetime
import json
import unittest
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from app import create_application_router
from forgedesk.auth.service import authenticate_user, create_user, create_user_session
from forgedesk.core.http import Request, Response
from forgedesk.db.connection import get_connection, query_all, query_one
from forgedesk.db.migrations import apply_migrations, reset_database
from forgedesk.db.seed import seed_database
from forgedesk.members.service import create_member, grant_qualification, list_members
from forgedesk.reservations.availability import check_machine_availability, find_interval_conflicts
from forgedesk.reservations.recurrence import (
    APP_TIMEZONE,
    RecurrenceRuleError,
    format_recurrence_summary,
    generate_occurrence_datetimes,
    parse_recurrence_rule,
)
from forgedesk.reservations.service import (
    ReservationConflictError,
    ReservationNotFoundError,
    ReservationPermissionError,
    cancel_recurring_series,
    cancel_reservation,
    create_recurring_reservations,
    create_reservation,
    get_recurring_series,
    get_reservation_by_id,
    list_reservations,
    update_reservation,
)
from forgedesk.utils.datetime_tz import format_iso, now_rome, now_rome_iso, parse_datetime, today_rome_str


class TestReservationsRecurring(unittest.TestCase):
    """Test suite covering recurring reservation logic, Europe/Rome DST, and isolated edits."""

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

    def setUp(self):
        """Reset and seed database before each test for complete state isolation."""
        reset_database()
        apply_migrations()
        seed_database(force=True)

        self.admin_user = authenticate_user("admin", "admin123")
        self.admin_session = create_user_session(self.admin_user["id"])

        self.operator_user = authenticate_user("operator", "operator123")
        self.operator_session = create_user_session(self.operator_user["id"])

        self.member_alice = authenticate_user("member_alice", "member123")
        self.alice_session = create_user_session(self.member_alice["id"])

        self.member_bob = authenticate_user("member_bob", "member123")
        self.bob_session = create_user_session(self.member_bob["id"])

        self.viewer_user = authenticate_user("viewer", "viewer123")
        self.viewer_session = create_user_session(self.viewer_user["id"])

        alice_mem = query_one("SELECT * FROM members WHERE user_id = ?;", (self.member_alice["id"],))
        self.alice_member_id = alice_mem["id"]

        bob_mem = query_one("SELECT * FROM members WHERE user_id = ?;", (self.member_bob["id"],))
        self.bob_member_id = bob_mem["id"]

    def _make_request(
        self,
        method: str,
        path: str,
        bearer_token: Optional[str] = None,
        body: Optional[Any] = None,
        content_type: Optional[str] = None,
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

        req = Request(
            method=method,
            path=path,
            headers=headers,
            body=body_bytes,
            client_address=(client_ip, 54321),
        )
        return self.router.dispatch(req)

    # -------------------------------------------------------------------------
    # 1. Recurrence Rule Parsing, Validation, and Formatting
    # -------------------------------------------------------------------------

    def test_recurrence_rule_parsing_and_formatting(self):
        """Verify parsing of various recurrence rule dictionaries and strings."""
        rule1 = parse_recurrence_rule({"frequency": "weekly", "weekdays": ["MO", "WE"], "count": 6})
        self.assertEqual(rule1["frequency"], "weekly")
        self.assertEqual(rule1["weekdays"], ["MO", "WE"])
        self.assertEqual(rule1["count"], 6)
        self.assertEqual(format_recurrence_summary(rule1), "Weekly on Mon, Wed (6 occurrences)")

        rule2 = parse_recurrence_rule('{"frequency": "biweekly", "weekdays": ["FR"], "until": "2026-12-31"}')
        self.assertEqual(rule2["frequency"], "biweekly")
        self.assertEqual(rule2["weekdays"], ["FR"])
        self.assertEqual(rule2["until"], "2026-12-31")
        self.assertEqual(format_recurrence_summary(rule2), "Bi-weekly on Fri (until 2026-12-31)")

        rule3 = parse_recurrence_rule({"frequency": "daily", "count": 5})
        self.assertEqual(rule3["frequency"], "daily")
        self.assertEqual(format_recurrence_summary(rule3), "Daily (5 occurrences)")

        # Invalid rules
        with self.assertRaises(RecurrenceRuleError):
            parse_recurrence_rule({"frequency": "invalid_freq"})

        with self.assertRaises(RecurrenceRuleError):
            parse_recurrence_rule({"frequency": "weekly", "count": 0})

        with self.assertRaises(RecurrenceRuleError):
            parse_recurrence_rule({"frequency": "weekly", "count": 999})

    # -------------------------------------------------------------------------
    # 2. DST Transitions in Europe/Rome (Wall-Clock Preservation)
    # -------------------------------------------------------------------------

    def test_dst_spring_transition_wall_clock_preservation(self):
        """Verify that occurrences across Spring DST change strictly preserve Europe/Rome wall-clock time."""
        # Spring 2026 transition occurs on Sunday, March 29, 2026 (CET UTC+1 -> CEST UTC+2).
        # We book weekly on Mondays: March 23 (CET) and March 30 (CEST), from 10:00 to 12:00.
        rule = {"frequency": "weekly", "weekdays": ["MO"], "count": 2}
        intervals = generate_occurrence_datetimes(
            start_date="2026-03-23",
            start_time_of_day="10:00",
            end_time_of_day="12:00",
            recurrence_rule=rule,
        )

        self.assertEqual(len(intervals), 2)
        s1, e1 = intervals[0]
        s2, e2 = intervals[1]

        # Occurrence 1: 2026-03-23 in CET (UTC+1)
        self.assertEqual(str(s1.date()), "2026-03-23")
        self.assertEqual(s1.strftime("%H:%M:%S"), "10:00:00")
        self.assertEqual(e1.strftime("%H:%M:%S"), "12:00:00")
        self.assertEqual(s1.strftime("%z"), "+0100")
        self.assertEqual((e1 - s1).total_seconds(), 7200)

        # Occurrence 2: 2026-03-30 in CEST (UTC+2)
        self.assertEqual(str(s2.date()), "2026-03-30")
        self.assertEqual(s2.strftime("%H:%M:%S"), "10:00:00")
        self.assertEqual(e2.strftime("%H:%M:%S"), "12:00:00")
        self.assertEqual(s2.strftime("%z"), "+0200")
        self.assertEqual((e2 - s2).total_seconds(), 7200)

    def test_dst_autumn_transition_wall_clock_preservation(self):
        """Verify that occurrences across Autumn DST change strictly preserve Europe/Rome wall-clock time."""
        # Autumn 2026 transition occurs on Sunday, October 25, 2026 (CEST UTC+2 -> CET UTC+1).
        # We book weekly on Mondays: October 19 (CEST) and October 26 (CET), from 14:30 to 16:30.
        rule = {"frequency": "weekly", "weekdays": ["MO"], "count": 2}
        intervals = generate_occurrence_datetimes(
            start_date="2026-10-19",
            start_time_of_day="14:30",
            end_time_of_day="16:30",
            recurrence_rule=rule,
        )

        self.assertEqual(len(intervals), 2)
        s1, e1 = intervals[0]
        s2, e2 = intervals[1]

        # Occurrence 1: 2026-10-19 in CEST (UTC+2)
        self.assertEqual(str(s1.date()), "2026-10-19")
        self.assertEqual(s1.strftime("%H:%M:%S"), "14:30:00")
        self.assertEqual(e1.strftime("%H:%M:%S"), "16:30:00")
        self.assertEqual(s1.strftime("%z"), "+0200")
        self.assertEqual((e1 - s1).total_seconds(), 7200)

        # Occurrence 2: 2026-10-26 in CET (UTC+1)
        self.assertEqual(str(s2.date()), "2026-10-26")
        self.assertEqual(s2.strftime("%H:%M:%S"), "14:30:00")
        self.assertEqual(e2.strftime("%H:%M:%S"), "16:30:00")
        self.assertEqual(s2.strftime("%z"), "+0100")
        self.assertEqual((e2 - s2).total_seconds(), 7200)

    # -------------------------------------------------------------------------
    # 3. Recurring Series Creation, Retrieval, and Rollback
    # -------------------------------------------------------------------------

    def test_create_recurring_series_success(self):
        """Verify atomic creation of a multi-week recurring reservation series."""
        rule = {"frequency": "weekly", "weekdays": ["MO"], "count": 4}
        res_batch = create_recurring_reservations(
            machine_id=1,  # Laser Cutter 1
            member_id=self.alice_member_id,   # Alice (qualified)
            start_date="2026-09-07",
            start_time_of_day="10:00",
            end_time_of_day="12:00",
            recurrence_rule=rule,
            title="Weekly Laser Prototyping",
            actor_user=self.admin_user,
        )

        self.assertEqual(res_batch["created_count"], 4)
        self.assertEqual(res_batch["total_planned"], 4)
        group_id = res_batch["recurrence_group_id"]
        self.assertTrue(group_id.startswith("rec_"))

        # Verify series retrieval
        series_info = get_recurring_series(group_id)
        self.assertIsNotNone(series_info)
        self.assertEqual(series_info["total_occurrences"], 4)
        self.assertEqual(series_info["active_occurrences"], 4)
        self.assertEqual(series_info["cancelled_occurrences"], 0)

        # Verify linked details in get_reservation_by_id
        first_id = res_batch["created_reservations"][0]["id"]
        single_det = get_reservation_by_id(first_id, include_details=True)
        self.assertIsNotNone(single_det["recurrence_series"])
        self.assertEqual(single_det["recurrence_series"]["group_id"], group_id)
        self.assertEqual(single_det["recurrence_series"]["total_occurrences"], 4)
        self.assertEqual(single_det["recurrence_series"]["current_index"], 1)

    def test_create_recurring_series_strict_conflict_rollback(self):
        """Verify that in strict mode, a conflict on any occurrence aborts the entire series atomically."""
        # Create a blocking single reservation on week 3 (2026-09-21 11:00 to 13:00)
        create_reservation(
            machine_id=1,
            member_id=self.bob_member_id,
            start_time="2026-09-21T11:00:00",
            end_time="2026-09-21T13:00:00",
            title="Blocking single booking",
            actor_user=self.admin_user,
        )

        rule = {"frequency": "weekly", "weekdays": ["MO"], "count": 4}
        with self.assertRaises(ReservationConflictError) as ctx:
            create_recurring_reservations(
                machine_id=1,
                member_id=self.alice_member_id,
                start_date="2026-09-07",
                start_time_of_day="10:00",
                end_time_of_day="12:00",
                recurrence_rule=rule,
                skip_conflicts=False,
                actor_user=self.admin_user,
            )

        self.assertIn("Recurring booking conflict on 1 occurrence(s)", str(ctx.exception))

        # Verify database atomicity: no occurrences from the batch were created
        all_res = list_reservations(machine_id=1, member_id=self.alice_member_id, start_date="2026-09-01")
        self.assertEqual(len(all_res), 0)

    def test_create_recurring_series_flexible_skip_conflicts(self):
        """Verify that in flexible mode (skip_conflicts=True), available occurrences are booked and conflicts skipped."""
        # Block week 2 (2026-09-14 10:00 to 12:00)
        create_reservation(
            machine_id=1,
            member_id=self.bob_member_id,
            start_time="2026-09-14T10:00:00",
            end_time="2026-09-14T12:00:00",
            title="Blocking week 2",
            actor_user=self.admin_user,
        )

        rule = {"frequency": "weekly", "weekdays": ["MO"], "count": 4}
        res_batch = create_recurring_reservations(
            machine_id=1,
            member_id=self.alice_member_id,
            start_date="2026-09-07",
            start_time_of_day="10:00",
            end_time_of_day="12:00",
            recurrence_rule=rule,
            skip_conflicts=True,
            actor_user=self.admin_user,
        )

        self.assertEqual(res_batch["total_planned"], 4)
        self.assertEqual(res_batch["created_count"], 3)
        self.assertEqual(res_batch["skipped_count"], 1)
        self.assertEqual(res_batch["skipped_conflicts"][0]["date"], "2026-09-14")

    # -------------------------------------------------------------------------
    # 4. Temporal Qualification Validation per Occurrence
    # -------------------------------------------------------------------------

    def test_temporal_qualification_expiration_rejection(self):
        """Verify qualification expiration is checked per occurrence date."""
        # Create a temporary member with a qualification expiring on 2026-09-15
        new_uid = create_user(username="temp_member_exp", email="temp_exp@example.com", password="pwd", full_name="Temp Member Exp", role="member")
        new_member_id = create_member(full_name="Temp Member Exp", email="temp_exp@example.com", user_id=new_uid)
        grant_qualification(
            member_id=new_member_id,
            category_id=1,  # Laser Cutting category
            qualification_name="Laser Cutter Certificate",
            issue_date="2026-01-01",
            expiry_date="2026-09-15",
            actor_user=self.admin_user,
        )

        # Attempt to book 4-week series (Sept 7, Sept 14, Sept 21, Sept 28)
        rule = {"frequency": "weekly", "weekdays": ["MO"], "count": 4}
        with self.assertRaises(ReservationConflictError) as ctx:
            create_recurring_reservations(
                machine_id=1,
                member_id=new_member_id,
                start_date="2026-09-07",
                start_time_of_day="10:00",
                end_time_of_day="12:00",
                recurrence_rule=rule,
                skip_conflicts=False,
                actor_user=self.admin_user,
            )

        self.assertIn("conflict on 2 occurrence(s)", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 5. Isolated Occurrence Modification & Cancellation
    # -------------------------------------------------------------------------

    def test_isolated_occurrence_edit_does_not_alter_siblings(self):
        """Verify editing one occurrence changes ONLY that occurrence and preserves all siblings."""
        rule = {"frequency": "weekly", "weekdays": ["MO"], "count": 4}
        batch = create_recurring_reservations(
            machine_id=1,
            member_id=self.alice_member_id,
            start_date="2026-09-07",
            start_time_of_day="10:00",
            end_time_of_day="12:00",
            recurrence_rule=rule,
            title="Original Series Title",
            actor_user=self.admin_user,
        )

        reservations = batch["created_reservations"]
        occ1_id = reservations[0]["id"]
        occ2_id = reservations[1]["id"]
        occ3_id = reservations[2]["id"]
        occ4_id = reservations[3]["id"]

        # Edit only occurrence #2 (change time to 14:00-16:00 and new title)
        updated_occ2 = update_reservation(
            reservation_id=occ2_id,
            start_time="2026-09-14T14:00:00",
            end_time="2026-09-14T16:00:00",
            title="Customized Occ 2 Title",
            actor_user=self.admin_user,
        )

        self.assertEqual(updated_occ2["title"], "Customized Occ 2 Title")
        self.assertEqual(updated_occ2["start_time"], "2026-09-14T14:00:00+02:00")

        # Verify siblings are untouched
        occ1_fresh = get_reservation_by_id(occ1_id)
        occ3_fresh = get_reservation_by_id(occ3_id)
        occ4_fresh = get_reservation_by_id(occ4_id)

        self.assertEqual(occ1_fresh["title"], "Original Series Title")
        self.assertEqual(occ1_fresh["start_time"], "2026-09-07T10:00:00+02:00")

        self.assertEqual(occ3_fresh["title"], "Original Series Title")
        self.assertEqual(occ3_fresh["start_time"], "2026-09-21T10:00:00+02:00")

        self.assertEqual(occ4_fresh["title"], "Original Series Title")
        self.assertEqual(occ4_fresh["start_time"], "2026-09-28T10:00:00+02:00")

        # All 4 still belong to the recurrence group
        self.assertEqual(occ1_fresh["recurrence_group_id"], batch["recurrence_group_id"])
        self.assertEqual(updated_occ2["recurrence_group_id"], batch["recurrence_group_id"])
        self.assertEqual(occ3_fresh["recurrence_group_id"], batch["recurrence_group_id"])
        self.assertEqual(occ4_fresh["recurrence_group_id"], batch["recurrence_group_id"])

    def test_isolated_occurrence_cancellation_preserves_siblings(self):
        """Verify cancelling one occurrence releases only that slot and leaves siblings confirmed."""
        rule = {"frequency": "weekly", "weekdays": ["MO"], "count": 3}
        batch = create_recurring_reservations(
            machine_id=1,
            member_id=self.alice_member_id,
            start_date="2026-09-07",
            start_time_of_day="10:00",
            end_time_of_day="12:00",
            recurrence_rule=rule,
            actor_user=self.admin_user,
        )

        occ1_id = batch["created_reservations"][0]["id"]
        occ2_id = batch["created_reservations"][1]["id"]
        occ3_id = batch["created_reservations"][2]["id"]

        # Cancel occurrence #2
        cancel_reservation(occ2_id, reason="Doctor appointment", actor_user=self.admin_user)

        occ2_fresh = get_reservation_by_id(occ2_id)
        self.assertEqual(occ2_fresh["status"], "cancelled")
        self.assertEqual(occ2_fresh["cancellation_reason"], "Doctor appointment")

        # Occurrences 1 and 3 remain confirmed
        self.assertEqual(get_reservation_by_id(occ1_id)["status"], "confirmed")
        self.assertEqual(get_reservation_by_id(occ3_id)["status"], "confirmed")

        # The slot on Sept 14 10:00-12:00 is now free for another member
        avail, _, _, _ = check_machine_availability(
            machine_id=1,
            start_time="2026-09-14T10:00:00",
            end_time="2026-09-14T12:00:00",
            member_id=self.bob_member_id,
        )
        self.assertTrue(avail)

    # -------------------------------------------------------------------------
    # 6. Entire Recurring Series Cancellation
    # -------------------------------------------------------------------------

    def test_cancel_entire_recurring_series(self):
        """Verify bulk cancellation of an entire recurring series."""
        rule = {"frequency": "weekly", "weekdays": ["MO"], "count": 4}
        batch = create_recurring_reservations(
            machine_id=1,
            member_id=self.alice_member_id,
            start_date="2026-09-07",
            start_time_of_day="10:00",
            end_time_of_day="12:00",
            recurrence_rule=rule,
            actor_user=self.admin_user,
        )
        group_id = batch["recurrence_group_id"]

        cancel_res = cancel_recurring_series(group_id, reason="Project cancelled", actor_user=self.admin_user)
        self.assertEqual(cancel_res["cancelled_count"], 4)

        series = get_recurring_series(group_id)
        self.assertEqual(series["active_occurrences"], 0)
        self.assertEqual(series["cancelled_occurrences"], 4)

    def test_cancel_future_occurrences_only(self):
        """Verify cancelling future occurrences from a cutoff date."""
        rule = {"frequency": "weekly", "weekdays": ["MO"], "count": 4}
        batch = create_recurring_reservations(
            machine_id=1,
            member_id=self.alice_member_id,
            start_date="2026-09-07",
            start_time_of_day="10:00",
            end_time_of_day="12:00",
            recurrence_rule=rule,
            actor_user=self.admin_user,
        )
        group_id = batch["recurrence_group_id"]

        # Cancel from week 3 onward (2026-09-21)
        cancel_res = cancel_recurring_series(
            recurrence_group_id=group_id,
            future_only=True,
            from_date="2026-09-21T00:00:00+02:00",
            reason="Trim series",
            actor_user=self.admin_user,
        )
        self.assertEqual(cancel_res["cancelled_count"], 2)

        series = get_recurring_series(group_id)
        self.assertEqual(series["active_occurrences"], 2)
        self.assertEqual(series["cancelled_occurrences"], 2)

    # -------------------------------------------------------------------------
    # 7. REST API Endpoints & RBAC
    # -------------------------------------------------------------------------

    def test_api_recurring_create_and_get(self):
        """Verify REST API endpoints for recurring reservations."""
        req_body = {
            "machine_id": 1,
            "member_id": self.alice_member_id,
            "date": "2026-09-07",
            "start_time": "10:00",
            "end_time": "12:00",
            "recurrence_rule": {"frequency": "weekly", "weekdays": ["MO"], "count": 3},
            "title": "API Recurring Test",
        }

        resp = self._make_request(
            method="POST",
            path="/api/reservations/recurring",
            bearer_token=self.admin_session,
            content_type="application/json",
            body=req_body,
        )
        self.assertEqual(resp.status_code, 201)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(data["success"])
        group_id = data["series"]["recurrence_group_id"]
        self.assertEqual(data["series"]["created_count"], 3)

        # GET API for series
        get_resp = self._make_request(
            method="GET",
            path=f"/api/reservations/recurring/{group_id}",
            bearer_token=self.admin_session,
        )
        self.assertEqual(get_resp.status_code, 200)
        get_data = json.loads(get_resp.body.decode("utf-8"))
        self.assertEqual(get_data["series"]["total_occurrences"], 3)

        # POST cancel API
        cancel_resp = self._make_request(
            method="POST",
            path=f"/api/reservations/recurring/{group_id}/cancel",
            bearer_token=self.admin_session,
            content_type="application/json",
            body={"reason": "API cancel"},
        )
        self.assertEqual(cancel_resp.status_code, 200)
        cancel_data = json.loads(cancel_resp.body.decode("utf-8"))
        self.assertEqual(cancel_data["result"]["cancelled_count"], 3)

    def test_api_recurring_rbac_enforcement(self):
        """Verify that viewers cannot create recurring bookings."""
        req_body = {
            "machine_id": 1,
            "member_id": self.alice_member_id,
            "date": "2026-09-07",
            "start_time": "10:00",
            "end_time": "12:00",
            "recurrence_rule": {"frequency": "weekly", "count": 2},
        }

        # Viewer attempt -> 403
        resp_viewer = self._make_request(
            method="POST",
            path="/api/reservations/recurring",
            bearer_token=self.viewer_session,
            content_type="application/json",
            body=req_body,
        )
        self.assertEqual(resp_viewer.status_code, 403)


if __name__ == "__main__":
    unittest.main()
