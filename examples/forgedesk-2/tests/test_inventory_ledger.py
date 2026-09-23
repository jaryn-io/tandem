"""Comprehensive unit, integration, RBAC, immutability, and concurrency test suite for Inventory and Movement Ledger."""

import concurrent.futures
import json
import sqlite3
import unittest
from typing import Any, Dict, List, Optional

from app import create_application_router
from forgedesk.audit.service import query_audit_logs
from forgedesk.auth.middleware import COOKIE_SESSION_NAME
from forgedesk.auth.permissions import (
    PERM_INVENTORY_CONSUME,
    PERM_INVENTORY_MANAGE,
    PERM_INVENTORY_VIEW,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    has_permission,
)
from forgedesk.auth.service import authenticate_user, create_user_session
from forgedesk.core.http import Request, Response
from forgedesk.db import get_connection, init_db, reset_database, seed_database, transaction
from forgedesk.inventory.service import (
    compute_item_stock_balances,
    create_inventory_item,
    get_inventory_item_by_id,
    get_inventory_item_by_sku,
    get_inventory_metrics,
    get_item_active_holds,
    get_item_stock_summary,
    get_ledger_history,
    list_inventory_categories,
    list_inventory_items,
    list_inventory_items_with_stock,
    record_consumption,
    record_correction,
    record_receipt,
    record_release,
    record_reservation,
    update_inventory_item,
    validate_sku,
)


