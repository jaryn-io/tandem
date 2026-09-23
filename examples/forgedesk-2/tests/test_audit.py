"""Comprehensive unit and integration test suite for Append-Only Audit Logging, Diff Engine, Search, and Authorization."""

import json
import sqlite3
import unittest

from forgedesk.audit.service import (
    diff_values,
    export_audit_logs_csv,
    export_audit_logs_json,
    get_audit_event_by_id,
    get_audit_filter_options,
    query_audit_logs,
    record_audit_event,
)
from forgedesk.auth.middleware import COOKIE_SESSION_NAME
from forgedesk.auth.permissions import PERM_AUDIT_VIEW, has_permission
from forgedesk.auth.service import (
    authenticate_user,
    create_user,
    create_user_session,
    toggle_user_active_status,
    update_user_password,
    update_user_role,
)
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.db import get_connection, init_db, reset_database, seed_database, transaction
from app import create_application_router


class TestAuditLogging(unittest.TestCase):
    """Test append-only audit logging engine, diffing, searching, integrity, and RBAC security."""

    @classmethod
    def setUpClass(cls):
        reset_database()
        init_db(seed_if_empty=False)
        seed_database(force=True)

    def setUp(self):
        self.router = create_application_router()

    def test_01_record_audit_events(self):
        """Verify recording audit events with actor, timestamp, action, object, and before/after values."""
        admin = authenticate_user("admin", "admin123")
        self.assertIsNotNone(admin)

        before_state = {"state": "available", "location": "Bench A1"}
        after_state = {"state": "under_maintenance", "location": "Bench A1"}
        details_payload = {"reason": "Routine spindle inspection", "ticket_id": "MNT-9901"}

        event_id = record_audit_event(
            action="machine.state_changed",
            object_type="machine",
            object_id="PRUSA-MK4-01",
            actor=admin,
            before=before_state,
            after=after_state,
            details=details_payload,
            ip_address="192.168.1.42",
        )

        self.assertIsInstance(event_id, int)
        self.assertGreater(event_id, 0)

        # Retrieve and inspect
        event = get_audit_event_by_id(event_id)
        self.assertIsNotNone(event)
        self.assertEqual(event["id"], event_id)
        self.assertEqual(event["action"], "machine.state_changed")
        self.assertEqual(event["object_type"], "machine")
        self.assertEqual(event["object_id"], "PRUSA-MK4-01")
        self.assertEqual(event["actor_id"], admin["id"])
        self.assertEqual(event["actor_name"], admin["full_name"])
        self.assertEqual(event["ip_address"], "192.168.1.42")
        self.assertIn("T", event["created_at"])  # ISO timestamp

        # Verify parsed before/after and computed diffs
        self.assertEqual(event["before"], before_state)
        self.assertEqual(event["after"], after_state)
        self.assertEqual(event["details"], details_payload)
        self.assertIn("state", event["diffs"])
        self.assertEqual(event["diffs"]["state"]["before"], "available")
        self.assertEqual(event["diffs"]["state"]["after"], "under_maintenance")
        self.assertEqual(event["diffs"]["state"]["change_type"], "modified")
        # Location was unchanged, so not in diffs
        self.assertNotIn("location", event["diffs"])

    def test_02_diff_values_utility(self):
        """Test the diff_values engine for additions, removals, modifications, and ignored keys."""
        before = {
            "name": "Old Name",
            "hourly_rate_cents": 200,
            "old_feature": True,
            "password_salt": "secret_salt_123",
        }
        after = {
            "name": "New Name",
            "hourly_rate_cents": 200,
            "new_feature": "enabled",
            "password_salt": "new_salt_456",
        }

        diffs = diff_values(before, after)

        # 'name' is modified
        self.assertIn("name", diffs)
        self.assertEqual(diffs["name"]["change_type"], "modified")
        self.assertEqual(diffs["name"]["before"], "Old Name")
        self.assertEqual(diffs["name"]["after"], "New Name")

        # 'hourly_rate_cents' is unchanged -> not in diff
        self.assertNotIn("hourly_rate_cents", diffs)

        # 'old_feature' was removed
        self.assertIn("old_feature", diffs)
        self.assertEqual(diffs["old_feature"]["change_type"], "removed")
        self.assertEqual(diffs["old_feature"]["before"], True)
        self.assertIsNone(diffs["old_feature"]["after"])

        # 'new_feature' was added
        self.assertIn("new_feature", diffs)
        self.assertEqual(diffs["new_feature"]["change_type"], "added")
        self.assertIsNone(diffs["new_feature"]["before"])
        self.assertEqual(diffs["new_feature"]["after"], "enabled")

        # 'password_salt' must be ignored by default
        self.assertNotIn("password_salt", diffs)

    def test_03_query_and_search_audit_logs(self):
        """Test searching and filtering audit records with multiple criteria and pagination."""
        operator = authenticate_user("operator", "operator123")
        self.assertIsNotNone(operator)

        # Record distinctive events
        record_audit_event(
            action="inventory.receipt",
            object_type="inventory_item",
            object_id="SKU-TEST-001",
            actor=operator,
            details={"quantity": 100, "supplier": "Acme Tools"},
        )
        record_audit_event(
            action="reservation.cancelled",
            object_type="reservation",
            object_id="999",
            actor=operator,
            details={"cancellation_reason": "Member sick"},
        )

        # Filter by action
        inv_records, inv_total = query_audit_logs(action="inventory.receipt")
        self.assertTrue(any(r["object_id"] == "SKU-TEST-001" for r in inv_records))

        # Filter by object_type
        res_records, res_total = query_audit_logs(object_type="reservation")
        self.assertTrue(any(r["object_id"] == "999" for r in res_records))

        # Free-text search matching details text
        search_records, search_total = query_audit_logs(search_query="Acme Tools")
        self.assertTrue(len(search_records) >= 1)
        self.assertEqual(search_records[0]["object_id"], "SKU-TEST-001")

        # Pagination test
        page1, total1 = query_audit_logs(limit=2, offset=0)
        page2, total2 = query_audit_logs(limit=2, offset=2)
        self.assertEqual(total1, total2)
        self.assertEqual(len(page1), 2)
        if total1 >= 4:
            self.assertEqual(len(page2), 2)
            self.assertNotEqual(page1[0]["id"], page2[0]["id"])

    def test_04_transactional_atomic_rollback(self):
        """Test that audit events written within a database transaction roll back atomically on failure."""
        admin = authenticate_user("admin", "admin123")

        # Attempt multi-step operation that fails halfway
        try:
            with transaction() as tx:
                # 1. Update user
                tx.execute("UPDATE users SET full_name = 'Temporary Name' WHERE id = 3;")
                # 2. Record audit inside tx
                record_audit_event(
                    action="user.temp_update",
                    object_type="user",
                    object_id=3,
                    actor=admin,
                    details={"attempt": "will_fail"},
                    conn=tx,
                )
                # 3. Simulate failure
                raise RuntimeError("Intentional simulated transaction failure")
        except RuntimeError:
            pass

        # Verify full_name was rolled back
        user = authenticate_user("member_alice", "member123")
        self.assertEqual(user["full_name"], "Alice Moretti")

        # Verify audit event was also rolled back
        records, _ = query_audit_logs(action="user.temp_update")
        self.assertEqual(len(records), 0)

        # Successful transaction
        with transaction() as tx:
            tx.execute("UPDATE users SET full_name = 'Alice M. Moretti' WHERE id = 3;")
            record_audit_event(
                action="user.name_updated",
                object_type="user",
                object_id=3,
                actor=admin,
                before={"full_name": "Alice Moretti"},
                after={"full_name": "Alice M. Moretti"},
                conn=tx,
            )

        # Verify both data and audit committed
        user = authenticate_user("member_alice", "member123")
        self.assertEqual(user["full_name"], "Alice M. Moretti")
        records, _ = query_audit_logs(action="user.name_updated")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["diffs"]["full_name"]["after"], "Alice M. Moretti")

        # Restore original name
        with transaction() as tx:
            tx.execute("UPDATE users SET full_name = 'Alice Moretti' WHERE id = 3;")

    def test_05_append_only_immutability_enforcement(self):
        """Verify that SQLite triggers block UPDATE and DELETE statements on audit_log table."""
        conn = get_connection()
        try:
            # 1. Find an existing audit record
            row = conn.execute("SELECT id FROM audit_log LIMIT 1;").fetchone()
            self.assertIsNotNone(row)
            audit_id = row["id"]

            # 2. Attempt direct UPDATE -> Must raise sqlite3.IntegrityError / OperationalError
            with self.assertRaises(sqlite3.Error) as ctx_update:
                conn.execute("UPDATE audit_log SET action = 'tampered.action' WHERE id = ?;", (audit_id,))
                conn.commit()
            self.assertIn("append-only", str(ctx_update.exception).lower())

            # 3. Attempt direct DELETE -> Must raise sqlite3.IntegrityError / OperationalError
            with self.assertRaises(sqlite3.Error) as ctx_delete:
                conn.execute("DELETE FROM audit_log WHERE id = ?;", (audit_id,))
                conn.commit()
            self.assertIn("append-only", str(ctx_delete.exception).lower())

        finally:
            conn.close()

    def test_06_rbac_access_control_on_audit(self):
        """Enforce that only Admin and Operator can view audit logs, while Member and Viewer are blocked with 403."""
        admin_token = create_user_session(1)
        operator_token = create_user_session(2)
        alice_token = create_user_session(3)   # Member
        viewer_token = create_user_session(6)  # Viewer

        # 1. Unauthenticated -> 303 Redirect to login (HTML) or 401 Unauthorized (API)
        req_unauth_html = Request(method="GET", path="/audit", headers={})
        res_unauth_html = self.router.dispatch(req_unauth_html)
        self.assertEqual(res_unauth_html.status_code, 303)
        self.assertIn("/auth/login", res_unauth_html.headers.get("Location", ""))

        req_unauth_api = Request(method="GET", path="/api/audit/logs", headers={"Accept": "application/json"})
        res_unauth_api = self.router.dispatch(req_unauth_api)
        self.assertEqual(res_unauth_api.status_code, 401)

        # 2. Member (Alice) -> 403 Forbidden
        req_alice_html = Request(method="GET", path="/audit", headers={"Cookie": f"fd_session={alice_token}"})
        res_alice_html = self.router.dispatch(req_alice_html)
        self.assertEqual(res_alice_html.status_code, 403)
        self.assertIn("403 - Access Forbidden", res_alice_html.body.decode("utf-8"))

        req_alice_api = Request(
            method="GET",
            path="/api/audit/logs",
            headers={"Authorization": f"Bearer {alice_token}", "Accept": "application/json"},
        )
        res_alice_api = self.router.dispatch(req_alice_api)
        self.assertEqual(res_alice_api.status_code, 403)

        # 3. Viewer -> 403 Forbidden
        req_viewer_api = Request(
            method="GET",
            path="/api/audit/logs",
            headers={"Authorization": f"Bearer {viewer_token}", "Accept": "application/json"},
        )
        res_viewer_api = self.router.dispatch(req_viewer_api)
        self.assertEqual(res_viewer_api.status_code, 403)

        # 4. Operator -> 200 OK (Allowed)
        req_op_html = Request(method="GET", path="/audit", headers={"Cookie": f"fd_session={operator_token}"})
        res_op_html = self.router.dispatch(req_op_html)
        self.assertEqual(res_op_html.status_code, 200)
        self.assertIn("Searchable Audit Trail", res_op_html.body.decode("utf-8"))

        req_op_api = Request(
            method="GET",
            path="/api/audit/logs",
            headers={"Authorization": f"Bearer {operator_token}", "Accept": "application/json"},
        )
        res_op_api = self.router.dispatch(req_op_api)
        self.assertEqual(res_op_api.status_code, 200)
        data_op = json.loads(res_op_api.body.decode("utf-8"))
        self.assertIn("records", data_op)
        self.assertIn("total", data_op)

        # 5. Admin -> 200 OK (Allowed)
        req_admin_api = Request(
            method="GET",
            path="/api/audit/logs",
            headers={"Authorization": f"Bearer {admin_token}", "Accept": "application/json"},
        )
        res_admin_api = self.router.dispatch(req_admin_api)
        self.assertEqual(res_admin_api.status_code, 200)

    def test_07_auth_integration_audit_trail(self):
        """Test that authentication, password changes, role updates, and status toggles generate audit events."""
        admin = authenticate_user("admin", "admin123")
        self.assertIsNotNone(admin)

        # 1. Successful Login via API
        req_login = Request(
            method="POST",
            path="/api/auth/login",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"username": "member_bob", "password": "member123"}).encode("utf-8"),
        )
        res_login = self.router.dispatch(req_login)
        self.assertEqual(res_login.status_code, 200)

        # Verify auth.login_success event recorded
        records, _ = query_audit_logs(action="auth.login_success")
        self.assertTrue(any(r["actor_name"] == "Bob Rossi" for r in records))

        # 2. Failed Login via API
        req_fail = Request(
            method="POST",
            path="/api/auth/login",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"username": "unknown_intruder", "password": "wrong_password"}).encode("utf-8"),
        )
        res_fail = self.router.dispatch(req_fail)
        self.assertEqual(res_fail.status_code, 401)

        # Verify auth.login_failed event recorded
        records, _ = query_audit_logs(action="auth.login_failed")
        self.assertTrue(any("unknown_intruder" in (r["details_json"] or "") for r in records))

        # 3. Role Change
        target_user = authenticate_user("member_bob", "member123")
        update_user_role(target_user["id"], "viewer", admin)

        records, _ = query_audit_logs(action="user.role_changed")
        self.assertTrue(any(r["object_id"] == str(target_user["id"]) for r in records))
        role_event = next(r for r in records if r["object_id"] == str(target_user["id"]))
        self.assertEqual(role_event["diffs"]["role"]["before"], "member")
        self.assertEqual(role_event["diffs"]["role"]["after"], "viewer")

        # Restore role
        update_user_role(target_user["id"], "member", admin)

        # 4. Account Status Toggle
        toggle_user_active_status(target_user["id"], admin)
        records, _ = query_audit_logs(action="user.status_toggled")
        self.assertTrue(any(r["object_id"] == str(target_user["id"]) for r in records))
        toggle_event = next(r for r in records if r["object_id"] == str(target_user["id"]))
        self.assertEqual(toggle_event["diffs"]["is_active"]["before"], 1)
        self.assertEqual(toggle_event["diffs"]["is_active"]["after"], 0)

        # Re-activate
        toggle_user_active_status(target_user["id"], admin)

    def test_08_export_csv_and_json(self):
        """Test CSV and JSON export functions and HTTP download endpoints."""
        admin_token = create_user_session(1)

        # 1. Direct CSV export helper
        csv_data = export_audit_logs_csv(limit=50)
        self.assertIn("Timestamp (Europe/Rome)", csv_data)
        self.assertIn("Action", csv_data)
        self.assertIn("Object Type", csv_data)
        self.assertIn("Before JSON", csv_data)
        self.assertIn("After JSON", csv_data)

        # 2. Direct JSON export helper
        json_data = export_audit_logs_json(limit=50)
        parsed = json.loads(json_data)
        self.assertEqual(parsed.get("app"), "ForgeDesk")
        self.assertIn("records", parsed)
        self.assertGreater(parsed.get("total_records", 0), 0)

        # 3. HTTP Export CSV endpoint
        req_export_csv = Request(
            method="GET",
            path="/audit/export?format=csv",
            headers={"Cookie": f"fd_session={admin_token}"},
        )
        res_export_csv = self.router.dispatch(req_export_csv)
        self.assertEqual(res_export_csv.status_code, 200)
        self.assertIn("text/csv", res_export_csv.headers.get("Content-Type", ""))
        self.assertIn("attachment; filename=", res_export_csv.headers.get("Content-Disposition", ""))

        # 4. HTTP Export JSON endpoint
        req_export_json = Request(
            method="GET",
            path="/audit/export?format=json",
            headers={"Cookie": f"fd_session={admin_token}"},
        )
        res_export_json = self.router.dispatch(req_export_json)
        self.assertEqual(res_export_json.status_code, 200)
        self.assertIn("application/json", res_export_json.headers.get("Content-Type", ""))

        # 5. Filter Options API endpoint
        req_opts = Request(
            method="GET",
            path="/api/audit/filter-options",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        res_opts = self.router.dispatch(req_opts)
        self.assertEqual(res_opts.status_code, 200)
        opts_data = json.loads(res_opts.body.decode("utf-8"))
        self.assertIn("actions", opts_data)
        self.assertIn("object_types", opts_data)
        self.assertIn("actors", opts_data)

        # 6. Single Audit Event Detail HTML & API
        event = query_audit_logs(limit=1)[0][0]
        event_id = event["id"]

        req_detail_html = Request(
            method="GET",
            path=f"/audit/{event_id}",
            headers={"Cookie": f"fd_session={admin_token}"},
        )
        res_detail_html = self.router.dispatch(req_detail_html)
        self.assertEqual(res_detail_html.status_code, 200)
        self.assertIn(f"Audit Event #{event_id}", res_detail_html.body.decode("utf-8"))

        req_detail_api = Request(
            method="GET",
            path=f"/api/audit/logs/{event_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        res_detail_api = self.router.dispatch(req_detail_api)
        self.assertEqual(res_detail_api.status_code, 200)
        data_detail = json.loads(res_detail_api.body.decode("utf-8"))
        self.assertEqual(data_detail["record"]["id"], event_id)


if __name__ == "__main__":
    unittest.main()
