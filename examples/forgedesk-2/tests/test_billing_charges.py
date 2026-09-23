"""Comprehensive unit and integration tests for ForgeDesk Machine Pricing, Usage Charges & Billing."""

import datetime
from decimal import Decimal
import json
import sqlite3
from typing import Any, Dict, Optional
import unittest

from forgedesk.auth.service import authenticate_user, create_user_session, get_user_by_id
from forgedesk.billing.handlers import register_billing_routes
from forgedesk.billing.service import (
    calculate_usage_charge,
    compute_peak_offpeak_minutes,
    create_charge_adjustment,
    create_usage_charge_for_checkout,
    format_cents_currency,
    get_billing_summary,
    get_usage_charge_by_id,
    get_usage_charge_by_reservation_id,
    list_charge_adjustments,
    list_usage_charges,
    parse_currency_to_cents,
)
from forgedesk.core.http import Request
from forgedesk.core.router import Router
from forgedesk.db.connection import get_connection, query_all, query_one, transaction
from forgedesk.db.migrations import apply_migrations, reset_database
from forgedesk.db.seed import seed_database
from forgedesk.machines.service import create_machine, get_machine_by_id, update_machine
from forgedesk.members.service import create_member, get_member_by_id
from forgedesk.reservations.service import (
    check_in_reservation,
    check_out_reservation,
    create_reservation,
    get_reservation_by_id,
)
from forgedesk.utils.datetime_tz import (
    APP_TIMEZONE,
    format_iso,
    now_rome,
    now_rome_iso,
    parse_datetime,
    today_rome_str,
)


def make_request(
    method: str,
    path: str,
    user: Optional[Dict[str, Any]] = None,
    body: bytes = b"",
    headers: Optional[Dict[str, str]] = None,
    route_params: Optional[Dict[str, str]] = None,
) -> Request:
    """Helper to instantiate Request and bind user context."""
    req = Request(
        method=method,
        path=path,
        headers=headers or {},
        body=body,
    )
    req.user = user
    if route_params:
        req.route_params = route_params
    return req