class TestInventoryLedger(unittest.TestCase):
    """Test suite for ForgeDesk Inventory Management and Immutable Ledger."""

    @classmethod
    def setUpClass(cls):
        reset_database()
        init_db(seed_if_empty=False)
        seed_database(force=True)

    def setUp(self):
        self.router = create_application_router()
        self.admin = authenticate_user("admin", "admin123")
        self.operator = authenticate_user("operator", "operator123")
        self.member = authenticate_user("member_alice", "member123")
        self.viewer = authenticate_user("viewer", "viewer123")

    def _create_authenticated_request(
        self,
        method: str,
        path: str,
        user: Optional[Dict[str, Any]],
        body: Optional[Any] = None,
        is_json: bool = False,
        headers: Optional[Dict[str, str]] = None,
        use_bearer: bool = False,
    ) -> Request:
        req_headers = headers.copy() if headers else {}
        if user:
            session_token = create_user_session(user["id"], ip_address="127.0.0.1", user_agent="UnitTest/1.0")
            if use_bearer:
                req_headers["Authorization"] = f"Bearer {session_token}"
            else:
                req_headers["Cookie"] = f"{COOKIE_SESSION_NAME}={session_token}"
                from forgedesk.config import SECRET_KEY
                from forgedesk.utils.crypto import generate_csrf_token
                csrf_token = generate_csrf_token(session_token, SECRET_KEY)
                req_headers["X-CSRF-Token"] = csrf_token

        raw_body = b""
        if body is not None:
            if is_json:
                raw_body = json.dumps(body).encode("utf-8")
                req_headers["Content-Type"] = "application/json"
                req_headers["Content-Length"] = str(len(raw_body))
            elif isinstance(body, dict):
                import urllib.parse
                raw_body = urllib.parse.urlencode(body).encode("utf-8")
                req_headers["Content-Type"] = "application/x-www-form-urlencoded"
                req_headers["Content-Length"] = str(len(raw_body))
            elif isinstance(body, str):
                raw_body = body.encode("utf-8")
                req_headers["Content-Length"] = str(len(raw_body))
            elif isinstance(body, bytes):
                raw_body = body
                req_headers["Content-Length"] = str(len(raw_body))

        req = Request(
            method=method,
            path=path,
            headers=req_headers,
            body=raw_body,
            client_address=("127.0.0.1", 54321),
        )
        return req

    # -------------------------------------------------------------------------
    # 1. Item Creation, Validation & Metadata Tests
    # -------------------------------------------------------------------------

    def test_01_create_inventory_item_validation(self):
        """Verify SKU validation, field constraints, uniqueness, and audit creation."""
        # Invalid SKU format
        with self.assertRaises(ValueError):
            create_inventory_item(sku="bad sku with spaces", name="Test Item", category="3D Printing", actor=self.admin)

        with self.assertRaises(ValueError):
            create_inventory_item(sku="", name="Test Item", category="3D Printing", actor=self.admin)

        # Valid item creation
        item = create_inventory_item(
            sku="TEST-RESIN-GREY-1L",
            name="Grey UV Photopolymer 3D Resin 1L",
            category="3D Printing",
            unit="liters",
            unit_cost_cents=3500,
            minimum_stock=2,
            location="Chemical Cabinet Shelf 1",
            description="405nm standard rapid resin for MSLA printers",
            initial_stock=5,
            actor=self.admin,
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["sku"], "TEST-RESIN-GREY-1L")
        self.assertEqual(item["on_hand"], 5)
        self.assertEqual(item["available"], 5)
        self.assertEqual(item["reserved"], 0)
        self.assertFalse(item["is_low_stock"])
        self.assertEqual(item["total_value_cents"], 17500)

        # Duplicate SKU rejection
        with self.assertRaises(ValueError):
            create_inventory_item(
                sku="TEST-RESIN-GREY-1L",
                name="Duplicate SKU Attempt",
                category="3D Printing",
                actor=self.admin,
            )

        # Check audit trail
        logs, count = query_audit_logs(action="inventory.item_created", object_id="TEST-RESIN-GREY-1L")
        self.assertGreaterEqual(count, 1)
        self.assertEqual(logs[0]["object_id"], "TEST-RESIN-GREY-1L")

    def test_02_update_inventory_item(self):
        """Verify updating item details and recording audit diff."""
        item = get_inventory_item_by_sku("TEST-RESIN-GREY-1L")
        self.assertIsNotNone(item)

        updated = update_inventory_item(
            item_id=item["id"],
            name="Grey UV Photopolymer Rapid Resin 1000ml",
            minimum_stock=4,
            location="Chemical Cabinet Shelf 2",
            actor=self.operator,
        )
        self.assertEqual(updated["name"], "Grey UV Photopolymer Rapid Resin 1000ml")
        self.assertEqual(updated["minimum_stock"], 4)
        self.assertEqual(updated["location"], "Chemical Cabinet Shelf 2")

        # Verify audit log
        logs, count = query_audit_logs(action="inventory.item_updated", object_id="TEST-RESIN-GREY-1L")
        self.assertGreaterEqual(count, 1)
        self.assertEqual(logs[0]["actor_role"], ROLE_OPERATOR)

    # -------------------------------------------------------------------------
    # 2. Immutable Ledger Movements & Stock Balances Derivation
    # -------------------------------------------------------------------------

    def test_03_record_receipt_movement(self):
        """Verify recording stock receipts and derivation of on-hand & available balances."""
        item = create_inventory_item(
            sku="TEST-BRASS-ROD-8MM",
            name="Brass Round Bar 8mm x 1000mm",
            category="Metalworking",
            unit="pcs",
            unit_cost_cents=1200,
            minimum_stock=3,
            initial_stock=0,
            actor=self.admin,
        )
        self.assertEqual(item["on_hand"], 0)
        self.assertEqual(item["available"], 0)
        self.assertTrue(item["is_low_stock"])

        # Record receipt of 10 rods
        rec = record_receipt(
            item_id=item["id"],
            quantity=10,
            unit_cost_cents=1200,
            reference_type="receipt",
            reference_id="PO-2026-901",
            reason="Delivered by Metals Direct",
            actor=self.operator,
        )
        self.assertEqual(rec["quantity"], 10)
        self.assertEqual(rec["stock"]["on_hand"], 10)
        self.assertEqual(rec["stock"]["available"], 10)
        self.assertEqual(rec["stock"]["reserved"], 0)

        # Check ledger entry exists and cannot be modified
        history, total = get_ledger_history(item_id=item["id"])
        self.assertEqual(total, 1)
        self.assertEqual(history[0]["movement_type"], "receipt")
        self.assertEqual(history[0]["quantity"], 10)

    def test_04_record_reservation_and_available_stock(self):
        """Verify reserving materials decrements available stock while preserving on-hand stock."""
        item = get_inventory_item_by_sku("TEST-BRASS-ROD-8MM")
        self.assertIsNotNone(item)

        # Reserve 4 rods for reservation #101
        res1 = record_reservation(
            item_id=item["id"],
            quantity=4,
            reference_type="reservation",
            reference_id="RES-101",
            reason="Lathe turning project parts",
            actor=self.member,
        )
        self.assertEqual(res1["stock"]["on_hand"], 10)
        self.assertEqual(res1["stock"]["reserved"], 4)
        self.assertEqual(res1["stock"]["available"], 6)

        # Reserve 3 more rods for maintenance job #44
        res2 = record_reservation(
            item_id=item["id"],
            quantity=3,
            reference_type="maintenance",
            reference_id="MNT-44",
            reason="Custom bushing replacement for CNC router",
            actor=self.operator,
        )
        self.assertEqual(res2["stock"]["on_hand"], 10)
        self.assertEqual(res2["stock"]["reserved"], 7)
        self.assertEqual(res2["stock"]["available"], 3)

        # Active holds verification
        holds = get_item_active_holds(item["id"])
        self.assertEqual(len(holds), 2)
        hold_dict = {h["reference_id"]: h["active_hold_quantity"] for h in holds}
        self.assertEqual(hold_dict["RES-101"], 4)
        self.assertEqual(hold_dict["MNT-44"], 3)

    def test_05_insufficient_stock_prevention(self):
        """Verify that attempting to reserve more than available stock is strictly rejected."""
        item = get_inventory_item_by_sku("TEST-BRASS-ROD-8MM")
        # Current available is 3
        summary = get_item_stock_summary(item["id"])
        self.assertEqual(summary["available"], 3)

        with self.assertRaises(ValueError) as ctx:
            record_reservation(
                item_id=item["id"],
                quantity=5,  # 5 > 3
                reference_type="reservation",
                reference_id="RES-102",
                reason="Attempted overbooking",
                actor=self.member,
            )
        self.assertIn("Insufficient available stock", str(ctx.exception))

        # Ensure balances remained intact
        after = get_item_stock_summary(item["id"])
        self.assertEqual(after["available"], 3)
        self.assertEqual(after["reserved"], 7)
        self.assertEqual(after["on_hand"], 10)

    def test_06_record_consumption_against_reservation(self):
        """Verify consuming against an active reservation clears the hold and decreases physical on-hand stock."""
        item = get_inventory_item_by_sku("TEST-BRASS-ROD-8MM")

        # Member consumes 4 rods for RES-101
        con = record_consumption(
            item_id=item["id"],
            quantity=4,
            reference_type="reservation",
            reference_id="RES-101",
            reason="Turned 4 brass rods during lathe session",
            actor=self.member,
        )
        # On-hand drops from 10 to 6; reserved drops from 7 to 3; available remains 3!
        self.assertEqual(con["stock"]["on_hand"], 6)
        self.assertEqual(con["stock"]["reserved"], 3)
        self.assertEqual(con["stock"]["available"], 3)

        # Hold for RES-101 should now be 0 (cleared)
        holds = get_item_active_holds(item["id"])
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["reference_id"], "MNT-44")
        self.assertEqual(holds[0]["active_hold_quantity"], 3)

    def test_07_record_release_movement(self):
        """Verify releasing a reserved hold returns units back to the available pool without changing on-hand stock."""
        item = get_inventory_item_by_sku("TEST-BRASS-ROD-8MM")

        # Release 1 of the 3 rods reserved for MNT-44
        rel = record_release(
            item_id=item["id"],
            quantity=1,
            reference_type="maintenance",
            reference_id="MNT-44",
            reason="Maintenance job required only 2 rods, releasing 1",
            actor=self.operator,
        )
        # On-hand remains 6; reserved drops from 3 to 2; available rises from 3 to 4!
        self.assertEqual(rel["stock"]["on_hand"], 6)
        self.assertEqual(rel["stock"]["reserved"], 2)
        self.assertEqual(rel["stock"]["available"], 4)

        # Check invalid release (attempting to release more than held)
        with self.assertRaises(ValueError):
            record_release(
                item_id=item["id"],
                quantity=10,  # Only 2 held
                reference_type="maintenance",
                reference_id="MNT-44",
                reason="Excess release attempt",
                actor=self.operator,
            )

    def test_08_record_direct_walkin_consumption(self):
        """Verify direct walk-in consumption decrements both on-hand and available stock."""
        item = get_inventory_item_by_sku("TEST-BRASS-ROD-8MM")

        # Direct consumption of 2 units
        con = record_consumption(
            item_id=item["id"],
            quantity=2,
            reference_type="manual",
            reason="Quick workshop repair walk-in",
            actor=self.operator,
        )
        # On-hand: 6 -> 4; reserved: 2; available: 4 -> 2
        self.assertEqual(con["stock"]["on_hand"], 4)
        self.assertEqual(con["stock"]["reserved"], 2)
        self.assertEqual(con["stock"]["available"], 2)

    def test_09_record_motivated_correction(self):
        """Verify manual stock correction with mandatory justification and negative stock boundary checks."""
        item = get_inventory_item_by_sku("TEST-BRASS-ROD-8MM")

        # Missing or empty motivation must fail
        with self.assertRaises(ValueError):
            record_correction(
                item_id=item["id"],
                quantity_delta=-1,
                reason="",  # empty
                actor=self.admin,
            )

        with self.assertRaises(ValueError):
            record_correction(
                item_id=item["id"],
                quantity_delta=-1,
                reason="no",  # too short
                actor=self.admin,
            )

        # Valid negative correction: 1 damaged rod discarded
        # Current: on_hand=4, reserved=2, available=2
        corr = record_correction(
            item_id=item["id"],
            quantity_delta=-1,
            reason="Damaged rod found bent during weekly rack cleanup and discarded",
            actor=self.admin,
        )
        self.assertEqual(corr["stock"]["on_hand"], 3)
        self.assertEqual(corr["stock"]["reserved"], 2)
        self.assertEqual(corr["stock"]["available"], 1)

        # Valid positive correction: 2 rods found in overflow bin
        corr_plus = record_correction(
            item_id=item["id"],
            quantity_delta=2,
            reason="Found 2 uncataloged rods in metal stock overflow bin",
            actor=self.admin,
        )
        self.assertEqual(corr_plus["stock"]["on_hand"], 5)
        self.assertEqual(corr_plus["stock"]["reserved"], 2)
        self.assertEqual(corr_plus["stock"]["available"], 3)

        # Invalid correction that would drive available or on-hand stock below 0
        with self.assertRaises(ValueError) as ctx:
            record_correction(
                item_id=item["id"],
                quantity_delta=-4,  # available is 3, subtracting 4 would violate reserved obligations
                reason="Bulk deduction attempt",
                actor=self.admin,
            )
        self.assertIn("negative", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 3. High-Concurrency Stress Test (Zero Overbooking / Race Protection)
    # -------------------------------------------------------------------------

    def test_10_concurrent_reservations_negative_stock_protection(self):
        """
        Stress test: 20 concurrent worker threads attempting to reserve 1 unit each from a pool of 5 units.
        Exactly 5 must succeed and 15 must fail with InsufficientStock.
        Final available balance must be exactly 0, never negative.
        """
        item = create_inventory_item(
            sku="CONCUR-NOZ-04-E3D",
            name="Brass 0.4mm E3D V6 Nozzle",
            category="3D Printing",
            unit="pcs",
            unit_cost_cents=450,
            minimum_stock=1,
            initial_stock=5,  # Exactly 5 units in stock
            actor=self.admin,
        )
        item_id = item["id"]

        success_count = 0
        failure_count = 0

        def try_reserve(thread_idx: int) -> bool:
            try:
                record_reservation(
                    item_id=item_id,
                    quantity=1,
                    reference_type="reservation",
                    reference_id=f"CONCUR-RES-{thread_idx}",
                    reason=f"Concurrent reservation test worker #{thread_idx}",
                    actor=self.member,
                )
                return True
            except ValueError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(try_reserve, i) for i in range(20)]
            for f in concurrent.futures.as_completed(futures):
                if f.result():
                    success_count += 1
                else:
                    failure_count += 1

        self.assertEqual(success_count, 5, "Exactly 5 reservations must succeed.")
        self.assertEqual(failure_count, 15, "Exactly 15 reservations must fail due to exhausted stock.")

        # Inspect final derived balances
        final_stock = get_item_stock_summary(item_id)
        self.assertEqual(final_stock["on_hand"], 5)
        self.assertEqual(final_stock["reserved"], 5)
        self.assertEqual(final_stock["available"], 0)
        self.assertGreaterEqual(final_stock["available"], 0, "Available stock must never be negative.")

    # -------------------------------------------------------------------------
    # 4. RBAC & Security Permission Enforcement
    # -------------------------------------------------------------------------

    def test_11_rbac_inventory_permissions(self):
        """Verify role permissions across Admin, Operator, Member, and Viewer."""
        # Viewer can view but not create or record movements
        req_viewer_view = self._create_authenticated_request("GET", "/inventory", self.viewer)
        resp = self.router.dispatch(req_viewer_view)
        self.assertEqual(resp.status_code, 200)

        req_viewer_create = self._create_authenticated_request(
            "POST",
            "/api/inventory/items",
            self.viewer,
            {"sku": "VIEWER-FAIL", "name": "Illegal", "category": "Test"},
            is_json=True,
        )
        resp_vc = self.router.dispatch(req_viewer_create)
        self.assertEqual(resp_vc.status_code, 403)

        # Member can reserve and consume for bookings, but cannot create items or release/correct stock
        req_member_create = self._create_authenticated_request(
            "POST",
            "/api/inventory/items",
            self.member,
            {"sku": "MEMBER-FAIL", "name": "Illegal", "category": "Test"},
            is_json=True,
        )
        resp_mc = self.router.dispatch(req_member_create)
        self.assertEqual(resp_mc.status_code, 403)

        req_member_correct = self._create_authenticated_request(
            "POST",
            "/api/inventory/correct",
            self.member,
            {"item_id": 1, "quantity_delta": 5, "reason": "Unauthorized correction"},
            is_json=True,
        )
        resp_mcorr = self.router.dispatch(req_member_correct)
        self.assertEqual(resp_mcorr.status_code, 403)

        # Operator can create items, receive stock, correct stock
        req_op_create = self._create_authenticated_request(
            "POST",
            "/api/inventory/items",
            self.operator,
            {
                "sku": "OP-ACRYLIC-3MM",
                "name": "Clear Cast Acrylic Sheet 3mm 600x400",
                "category": "Laser Cutting",
                "unit": "sheet",
                "unit_cost_cents": 850,
                "minimum_stock": 5,
                "initial_stock": 10,
            },
            is_json=True,
        )
        resp_opc = self.router.dispatch(req_op_create)
        self.assertEqual(resp_opc.status_code, 201)
        data = json.loads(resp_opc.body.decode("utf-8"))
        self.assertTrue(data["success"])
        self.assertEqual(data["item"]["sku"], "OP-ACRYLIC-3MM")

    # -------------------------------------------------------------------------
    # 5. Full HTTP Web Interface & API Integration Tests
    # -------------------------------------------------------------------------

    def test_12_http_dashboard_and_detail_views(self):
        """Verify HTML views (/inventory, /inventory/{id}) render correct stock gauges, tables, and buttons."""
        req_dash = self._create_authenticated_request("GET", "/inventory", self.admin)
        resp_dash = self.router.dispatch(req_dash)
        self.assertEqual(resp_dash.status_code, 200)
        body = resp_dash.body.decode("utf-8")
        self.assertIn("Inventory Catalog & Derived Stock", body)
        self.assertIn("Recent Immutable Movement Ledger Activity", body)

        # Detail view
        item = get_inventory_item_by_sku("FIL-PLA-BLK-1KG")
        self.assertIsNotNone(item)
        req_det = self._create_authenticated_request("GET", f"/inventory/{item['id']}", self.admin)
        resp_det = self.router.dispatch(req_det)
        self.assertEqual(resp_det.status_code, 200)
        det_body = resp_det.body.decode("utf-8")
        self.assertIn("FIL-PLA-BLK-1KG", det_body)
        self.assertIn("Complete Immutable Movement Ledger", det_body)

    def test_13_api_endpoints_full_lifecycle(self):
        """Verify all JSON API endpoints (/api/inventory/*) return proper structure and error handling."""
        # 1. List items
        req_list = self._create_authenticated_request("GET", "/api/inventory/items", self.admin)
        resp_list = self.router.dispatch(req_list)
        self.assertEqual(resp_list.status_code, 200)
        data_list = json.loads(resp_list.body.decode("utf-8"))
        self.assertTrue(data_list["success"])
        self.assertGreaterEqual(data_list["count"], 5)

        # 2. Get single item
        item = data_list["items"][0]
        req_single = self._create_authenticated_request("GET", f"/api/inventory/items/{item['id']}", self.admin)
        resp_single = self.router.dispatch(req_single)
        self.assertEqual(resp_single.status_code, 200)
        data_single = json.loads(resp_single.body.decode("utf-8"))
        self.assertEqual(data_single["item"]["id"], item["id"])
        self.assertIn("active_holds", data_single)

        # 3. Query ledger history API
        req_ledger = self._create_authenticated_request("GET", f"/api/inventory/ledger?item_id={item['id']}", self.admin)
        resp_ledger = self.router.dispatch(req_ledger)
        self.assertEqual(resp_ledger.status_code, 200)
        data_ledger = json.loads(resp_ledger.body.decode("utf-8"))
        self.assertTrue(data_ledger["success"])
        self.assertIn("entries", data_ledger)


if __name__ == "__main__":
    unittest.main()
