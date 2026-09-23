"""Comprehensive unit and integration tests for ForgeDesk CSV Import/Export, Backup, and Restore (Step S19)."""

import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from app import create_application_router
from forgedesk.auth.service import authenticate_user, create_user_session
from forgedesk.config import BACKUP_DIR, DB_PATH
from forgedesk.core.http import Request, Response
from forgedesk.db.connection import get_connection, query_all, query_one
from forgedesk.db.migrations import apply_migrations, reset_database
from forgedesk.db.seed import seed_database
from forgedesk.system.service import (
    create_database_backup,
    execute_csv_import,
    export_charges_csv,
    export_charges_json,
    export_full_system_json,
    export_inventory_csv,
    export_inventory_json,
    export_inventory_ledger_csv,
    export_machines_csv,
    export_machines_json,
    export_members_csv,
    export_members_json,
    export_reservations_csv,
    export_reservations_json,
    get_sample_csv,
    preview_csv_import,
    restore_database_backup,
    validate_backup_file,
)
from forgedesk.utils.crypto import generate_csrf_token


class TestSystemImportExportBackup(unittest.TestCase):
    """Test suite for CSV bulk import with preview/validation/rollback, exports, and backup/restore."""

    @classmethod
    def setUpClass(cls):
        reset_database()
        apply_migrations()
        seed_database(force=True)

        cls.router = create_application_router()

        # Demo credentials
        cls.admin = authenticate_user("admin", "admin123")
        cls.admin_session = create_user_session(cls.admin["id"])

        cls.operator = authenticate_user("operator", "operator123")
        cls.operator_session = create_user_session(cls.operator["id"])

        cls.member = authenticate_user("member_alice", "member123")
        cls.member_session = create_user_session(cls.member["id"])

        cls.viewer = authenticate_user("viewer", "viewer123")
        cls.viewer_session = create_user_session(cls.viewer["id"])

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

    # =========================================================================
    # 1. CSV Import Preview & Row-by-Row Validation Tests
    # =========================================================================

    def test_01_members_csv_preview_valid_and_invalid_rows(self):
        """Test Members CSV preview with row-by-row diagnostics."""
        csv_content = (
            "full_name,email,phone,membership_status,membership_expiry,notes,qualifications\n"
            '"Lucia Neri","lucia.neri@test.it","+39 340 9988776","active","2026-12-31","Fine maker","3d_printers:2026-12-31;laser_cutters"\n'
            '"Bad Email User","notanemail","123","invalid_status","bad-date","","nonexistent_category"\n'
            '"","missing.name@test.it","","active","","",""\n'
        )

        preview = preview_csv_import("members", csv_content, actor_user=self.admin)
        self.assertEqual(preview["entity_type"], "members")
        self.assertEqual(preview["total_rows"], 3)
        self.assertEqual(preview["valid_rows_count"], 1)
        self.assertEqual(preview["invalid_rows_count"], 2)
        self.assertFalse(preview["can_import"])

        # Row 1 is valid
        row1 = preview["rows"][0]
        self.assertTrue(row1["is_valid"])
        self.assertEqual(len(row1["errors"]), 0)
        self.assertEqual(row1["parsed"]["full_name"], "Lucia Neri")
        self.assertEqual(len(row1["parsed"]["qualifications"]), 2)

        # Row 2 is invalid
        row2 = preview["rows"][1]
        self.assertFalse(row2["is_valid"])
        self.assertGreater(len(row2["errors"]), 0)
        err_str = " ".join(row2["errors"])
        self.assertIn("Invalid email format", err_str)
        self.assertIn("Invalid membership_status", err_str)
        self.assertIn("Invalid membership_expiry", err_str)
        self.assertIn("Unknown machine category", err_str)

        # Row 3 is invalid (missing name)
        row3 = preview["rows"][2]
        self.assertFalse(row3["is_valid"])
        self.assertIn("Field 'full_name' is required", row3["errors"][0])

    def test_02_machines_csv_preview_validation(self):
        """Test Machines CSV preview with capacity, hours, and rate parsing."""
        csv_content = (
            "code,name,category_code,capacity,state,operating_hours_start,operating_hours_end,hourly_rate,minimum_charge,peak_hourly_rate,peak_hours_start,peak_hours_end,location,description\n"
            '"FORGE-CNC-01","Forge Mill Pro","cnc_mills",1,"available","08:00","20:00","15.50","5.00","20.00","17:00","20:00","Room 2","Heavy CNC"\n'
            '"BAD-MACH-01","No Cat Machine","unknown_cat",0,"bad_state","25:00","08:00","abc","","","","","",""\n'
        )

        preview = preview_csv_import("machines", csv_content, actor_user=self.admin)
        self.assertEqual(preview["total_rows"], 2)
        self.assertEqual(preview["valid_rows_count"], 1)
        self.assertEqual(preview["invalid_rows_count"], 1)
        self.assertFalse(preview["can_import"])

        # Row 1 valid
        row1 = preview["rows"][0]
        self.assertTrue(row1["is_valid"])
        self.assertEqual(row1["parsed"]["hourly_rate_cents"], 1550)
        self.assertEqual(row1["parsed"]["minimum_charge_cents"], 500)
        self.assertEqual(row1["parsed"]["peak_hourly_rate_cents"], 2000)

        # Row 2 invalid
        row2 = preview["rows"][1]
        self.assertFalse(row2["is_valid"])
        err_str = " ".join(row2["errors"])
        self.assertIn("Unknown machine category code", err_str)
        self.assertIn("Capacity must be at least 1", err_str)
        self.assertIn("Invalid machine state", err_str)
        self.assertIn("Invalid operating hours format", err_str)

    def test_03_inventory_csv_preview_validation(self):
        """Test Inventory CSV preview with SKU, units, and initial stock."""
        csv_content = (
            "sku,name,category,unit,unit_cost,minimum_stock,initial_quantity,location,description\n"
            '"TEST-FIL-PETG-01","PETG Filament Transparent 1kg","filament","spool","24.00",2,8,"Shelf C1","PETG Spool"\n'
            '"","Missing SKU Item","general","pcs","0",0,0,"",""\n'
        )

        preview = preview_csv_import("inventory", csv_content, actor_user=self.admin)
        self.assertEqual(preview["total_rows"], 2)
        self.assertEqual(preview["valid_rows_count"], 1)
        self.assertEqual(preview["invalid_rows_count"], 1)

    # =========================================================================
    # 2. Atomic Rollback on Failed Imports
    # =========================================================================

    def test_04_import_execution_full_rollback_on_error(self):
        """Verify that if any row in the batch fails validation or DB constraint,
        zero rows are inserted and the database is completely unchanged.
        """
        initial_member_count = query_one("SELECT COUNT(*) AS c FROM members;")["c"]

        # Batch containing 1 valid row and 1 invalid row
        csv_failing_batch = (
            "full_name,email,phone,membership_status,membership_expiry,notes,qualifications\n"
            '"Valid Member Should Rollback","valid.rollback@test.it","123","active","2026-12-31","",""\n'
            '"Invalid Member","bad-email-address","","active","","",""\n'
        )

        with self.assertRaises(ValueError) as ctx:
            execute_csv_import("members", csv_failing_batch, actor_user=self.admin)
        self.assertIn("failed validation", str(ctx.exception).lower())

        # Verify count is strictly unchanged
        after_count = query_one("SELECT COUNT(*) AS c FROM members;")["c"]
        self.assertEqual(initial_member_count, after_count)

        # Verify the valid record was NOT created
        check_member = query_one("SELECT * FROM members WHERE email = 'valid.rollback@test.it';")
        self.assertIsNone(check_member)

    # =========================================================================
    # 3. Successful Atomic CSV Imports
    # =========================================================================

    def test_05_execute_members_import_success(self):
        """Test successful atomic import of members with qualifications and audit logging."""
        csv_content = (
            "full_name,email,phone,membership_status,membership_expiry,notes,qualifications\n"
            '"Martina Galli","martina.galli@test.it","+39 349 1122334","active","2026-12-31","Maker","3d_printers:2026-12-31;laser_cutters:2026-11-30"\n'
            '"Davide Bisi","davide.bisi@test.it","+39 348 5566778","active","2027-01-31","CNC Specialist","cnc_mills"\n'
        )

        res = execute_csv_import("members", csv_content, actor_user=self.admin)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["imported_count"], 2)
        self.assertEqual(len(res["created_ids"]), 2)

        # Verify members exist
        m1 = query_one("SELECT * FROM members WHERE email = 'martina.galli@test.it';")
        self.assertIsNotNone(m1)
        self.assertEqual(m1["full_name"], "Martina Galli")

        # Verify qualifications
        quals = query_all("SELECT * FROM qualifications WHERE member_id = ?;", (m1["id"],))
        self.assertEqual(len(quals), 2)

        # Verify audit logs
        audit_events = query_all("SELECT * FROM audit_log WHERE object_type = 'member' AND action = 'member.imported';")
        self.assertGreaterEqual(len(audit_events), 2)

    def test_06_execute_machines_import_success(self):
        """Test successful atomic import of machines."""
        csv_content = (
            "code,name,category_code,capacity,state,operating_hours_start,operating_hours_end,hourly_rate,minimum_charge,peak_hourly_rate,peak_hours_start,peak_hours_end,location,description\n"
            '"RESIN-ELEGOO-01","Elegoo Saturn 4 Ultra SLA","3d_printers",1,"available","08:00","22:00","3.50","1.50","5.00","17:00","21:00","SLA Darkroom","12K Resin 3D printer"\n'
        )

        res = execute_csv_import("machines", csv_content, actor_user=self.admin)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["imported_count"], 1)

        mach = query_one("SELECT * FROM machines WHERE code = 'RESIN-ELEGOO-01';")
        self.assertIsNotNone(mach)
        self.assertEqual(mach["name"], "Elegoo Saturn 4 Ultra SLA")
        self.assertEqual(mach["hourly_rate_cents"], 350)
        self.assertEqual(mach["minimum_charge_cents"], 150)
        self.assertEqual(mach["peak_hourly_rate_cents"], 500)

    def test_07_execute_inventory_import_with_ledger_initialization(self):
        """Test successful atomic import of inventory items with automatic ledger receipts."""
        csv_content = (
            "sku,name,category,unit,unit_cost,minimum_stock,initial_quantity,location,description\n"
            '"RAW-PLY-BIRCH-6MM","Baltic Birch Plywood 6mm 600x400","wood","sheet","18.00",5,12,"Rack W-2","Laser grade birch plywood sheet"\n'
        )

        res = execute_csv_import("inventory", csv_content, actor_user=self.admin)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["imported_count"], 1)

        item = query_one("SELECT * FROM inventory_items WHERE sku = 'RAW-PLY-BIRCH-6MM';")
        self.assertIsNotNone(item)
        self.assertEqual(item["unit_cost_cents"], 1800)

        # Verify ledger receipt record
        ledger = query_one("SELECT * FROM inventory_ledger WHERE item_id = ? AND movement_type = 'receipt';", (item["id"],))
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger["quantity"], 12)

    # =========================================================================
    # 4. Domain CSV / JSON Export Engine Tests
    # =========================================================================

    def test_08_export_members_csv_and_json(self):
        """Verify members CSV and JSON exports."""
        csv_data = export_members_csv()
        self.assertIn("Member Number", csv_data)
        self.assertIn("martina.galli@test.it", csv_data)

        json_str = export_members_json()
        data = json.loads(json_str)
        self.assertEqual(data["app"], "ForgeDesk")
        self.assertEqual(data["entity"], "members")
        self.assertGreater(data["total_count"], 0)

    def test_09_export_machines_csv_and_json(self):
        """Verify machines CSV and JSON exports."""
        csv_data = export_machines_csv()
        self.assertIn("Hourly Rate (EUR)", csv_data)
        self.assertIn("PRUSA-MK4-01", csv_data)

        json_str = export_machines_json()
        data = json.loads(json_str)
        self.assertEqual(data["entity"], "machines")
        self.assertGreater(data["total_count"], 0)

    def test_10_export_reservations_csv_and_json(self):
        """Verify reservations CSV and JSON exports."""
        csv_data = export_reservations_csv()
        self.assertIn("Start Time (Europe/Rome)", csv_data)

        json_str = export_reservations_json()
        data = json.loads(json_str)
        self.assertEqual(data["entity"], "reservations")

    def test_11_export_inventory_and_ledger_csv(self):
        """Verify inventory and immutable movement ledger exports."""
        inv_csv = export_inventory_csv()
        self.assertIn("SKU", inv_csv)
        self.assertIn("Current Stock", inv_csv)

        ledger_csv = export_inventory_ledger_csv()
        self.assertIn("Movement Type", ledger_csv)
        self.assertIn("Unit Cost (EUR)", ledger_csv)

        inv_json = export_inventory_json()
        data = json.loads(inv_json)
        self.assertIn("items", data)
        self.assertIn("ledger", data)

    def test_12_export_charges_csv_and_json(self):
        """Verify usage charges and adjustments exports."""
        charges_csv = export_charges_csv()
        self.assertIn("Base Charge (EUR)", charges_csv)

        charges_json = export_charges_json()
        data = json.loads(charges_json)
        self.assertEqual(data["entity"], "charges")

    def test_13_export_full_system_json_dump(self):
        """Verify full system database JSON dump."""
        dump_str = export_full_system_json()
        dump = json.loads(dump_str)
        self.assertEqual(dump["app"], "ForgeDesk")
        self.assertEqual(dump["export_type"], "full_system_database_dump")
        self.assertIn("users", dump["tables"])
        self.assertIn("reservations", dump["tables"])
        self.assertIn("audit_log", dump["tables"])

        # Check password hash redaction
        users_table = dump["tables"]["users"]
        if users_table:
            self.assertEqual(users_table[0]["password_hash"], "[REDACTED]")

    # =========================================================================
    # 5. Online SQLite Backup & Restore Engine Tests
    # =========================================================================

    def test_14_online_sqlite_backup_creation_and_validation(self):
        """Verify online SQLite backup generation and header/schema validation."""
        backup_path = create_database_backup(actor_user=self.admin)
        self.assertTrue(backup_path.is_file())
        self.assertGreater(backup_path.stat().st_size, 1024)

        # Validate backup file integrity
        validation = validate_backup_file(backup_path)
        self.assertTrue(validation["is_valid"])
        self.assertIn("users", validation["tables"])
        self.assertIn("reservations", validation["tables"])
        self.assertIn("audit_log", validation["tables"])
        self.assertGreater(validation["user_count"], 0)

        # Verify audit log event
        audit = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'system' AND action = 'system.backup_created' ORDER BY id DESC LIMIT 1;"
        )
        self.assertIsNotNone(audit)
        self.assertEqual(audit["object_id"], backup_path.name)

    def test_15_database_restore_verified_and_atomic(self):
        """Verify atomic database restoration from a validated backup file."""
        # 1. Create a baseline backup
        baseline_backup = create_database_backup(actor_user=self.admin)

        # 2. Add a temporary canary member
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO members (member_number, full_name, email, membership_status, created_at, updated_at)
            VALUES ('FD-CANARY-01', 'Canary Member', 'canary@test.it', 'active', '2026-08-30T12:00:00', '2026-08-30T12:00:00');
            """
        )
        conn.commit()
        conn.close()

        # Check canary exists
        canary = query_one("SELECT * FROM members WHERE member_number = 'FD-CANARY-01';")
        self.assertIsNotNone(canary)

        # 3. Restore database from baseline backup
        res = restore_database_backup(baseline_backup, actor_user=self.admin)
        self.assertEqual(res["status"], "success")

        # 4. Verify canary member is gone (database rolled back to baseline snapshot)
        canary_after = query_one("SELECT * FROM members WHERE member_number = 'FD-CANARY-01';")
        self.assertIsNone(canary_after)

        # 5. Verify restore audit log exists in restored database
        restore_audit = query_one(
            "SELECT * FROM audit_log WHERE object_type = 'system' AND action = 'system.database_restored' ORDER BY id DESC LIMIT 1;"
        )
        self.assertIsNotNone(restore_audit)

    def test_16_restore_invalid_corrupt_file_rejected(self):
        """Verify that attempting to restore a non-SQLite or corrupt file is rejected with error."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(b"NOT A SQLITE DATABASE HEADER DATA CORRUPT")
            corrupt_path = Path(f.name)

        try:
            with self.assertRaises(ValueError) as ctx:
                restore_database_backup(corrupt_path, actor_user=self.admin)
            self.assertTrue(
                "not a valid sqlite database" in str(ctx.exception).lower()
                or "too small" in str(ctx.exception).lower()
            )
        finally:
            if corrupt_path.is_file():
                corrupt_path.unlink()

    # =========================================================================
    # 6. RBAC & HTTP Endpoint Security Tests
    # =========================================================================

    def test_17_rbac_system_endpoints(self):
        """Verify server-side RBAC on /system/data, /system/import, /system/backup, and exports."""
        # 1. Admin can access Data Hub, Import, and Backup
        resp_admin = self._make_request("GET", "/system/data", cookie_session=self.admin_session)
        self.assertEqual(resp_admin.status_code, 200)

        resp_admin_imp = self._make_request("GET", "/system/import", cookie_session=self.admin_session)
        self.assertEqual(resp_admin_imp.status_code, 200)

        # 2. Operator can access Data Hub, Export, and Backup
        resp_op = self._make_request("GET", "/system/data", cookie_session=self.operator_session)
        self.assertEqual(resp_op.status_code, 200)

        resp_op_exp = self._make_request("GET", "/members/export?format=csv", cookie_session=self.operator_session)
        self.assertEqual(resp_op_exp.status_code, 200)

        # 3. Regular Member is blocked (403 Forbidden)
        resp_mem = self._make_request("GET", "/system/data", cookie_session=self.member_session)
        self.assertEqual(resp_mem.status_code, 403)

        resp_mem_imp = self._make_request("GET", "/system/import", cookie_session=self.member_session)
        self.assertEqual(resp_mem_imp.status_code, 403)

        # 4. Viewer is blocked (403 Forbidden)
        resp_viewer = self._make_request("GET", "/system/data", cookie_session=self.viewer_session)
        self.assertEqual(resp_viewer.status_code, 403)

    def test_18_rest_api_import_preview_and_execute(self):
        """Verify REST API /api/system/import/preview and /api/system/import/execute."""
        csv_payload = (
            "sku,name,category,unit,unit_cost,minimum_stock,initial_quantity,location,description\n"
            '"API-MAT-01","API Material Item","general","pcs","5.00",1,10,"Shelf API","API Desc"\n'
        )

        # 1. Preview API
        resp = self._make_request(
            "POST",
            "/api/system/import/preview",
            bearer_token=self.admin_session,
            body={"entity_type": "inventory", "csv_content": csv_payload},
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["preview"]["can_import"])

        # 2. Execute API
        resp_exec = self._make_request(
            "POST",
            "/api/system/import/execute",
            bearer_token=self.admin_session,
            body={"entity_type": "inventory", "csv_content": csv_payload},
        )
        self.assertEqual(resp_exec.status_code, 200)
        data_exec = json.loads(resp_exec.body.decode("utf-8"))
        self.assertEqual(data_exec["status"], "success")
        self.assertEqual(data_exec["imported_count"], 1)

        # Verify in DB
        db_item = query_one("SELECT * FROM inventory_items WHERE sku = 'API-MAT-01';")
        self.assertIsNotNone(db_item)

    # =========================================================================
    # 7. Challenger Finding Verification Tests (CHAL-S20-001, 002, 003)
    # =========================================================================

    def test_19_dashboard_renders_recent_audit_events(self):
        """Verify dashboard index handler queries audit_log and renders recent events correctly (CHAL-S20-001)."""
        # Record a test audit event
        from forgedesk.audit.service import record_audit_event
        record_audit_event(
            action="system.dashboard_test_event",
            object_type="system",
            object_id="CHAL-S20-001",
            actor=self.admin,
        )

        resp = self._make_request("GET", "/", cookie_session=self.admin_session)
        self.assertEqual(resp.status_code, 200)
        body_text = resp.body.decode("utf-8")
        self.assertIn("system.dashboard_test_event", body_text)
        self.assertIn("CHAL-S20-001", body_text)
        self.assertNotIn("No audit events recorded yet.", body_text)

    def test_20_member_import_non_sequential_member_number_uniqueness(self):
        """Verify member CSV import derives unique sequential numbers even with custom member numbers (CHAL-S20-003)."""
        # Insert a member with a higher sequence number
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO members (member_number, full_name, email, membership_status, created_at, updated_at)
            VALUES ('FD-MEM-9900', 'High Seq User', 'highseq@test.it', 'active', '2026-08-30T10:00:00', '2026-08-30T10:00:00');
            """
        )
        conn.commit()
        conn.close()

        csv_content = (
            "full_name,email,phone,membership_status,membership_expiry,notes,qualifications\n"
            '"Imported High User","imported.high@test.it","+39 340 1111111","active","2026-12-31","",""\n'
        )

        res = execute_csv_import("members", csv_content, actor_user=self.admin)
        self.assertEqual(res["status"], "success")

        m = query_one("SELECT * FROM members WHERE email = 'imported.high@test.it';")
        self.assertIsNotNone(m)
        self.assertEqual(m["member_number"], "FD-MEM-9901")

    def test_21_database_restore_wal_cleanup(self):
        """Verify that database restore removes stale WAL/SHM auxiliary files cleanly (CHAL-S20-002)."""
        # Create backup
        bpath = create_database_backup(actor_user=self.admin)
        self.assertTrue(bpath.is_file())

        # Simulate presence of WAL/SHM files
        wal_file = DB_PATH.parent / f"{DB_PATH.name}-wal"
        shm_file = DB_PATH.parent / f"{DB_PATH.name}-shm"
        wal_file.write_text("dummy-wal-content")
        shm_file.write_text("dummy-shm-content")

        self.assertTrue(wal_file.exists())
        self.assertTrue(shm_file.exists())

        # Restore
        res = restore_database_backup(bpath, actor_user=self.admin)
        self.assertEqual(res["status"], "success")

        # Check that dummy WAL/SHM were cleaned up and DB is healthy
        self.assertFalse(wal_file.exists())
        self.assertFalse(shm_file.exists())
        check = query_one("SELECT COUNT(*) AS c FROM users;")
        self.assertGreater(check["c"], 0)


if __name__ == "__main__":
    unittest.main()