class TestBillingCharges(unittest.TestCase):
    """Test suite covering pricing calculation, peak/off-peak windows, checkout finalization, and adjustments."""

    @classmethod
    def setUpClass(cls):
        reset_database()
        apply_migrations()
        seed_database(force=True)

    def setUp(self):
        reset_database()
        apply_migrations()
        seed_database(force=True)

        self.admin_user = get_user_by_id(1)
        self.operator_user = get_user_by_id(2)
        self.alice_user = get_user_by_id(3)  # Member Alice (member_id: 1)
        self.bob_user = get_user_by_id(4)    # Member Bob (member_id: 2)
        self.viewer_user = get_user_by_id(6) # Viewer

        self.alice_member_id = 1
        self.bob_member_id = 2

        # Setup test router
        self.router = Router()
        register_billing_routes(self.router)

    # -------------------------------------------------------------------------
    # 1. Financial Precision & Exact Decimal Arithmetic
    # -------------------------------------------------------------------------
    def test_01_decimal_pricing_accuracy_no_float_rounding_errors(self):
        """Verify exact decimal arithmetic with no float rounding inaccuracies."""
        # 1. Helper formatting
        self.assertEqual(format_cents_currency(1250), "€12.50")
        self.assertEqual(format_cents_currency(0), "€0.00")
        self.assertEqual(format_cents_currency(-500), "-€5.00")
        self.assertEqual(format_cents_currency(99), "€0.99")

        # 2. Currency parsing
        self.assertEqual(parse_currency_to_cents("12.50"), 1250)
        self.assertEqual(parse_currency_to_cents("€12.50"), 1250)
        self.assertEqual(parse_currency_to_cents("-5.00"), -500)
        self.assertEqual(parse_currency_to_cents("0.99"), 99)
        self.assertEqual(parse_currency_to_cents("100"), 10000)

        with self.assertRaises(ValueError):
            parse_currency_to_cents("")
        with self.assertRaises(ValueError):
            parse_currency_to_cents("invalid_amt")

        # 3. Fractional hour precision: 33 minutes at €6.00/hr (600 cents/hr)
        # 33/60 * 600 = 330.0 cents
        machine_rates = {
            "hourly_rate_cents": 600,
            "peak_hourly_rate_cents": 600,
            "minimum_charge_cents": 100,
            "peak_hours_start": "17:00",
            "peak_hours_end": "21:00",
        }
        res = calculate_usage_charge(
            machine_or_id=1,
            start_time="2026-09-10T10:00:00+02:00",
            end_time="2026-09-10T10:33:00+02:00",
            rates_override=machine_rates,
        )
        self.assertEqual(res["duration_minutes"], 33)
        self.assertEqual(res["base_charge_cents"], 330)
        self.assertEqual(res["final_charge_cents"], 330)

        # 4. Odd minute rounding (e.g. 7 minutes at €3.50/hr = 350 cents)
        # 7 / 60 * 350 = 40.83333... -> rounds to 41 cents
        machine_rates2 = {
            "hourly_rate_cents": 350,
            "peak_hourly_rate_cents": 350,
            "minimum_charge_cents": 0,
            "peak_hours_start": "17:00",
            "peak_hours_end": "21:00",
        }
        res2 = calculate_usage_charge(
            machine_or_id=1,
            start_time="2026-09-10T10:00:00+02:00",
            end_time="2026-09-10T10:07:00+02:00",
            rates_override=machine_rates2,
        )
        self.assertEqual(res2["duration_minutes"], 7)
        self.assertEqual(res2["base_charge_cents"], 41)
        self.assertEqual(res2["final_charge_cents"], 41)

    # -------------------------------------------------------------------------
    # 2. Peak & Off-Peak Rate Calculations
    # -------------------------------------------------------------------------
    def test_02_peak_offpeak_same_day_intervals(self):
        """Verify peak and off-peak rate calculation for same-day bookings."""
        # Machine rates: offpeak €3.00 (300c), peak €5.00 (500c), peak window 17:00 - 21:00
        rates = {
            "hourly_rate_cents": 300,
            "peak_hourly_rate_cents": 500,
            "minimum_charge_cents": 100,
            "peak_hours_start": "17:00",
            "peak_hours_end": "21:00",
        }

        # Case A: 2 hours pure off-peak (10:00 to 12:00) -> 2 * 300 = 600 cents (€6.00)
        c_a = calculate_usage_charge(1, "2026-09-10T10:00:00+02:00", "2026-09-10T12:00:00+02:00", rates_override=rates)
        self.assertEqual(c_a["offpeak_minutes"], 120)
        self.assertEqual(c_a["peak_minutes"], 0)
        self.assertEqual(c_a["offpeak_cost_cents"], 600)
        self.assertEqual(c_a["peak_cost_cents"], 0)
        self.assertEqual(c_a["final_charge_cents"], 600)

        # Case B: 2 hours pure peak (18:00 to 20:00) -> 2 * 500 = 1000 cents (€10.00)
        c_b = calculate_usage_charge(1, "2026-09-10T18:00:00+02:00", "2026-09-10T20:00:00+02:00", rates_override=rates)
        self.assertEqual(c_b["offpeak_minutes"], 0)
        self.assertEqual(c_b["peak_minutes"], 120)
        self.assertEqual(c_b["offpeak_cost_cents"], 0)
        self.assertEqual(c_b["peak_cost_cents"], 1000)
        self.assertEqual(c_b["final_charge_cents"], 1000)

        # Case C: Straddling boundary (16:30 to 18:30)
        # 16:30 - 17:00 = 30m offpeak (30/60 * 300 = 150 cents)
        # 17:00 - 18:30 = 90m peak (90/60 * 500 = 750 cents)
        # Total base = 150 + 750 = 900 cents (€9.00)
        c_c = calculate_usage_charge(1, "2026-09-10T16:30:00+02:00", "2026-09-10T18:30:00+02:00", rates_override=rates)
        self.assertEqual(c_c["offpeak_minutes"], 30)
        self.assertEqual(c_c["peak_minutes"], 90)
        self.assertEqual(c_c["offpeak_cost_cents"], 150)
        self.assertEqual(c_c["peak_cost_cents"], 750)
        self.assertEqual(c_c["base_charge_cents"], 900)
        self.assertEqual(c_c["final_charge_cents"], 900)

    # -------------------------------------------------------------------------
    # 3. Minimum Charge Threshold
    # -------------------------------------------------------------------------
    def test_03_minimum_charge_threshold(self):
        """Verify minimum charge threshold applies when calculated usage is below minimum."""
        rates = {
            "hourly_rate_cents": 300,
            "peak_hourly_rate_cents": 300,
            "minimum_charge_cents": 200,  # Minimum €2.00
            "peak_hours_start": "17:00",
            "peak_hours_end": "21:00",
        }

        # 10 minutes usage at €3.00/hr = 50 cents base charge
        # Should be raised to minimum charge 200 cents
        c = calculate_usage_charge(1, "2026-09-10T10:00:00+02:00", "2026-09-10T10:10:00+02:00", rates_override=rates)
        self.assertEqual(c["duration_minutes"], 10)
        self.assertEqual(c["base_charge_cents"], 50)
        self.assertEqual(c["final_charge_cents"], 200)
        self.assertTrue(c["minimum_charge_applied"])

        # 60 minutes usage at €3.00/hr = 300 cents base charge > minimum 200
        c2 = calculate_usage_charge(1, "2026-09-10T10:00:00+02:00", "2026-09-10T11:00:00+02:00", rates_override=rates)
        self.assertEqual(c2["base_charge_cents"], 300)
        self.assertEqual(c2["final_charge_cents"], 300)
        self.assertFalse(c2["minimum_charge_applied"])

    # -------------------------------------------------------------------------
    # 4. Multi-Day and Overnight Intervals
    # -------------------------------------------------------------------------
    def test_04_multi_day_interval_breakdown(self):
        """Verify peak hours are calculated across multi-day intervals correctly in Europe/Rome."""
        # Start 20:00 on Day 1, End 02:00 on Day 2.
        # Peak hours 17:00 to 21:00.
        # Day 1: 20:00 to 21:00 = 60 min peak. 21:00 to 24:00 = 180 min offpeak.
        # Day 2: 00:00 to 02:00 = 120 min offpeak.
        # Total: 60 min peak, 300 min offpeak (total 360 min = 6 hours).
        s = parse_datetime("2026-09-10T20:00:00+02:00")
        e = parse_datetime("2026-09-11T02:00:00+02:00")
        offp, pk = compute_peak_offpeak_minutes(s, e, "17:00", "21:00")
        self.assertEqual(pk, 60)
        self.assertEqual(offp, 300)

    # -------------------------------------------------------------------------
    # 5. Checkout Finalization & Snapshot Immutability
    # -------------------------------------------------------------------------
    def test_05_checkout_creates_immutable_snapshot(self):
        """Verify checkout creates finalized usage charge that remains unchanged when machine rates change."""
        today = today_rome_str()
        # Create a single reservation for Alice on machine 1 (Prusa MK4: €2.00/hr, peak €3.00, min €1.00)
        res = create_reservation(
            machine_id=1,
            member_id=self.alice_member_id,
            start_time=f"{today}T10:00:00+02:00",
            end_time=f"{today}T12:00:00+02:00",
            title="Prusa 3D Print Job",
            actor_user=self.admin_user,
        )

        # Check in
        check_in_time = f"{today}T10:00:00+02:00"
        check_in_reservation(res["id"], actor_user=self.admin_user, actual_check_in_time=check_in_time)

        # Check out (2 hours usage from 10:00 to 12:00 -> 120 min offpeak at €2.00/hr = €4.00 = 400 cents)
        check_out_time = f"{today}T12:00:00+02:00"
        checked_out = check_out_reservation(
            res["id"],
            actor_user=self.admin_user,
            actual_check_out_time=check_out_time,
            notes="Completed print with PETG.",
        )

        self.assertEqual(checked_out["status"], "checked_out")
        self.assertIsNotNone(checked_out.get("usage_charge"))

        charge = get_usage_charge_by_reservation_id(res["id"])
        self.assertIsNotNone(charge)
        self.assertEqual(charge["base_charge_cents"], 400)
        self.assertEqual(charge["final_charge_cents"], 400)
        self.assertEqual(charge["status"], "finalized")
        self.assertEqual(charge["duration_minutes"], 120)

        # IMMUTABILITY TEST:
        # Now alter the machine's rates drastically (e.g. increase to €10.00/hr)
        update_machine(
            machine_id=1,
            hourly_rate_cents=1000,
            peak_hourly_rate_cents=1500,
            minimum_charge_cents=500,
            actor=self.admin_user,
        )

        # Re-fetch the previously finalized usage charge
        charge_after = get_usage_charge_by_id(charge["id"])
        self.assertEqual(charge_after["base_charge_cents"], 400, "Base charge must not change when machine rates update!")
        self.assertEqual(charge_after["final_charge_cents"], 400, "Final charge must not change when machine rates update!")
        self.assertEqual(charge_after["rate_breakdown"]["hourly_rate_cents"], 200, "Snapshot rates must remain preserved!")

    # -------------------------------------------------------------------------
    # 6. Admin Explicit Adjustments
    # -------------------------------------------------------------------------
    def test_06_admin_creates_explicit_adjustments(self):
        """Verify Administrator can create adjustments with reasons, preserving base charge."""
        today = today_rome_str()
        res = create_reservation(
            machine_id=1,
            member_id=self.alice_member_id,
            start_time=f"{today}T14:00:00+02:00",
            end_time=f"{today}T16:00:00+02:00",
            actor_user=self.admin_user,
        )
        check_in_reservation(res["id"], actor_user=self.admin_user, actual_check_in_time=f"{today}T14:00:00+02:00")
        check_out_reservation(res["id"], actor_user=self.admin_user, actual_check_out_time=f"{today}T16:00:00+02:00")

        charge = get_usage_charge_by_reservation_id(res["id"])
        charge_id = charge["id"]
        original_base = charge["base_charge_cents"]  # 400 cents

        # 1. Admin applies -€1.50 discount (-150 cents)
        adj1 = create_charge_adjustment(
            charge_id=charge_id,
            adjustment_cents=-150,
            reason="Makerspace community volunteer discount",
            actor_user=self.admin_user,
        )

        self.assertEqual(adj1["status"], "adjusted")
        self.assertEqual(adj1["base_charge_cents"], original_base, "Original base charge must remain unmodified!")
        self.assertEqual(adj1["final_charge_cents"], 250, "Final charge should be 400 - 150 = 250 cents")
        self.assertEqual(adj1["adjustments_count"], 1)

        # 2. Admin applies +€0.50 surcharge (+50 cents)
        adj2 = create_charge_adjustment(
            charge_id=charge_id,
            adjustment_cents=50,
            reason="Tool wear fee",
            actor_user=self.admin_user,
        )
        self.assertEqual(adj2["base_charge_cents"], original_base)
        self.assertEqual(adj2["final_charge_cents"], 300, "Final charge should be 400 - 150 + 50 = 300 cents")
        self.assertEqual(adj2["adjustments_count"], 2)

        # Verify adjustment list
        adjs = list_charge_adjustments(charge_id=charge_id)
        self.assertEqual(len(adjs), 2)
        self.assertEqual(adjs[0]["adjustment_cents"], 50)   # latest first in query
        self.assertEqual(adjs[1]["adjustment_cents"], -150)

    # -------------------------------------------------------------------------
    # 7. Non-Admin Cannot Adjust Charges (RBAC)
    # -------------------------------------------------------------------------
    def test_07_non_admin_cannot_create_adjustments(self):
        """Verify Operators, Members, and Viewers cannot adjust charges."""
        today = today_rome_str()
        res = create_reservation(
            machine_id=1,
            member_id=self.alice_member_id,
            start_time=f"{today}T14:00:00+02:00",
            end_time=f"{today}T15:00:00+02:00",
            actor_user=self.admin_user,
        )
        check_in_reservation(res["id"], actor_user=self.admin_user, actual_check_in_time=f"{today}T14:00:00+02:00")
        check_out_reservation(res["id"], actor_user=self.admin_user, actual_check_out_time=f"{today}T15:00:00+02:00")

        charge = get_usage_charge_by_reservation_id(res["id"])
        charge_id = charge["id"]

        # Operator forbidden
        with self.assertRaises(PermissionError):
            create_charge_adjustment(charge_id, -100, "Operator discount", actor_user=self.operator_user)

        # Member forbidden
        with self.assertRaises(PermissionError):
            create_charge_adjustment(charge_id, -100, "Member self discount", actor_user=self.alice_user)

        # Viewer forbidden
        with self.assertRaises(PermissionError):
            create_charge_adjustment(charge_id, -100, "Viewer discount", actor_user=self.viewer_user)

    # -------------------------------------------------------------------------
    # 8. Adjustment Validation Rules
    # -------------------------------------------------------------------------
    def test_08_adjustment_validation_rules(self):
        """Verify validation errors for zero adjustment, empty reason, or invalid charge ID."""
        today = today_rome_str()
        res = create_reservation(
            machine_id=1,
            member_id=self.alice_member_id,
            start_time=f"{today}T14:00:00+02:00",
            end_time=f"{today}T15:00:00+02:00",
            actor_user=self.admin_user,
        )
        check_in_reservation(res["id"], actor_user=self.admin_user, actual_check_in_time=f"{today}T14:00:00+02:00")
        check_out_reservation(res["id"], actor_user=self.admin_user, actual_check_out_time=f"{today}T15:00:00+02:00")
        charge = get_usage_charge_by_reservation_id(res["id"])

        # Zero amount
        with self.assertRaises(ValueError):
            create_charge_adjustment(charge["id"], 0, "Zero adjust", actor_user=self.admin_user)

        # Empty reason
        with self.assertRaises(ValueError):
            create_charge_adjustment(charge["id"], -100, "", actor_user=self.admin_user)

        # Non-existent charge
        with self.assertRaises(ValueError):
            create_charge_adjustment(999999, -100, "Non-existent", actor_user=self.admin_user)

    # -------------------------------------------------------------------------
    # 9. Audit Trail Logging for Charges and Adjustments
    # -------------------------------------------------------------------------
    def test_09_audit_trail_recorded(self):
        """Verify audit_log records append-only events for charge:created and charge:adjusted."""
        today = today_rome_str()
        res = create_reservation(
            machine_id=1,
            member_id=self.alice_member_id,
            start_time=f"{today}T10:00:00+02:00",
            end_time=f"{today}T11:00:00+02:00",
            actor_user=self.admin_user,
        )
        check_in_reservation(res["id"], actor_user=self.admin_user, actual_check_in_time=f"{today}T10:00:00+02:00")
        check_out_reservation(res["id"], actor_user=self.admin_user, actual_check_out_time=f"{today}T11:00:00+02:00")

        charge = get_usage_charge_by_reservation_id(res["id"])
        charge_id = charge["id"]

        # Check charge:created audit event
        audit_create = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'usage_charge' AND object_id = ? AND action = 'charge:created';",
            (str(charge_id),),
        )
        self.assertIsNotNone(audit_create)
        self.assertEqual(audit_create["actor_id"], 1)

        # Apply adjustment
        create_charge_adjustment(charge_id, -50, "Audit test discount", actor_user=self.admin_user)

        # Check charge:adjusted audit event
        audit_adj = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'usage_charge' AND object_id = ? AND action = 'charge:adjusted';",
            (str(charge_id),),
        )
        self.assertIsNotNone(audit_adj)
        self.assertIn("Audit test discount", audit_adj["details_json"])

    # -------------------------------------------------------------------------
    # 10. RBAC Isolation for Members
    # -------------------------------------------------------------------------
    def test_10_member_charges_rbac_isolation(self):
        """Verify Members can only view their own charges, while Staff can view all."""
        today = today_rome_str()
        # Seed has 1 charge for Alice (member 1)
        # Create a charge for Bob (member 2)
        res_bob = create_reservation(
            machine_id=2,
            member_id=self.bob_member_id,
            start_time=f"{today}T16:00:00+02:00",
            end_time=f"{today}T17:00:00+02:00",
            actor_user=self.admin_user,
        )
        check_in_reservation(res_bob["id"], actor_user=self.admin_user, actual_check_in_time=f"{today}T16:00:00+02:00")
        check_out_reservation(res_bob["id"], actor_user=self.admin_user, actual_check_out_time=f"{today}T17:00:00+02:00")
        bob_charge = get_usage_charge_by_reservation_id(res_bob["id"])

        # 1. Alice listing charges via service gets only Alice's charges
        alice_charges, count, _ = list_usage_charges(actor_user=self.alice_user)
        for c in alice_charges:
            self.assertEqual(c["member_id"], self.alice_member_id)

        # 2. Alice accessing Bob's charge via API gets 403 Forbidden
        req_bob = make_request(
            method="GET",
            path=f"/api/charges/{bob_charge['id']}",
            user=self.alice_user,
            route_params={"id": str(bob_charge["id"])},
        )
        resp_bob = self.router.dispatch(req_bob)
        self.assertEqual(resp_bob.status_code, 403)

        # 3. Bob accessing his own charge via API gets 200 OK
        req_bob_own = make_request(
            method="GET",
            path=f"/api/charges/{bob_charge['id']}",
            user=self.bob_user,
            route_params={"id": str(bob_charge["id"])},
        )
        resp_bob_own = self.router.dispatch(req_bob_own)
        self.assertEqual(resp_bob_own.status_code, 200)

        # 4. Operator accessing Bob's charge gets 200 OK
        req_op = make_request(
            method="GET",
            path=f"/api/charges/{bob_charge['id']}",
            user=self.operator_user,
            route_params={"id": str(bob_charge["id"])},
        )
        resp_op = self.router.dispatch(req_op)
        self.assertEqual(resp_op.status_code, 200)

    # -------------------------------------------------------------------------
    # 11. REST API Endpoints
    # -------------------------------------------------------------------------
    def test_11_rest_api_endpoints(self):
        """Test GET /api/charges, POST /api/charges/{id}/adjustments, POST /api/charges/estimate, GET /api/charges/summary."""
        # 1. GET /api/charges
        req = make_request(method="GET", path="/api/charges", user=self.admin_user)
        resp = self.router.dispatch(req)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertIn("charges", data)
        self.assertIn("summary", data)

        # 2. POST /api/charges/estimate
        today = today_rome_str()
        est_req = make_request(
            method="POST",
            path="/api/charges/estimate",
            user=self.alice_user,
            body=json.dumps({
                "machine_id": 1,
                "start_time": f"{today}T10:00:00+02:00",
                "end_time": f"{today}T11:30:00+02:00",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        est_resp = self.router.dispatch(est_req)
        self.assertEqual(est_resp.status_code, 200)
        est_data = json.loads(est_resp.body.decode("utf-8"))
        self.assertEqual(est_data["estimate"]["duration_minutes"], 90)

        # 3. POST /api/charges/1/adjustments (Admin)
        adj_req = make_request(
            method="POST",
            path="/api/charges/1/adjustments",
            user=self.admin_user,
            route_params={"id": "1"},
            body=json.dumps({
                "adjustment_cents": -50,
                "reason": "API promotional discount",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        adj_resp = self.router.dispatch(adj_req)
        self.assertEqual(adj_resp.status_code, 200)
        adj_data = json.loads(adj_resp.body.decode("utf-8"))
        self.assertTrue(adj_data["success"])

        # 4. GET /api/charges/summary
        sum_req = make_request(method="GET", path="/api/charges/summary", user=self.admin_user)
        sum_resp = self.router.dispatch(sum_req)
        self.assertEqual(sum_resp.status_code, 200)
        sum_data = json.loads(sum_resp.body.decode("utf-8"))
        self.assertIn("total_charges_count", sum_data)
        self.assertIn("formatted_total_final", sum_data)

    # -------------------------------------------------------------------------
    # 12. HTML Views
    # -------------------------------------------------------------------------
    def test_12_html_views_rendering(self):
        """Test GET /charges and GET /charges/{id} HTML rendering."""
        # 1. GET /charges
        req_list = make_request(method="GET", path="/charges", user=self.admin_user)
        resp_list = self.router.dispatch(req_list)
        self.assertEqual(resp_list.status_code, 200)
        self.assertIn(b"Charges &amp; Billing", resp_list.body)
        self.assertIn(b"Total Charges Billed", resp_list.body)

        # 2. GET /charges/1
        req_detail = make_request(
            method="GET",
            path="/charges/1",
            user=self.admin_user,
            route_params={"id": "1"},
        )
        resp_detail = self.router.dispatch(req_detail)
        self.assertEqual(resp_detail.status_code, 200)
        self.assertIn(b"Usage Charge #1 Details", resp_detail.body)
        self.assertIn(b"Rate Snapshot at Checkout", resp_detail.body)
        self.assertIn(b"Financial Adjustments Ledger", resp_detail.body)
        self.assertIn(b"Issue Explicit Adjustment", resp_detail.body)

    # -------------------------------------------------------------------------
    # 13. FD-SEC-001: Check-in/Check-out Client Timestamp Tampering Prevention
    # -------------------------------------------------------------------------
    def test_13_security_fd_sec_001_checkin_checkout_timestamp_tampering(self):
        """Verify that member client-supplied timestamps are ignored in favor of server time,
        while operator/admin manual timestamps are validated and explicitly audited.
        """
        today = today_rome_str()
        res = create_reservation(
            machine_id=1,
            member_id=self.alice_member_id,
            start_time=f"{today}T08:00:00+02:00",
            end_time=f"{today}T12:00:00+02:00",
            title="Timestamp Spoofing Security Test",
            actor_user=self.alice_user,
        )

        # 1. Member attempts to pass an arbitrary spoofed past check-in time
        spoofed_checkin = f"{today}T06:00:00+02:00"
        checked_in = check_in_reservation(
            reservation_id=res["id"],
            actor_user=self.alice_user,
            actual_check_in_time=spoofed_checkin,
        )
        self.assertEqual(checked_in["status"], "checked_in")
        # Assert that the member's spoofed check-in was IGNORED and server time was used
        self.assertNotEqual(checked_in["actual_check_in"], spoofed_checkin)

        # Check audit event has server_time_enforced = True
        audit_in = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'reservation' AND object_id = ? AND action = 'reservation:check_in';",
            (str(res["id"]),),
        )
        self.assertIsNotNone(audit_in)
        audit_in_details = json.loads(audit_in["details_json"])
        self.assertTrue(audit_in_details.get("server_time_enforced"))
        self.assertFalse(audit_in_details.get("manual_timestamp_override"))

        # 2. Member attempts to pass an arbitrary spoofed check-out time
        spoofed_checkout = f"{today}T06:05:00+02:00"
        checked_out = check_out_reservation(
            reservation_id=res["id"],
            actor_user=self.alice_user,
            actual_check_out_time=spoofed_checkout,
        )
        self.assertEqual(checked_out["status"], "checked_out")
        self.assertNotEqual(checked_out["actual_check_out"], spoofed_checkout)

        # Check audit event has server_time_enforced = True
        audit_out = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'reservation' AND object_id = ? AND action = 'reservation:check_out';",
            (str(res["id"]),),
        )
        self.assertIsNotNone(audit_out)
        audit_out_details = json.loads(audit_out["details_json"])
        self.assertTrue(audit_out_details.get("server_time_enforced"))
        self.assertFalse(audit_out_details.get("manual_timestamp_override"))

        # 3. Admin future timestamp beyond tolerance raises ValueError
        res_admin = create_reservation(
            machine_id=1,
            member_id=self.bob_member_id,
            start_time=f"{today}T08:00:00+02:00",
            end_time=f"{today}T12:00:00+02:00",
            title="Admin Manual Timestamp Test",
            actor_user=self.admin_user,
        )
        future_time = format_iso(now_rome() + datetime.timedelta(days=2))
        with self.assertRaises(ValueError) as ctx:
            check_in_reservation(
                reservation_id=res_admin["id"],
                actor_user=self.admin_user,
                actual_check_in_time=future_time,
            )
        self.assertIn("future", str(ctx.exception).lower())

    # -------------------------------------------------------------------------
    # 14. FD-SEC-002: Rates Override RBAC & Simulation Separation
    # -------------------------------------------------------------------------
    def test_14_security_fd_sec_002_estimate_rates_override_rbac_and_simulation(self):
        """Verify that rates_override in estimates is restricted to Admin/Operator,
        validated, flagged as non-binding simulation, and ignored during checkout.
        """
        today = today_rome_str()

        # 1. Member calling /api/charges/estimate with rates_override is rejected with 403
        member_override_req = make_request(
            method="POST",
            path="/api/charges/estimate",
            user=self.alice_user,
            body=json.dumps({
                "machine_id": 1,
                "start_time": f"{today}T10:00:00+02:00",
                "end_time": f"{today}T11:00:00+02:00",
                "rates_override": {"hourly_rate_cents": 1},
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp_403 = self.router.dispatch(member_override_req)
        self.assertEqual(resp_403.status_code, 403)
        data_403 = json.loads(resp_403.body.decode("utf-8"))
        self.assertEqual(data_403.get("code"), "rates_override_unauthorized")

        # 2. Admin calling with rates_override succeeds and returns simulation metadata
        admin_override_req = make_request(
            method="POST",
            path="/api/charges/estimate",
            user=self.admin_user,
            body=json.dumps({
                "machine_id": 1,
                "start_time": f"{today}T10:00:00+02:00",
                "end_time": f"{today}T11:00:00+02:00",
                "rates_override": {
                    "hourly_rate_cents": 9900,
                    "peak_hourly_rate_cents": 12000,
                    "minimum_charge_cents": 500,
                },
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp_admin = self.router.dispatch(admin_override_req)
        self.assertEqual(resp_admin.status_code, 200)
        data_admin = json.loads(resp_admin.body.decode("utf-8"))
        estimate = data_admin["estimate"]
        self.assertTrue(estimate["is_simulation"])
        self.assertEqual(estimate["rates_source"], "override_simulation")
        self.assertIsNotNone(estimate["disclaimer"])
        self.assertEqual(estimate["hourly_rate_cents"], 9900)

        # 3. Invalid rates_override (negative rate or invalid peak hours) raises 400
        admin_invalid_req = make_request(
            method="POST",
            path="/api/charges/estimate",
            user=self.admin_user,
            body=json.dumps({
                "machine_id": 1,
                "start_time": f"{today}T10:00:00+02:00",
                "end_time": f"{today}T11:00:00+02:00",
                "rates_override": {"hourly_rate_cents": -500},
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp_invalid = self.router.dispatch(admin_invalid_req)
        self.assertEqual(resp_invalid.status_code, 400)

        # 4. Standard member estimate without rates_override succeeds with server_database source
        member_std_req = make_request(
            method="POST",
            path="/api/charges/estimate",
            user=self.alice_user,
            body=json.dumps({
                "machine_id": 1,
                "start_time": f"{today}T10:00:00+02:00",
                "end_time": f"{today}T11:00:00+02:00",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp_std = self.router.dispatch(member_std_req)
        self.assertEqual(resp_std.status_code, 200)
        data_std = json.loads(resp_std.body.decode("utf-8"))
        self.assertFalse(data_std["estimate"]["is_simulation"])
        self.assertEqual(data_std["estimate"]["rates_source"], "server_database")
        self.assertIsNone(data_std["estimate"]["disclaimer"])

    # -------------------------------------------------------------------------
    # 15. FD-SEC-003: Database-Level Immutability Triggers
    # -------------------------------------------------------------------------
    def test_15_security_fd_sec_003_database_level_immutability_triggers(self):
        """Verify that SQLite triggers protect usage_charges snapshots,
        charge_adjustments, and inventory_ledger from direct SQL modifications and deletions.
        """
        conn = get_connection()
        try:
            # 1. Attempting direct SQL DELETE on usage_charges is aborted by trigger
            with self.assertRaises((sqlite3.OperationalError, sqlite3.IntegrityError)) as ctx_del:
                conn.execute("DELETE FROM usage_charges WHERE id = 1;")
            self.assertIn("Usage charges are immutable financial records", str(ctx_del.exception))

            # 2. Attempting direct SQL UPDATE on snapshot fields (base_charge_cents) is aborted
            with self.assertRaises((sqlite3.OperationalError, sqlite3.IntegrityError)) as ctx_upd:
                conn.execute("UPDATE usage_charges SET base_charge_cents = 99999 WHERE id = 1;")
            self.assertIn("Usage charge original rate snapshot fields are immutable", str(ctx_upd.exception))

            # 3. Direct SQL UPDATE on rate_breakdown_json is aborted
            with self.assertRaises((sqlite3.OperationalError, sqlite3.IntegrityError)) as ctx_json:
                conn.execute("UPDATE usage_charges SET rate_breakdown_json = '{}' WHERE id = 1;")
            self.assertIn("Usage charge original rate snapshot fields are immutable", str(ctx_json.exception))

            # 4. Attempting direct SQL DELETE or UPDATE on charge_adjustments is aborted
            with self.assertRaises((sqlite3.OperationalError, sqlite3.IntegrityError)) as ctx_adj_del:
                conn.execute("DELETE FROM charge_adjustments WHERE id = 1;")
            self.assertIn("Charge adjustments ledger is append-only", str(ctx_adj_del.exception))

            # 5. Direct SQL DELETE or UPDATE on inventory_ledger is aborted
            with self.assertRaises((sqlite3.OperationalError, sqlite3.IntegrityError)) as ctx_inv_del:
                conn.execute("DELETE FROM inventory_ledger WHERE id = 1;")
            self.assertIn("Inventory ledger is append-only", str(ctx_inv_del.exception))

            with self.assertRaises((sqlite3.OperationalError, sqlite3.IntegrityError)) as ctx_inv_upd:
                conn.execute("UPDATE inventory_ledger SET quantity = 999 WHERE id = 1;")
            self.assertIn("Inventory ledger is append-only", str(ctx_inv_upd.exception))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
