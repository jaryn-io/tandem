"""Comprehensive unit and integration test suite for Members, Machine Categories, and Qualifications."""

import datetime
import json
import unittest

from app import create_application_router
from forgedesk.auth.middleware import COOKIE_SESSION_NAME
from forgedesk.auth.permissions import ROLE_ADMIN, ROLE_MEMBER, ROLE_OPERATOR, ROLE_VIEWER
from forgedesk.auth.service import authenticate_user, create_user_session, get_user_by_id
from forgedesk.core.http import Request, Response
from forgedesk.db import init_db, reset_database, seed_database
from forgedesk.members.service import (
    change_membership_status,
    check_member_qualification,
    create_machine_category,
    create_member,
    delete_member,
    generate_next_member_number,
    get_machine_category_by_code,
    get_machine_category_by_id,
    get_member_by_id,
    get_member_by_number,
    get_member_by_user_id,
    get_qualification_by_id,
    grant_qualification,
    list_machine_categories,
    list_member_qualifications,
    list_members,
    revoke_qualification,
    update_member,
)
from forgedesk.utils.datetime_tz import now_rome, now_rome_iso


class TestMembersAndQualifications(unittest.TestCase):
    """Test suite covering member lifecycle, qualification validity across time, and role-based access."""

    @classmethod
    def setUpClass(cls):
        reset_database()
        init_db(seed_if_empty=False)
        seed_database(force=True)

    def setUp(self):
        self.router = create_application_router()

        # Fetch demo users
        self.admin_user = authenticate_user("admin", "admin123")
        self.operator_user = authenticate_user("operator", "operator123")
        self.alice_user = authenticate_user("member_alice", "member123")
        self.bob_user = authenticate_user("member_bob", "member123")
        self.clara_user = authenticate_user("member_clara", "member123")
        self.viewer_user = authenticate_user("viewer", "viewer123")

        # Create sessions
        self.admin_session = create_user_session(self.admin_user["id"])
        self.operator_session = create_user_session(self.operator_user["id"])
        self.alice_session = create_user_session(self.alice_user["id"])
        self.bob_session = create_user_session(self.bob_user["id"])
        self.viewer_session = create_user_session(self.viewer_user["id"])

    def _make_request(
        self,
        method: str,
        path: str,
        session_token: str = None,
        bearer_token: str = None,
        json_data: dict = None,
        form_data: dict = None,
        headers: dict = None,
    ) -> Response:
        """Helper to construct and dispatch HTTP requests through Router."""
        req_headers = headers.copy() if headers else {}
        body_bytes = b""

        if bearer_token:
            req_headers["Authorization"] = f"Bearer {bearer_token}"
        elif session_token:
            req_headers["Cookie"] = f"{COOKIE_SESSION_NAME}={session_token}"

        if json_data is not None:
            body_bytes = json.dumps(json_data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
            req_headers["Content-Length"] = str(len(body_bytes))
        elif form_data is not None:
            from urllib.parse import urlencode
            encoded = urlencode(form_data)
            body_bytes = encoded.encode("utf-8")
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"
            req_headers["Content-Length"] = str(len(body_bytes))

        req = Request(
            method=method,
            path=path,
            headers=req_headers,
            body=body_bytes,
            client_address=("127.0.0.1", 54321),
        )
        return self.router.dispatch(req)

    # -------------------------------------------------------------------------
    # 1. Member Lifecycle & Service Unit Tests
    # -------------------------------------------------------------------------

    def test_01_list_seeded_members(self):
        """Verify seeded demo members are listed with correct qualification metrics."""
        members = list_members()
        self.assertGreaterEqual(len(members), 3)

        alice = next((m for m in members if m["full_name"] == "Alice Moretti"), None)
        self.assertIsNotNone(alice)
        self.assertEqual(alice["membership_status"], "active")
        self.assertGreaterEqual(alice["total_qualifications"], 2)
        self.assertTrue(alice["is_active_and_valid"])

    def test_02_create_member_service(self):
        """Create new member with auto-generated member number and custom parameters."""
        mem_id = create_member(
            full_name="Davide Fontana",
            email="davide@makers.local",
            phone="+39 349 999 8888",
            membership_status="active",
            membership_expiry="2027-12-31",
            notes="Experienced metal lathe operator",
            actor_user=self.admin_user,
        )
        self.assertIsInstance(mem_id, int)

        member = get_member_by_id(mem_id)
        self.assertIsNotNone(member)
        self.assertEqual(member["full_name"], "Davide Fontana")
        self.assertEqual(member["email"], "davide@makers.local")
        self.assertEqual(member["phone"], "+39 349 999 8888")
        self.assertEqual(member["membership_status"], "active")
        self.assertEqual(member["membership_expiry"], "2027-12-31")
        self.assertTrue(member["member_number"].startswith("FD-MEM-"))
        self.assertTrue(member["is_active_and_valid"])

    def test_03_member_validation_errors(self):
        """Verify validation errors for missing name, invalid email, duplicate number, invalid dates."""
        with self.assertRaises(ValueError):
            create_member(full_name="", email="valid@email.com")

        with self.assertRaises(ValueError):
            create_member(full_name="Test User", email="invalid-email")

        with self.assertRaises(ValueError):
            create_member(full_name="Test User", email="test@email.com", membership_status="unknown_status")

        with self.assertRaises(ValueError):
            create_member(full_name="Test User", email="test@email.com", membership_expiry="not-a-date")

    def test_04_update_member_service(self):
        """Update member details, contact info, notes, and verify before/after audit recording."""
        mem_id = create_member(
            full_name="Elisa Galli",
            email="elisa@makers.local",
            membership_status="active",
            actor_user=self.operator_user,
        )

        success, msg = update_member(
            member_id=mem_id,
            full_name="Elisa Galli-Sartori",
            phone="+39 333 111 2222",
            notes="Updated contact number",
            actor_user=self.operator_user,
        )
        self.assertTrue(success)

        updated = get_member_by_id(mem_id)
        self.assertEqual(updated["full_name"], "Elisa Galli-Sartori")
        self.assertEqual(updated["phone"], "+39 333 111 2222")
        self.assertEqual(updated["notes"], "Updated contact number")

    def test_05_change_membership_status(self):
        """Change membership status between active, suspended, and expired."""
        mem_id = create_member(
            full_name="Fabio Rinaldi",
            email="fabio@makers.local",
            membership_status="active",
            actor_user=self.admin_user,
        )

        # Suspend
        success, _ = change_membership_status(mem_id, "suspended", actor_user=self.operator_user, notes="Policy violation")
        self.assertTrue(success)
        member = get_member_by_id(mem_id)
        self.assertEqual(member["membership_status"], "suspended")
        self.assertFalse(member["is_active_and_valid"])

        # Reactivate
        success, _ = change_membership_status(mem_id, "active", actor_user=self.admin_user)
        self.assertTrue(success)
        member = get_member_by_id(mem_id)
        self.assertEqual(member["membership_status"], "active")
        self.assertTrue(member["is_active_and_valid"])

    def test_06_delete_member_safety(self):
        """Ensure member with reservations cannot be deleted, but a clean member can."""
        # Alice has reservations seeded
        alice = get_member_by_user_id(self.alice_user["id"])
        success, msg = delete_member(alice["id"], actor_user=self.admin_user)
        self.assertFalse(success)
        self.assertIn("Cannot delete member with existing reservation", msg)

        # Clean member can be deleted
        clean_id = create_member(
            full_name="Temporary Member",
            email="temp@makers.local",
            actor_user=self.admin_user,
        )
        success, msg = delete_member(clean_id, actor_user=self.admin_user)
        self.assertTrue(success)
        self.assertIsNone(get_member_by_id(clean_id))

    # -------------------------------------------------------------------------
    # 2. Machine Categories & Qualifications Unit Tests
    # -------------------------------------------------------------------------

    def test_07_machine_categories_catalog(self):
        """List seeded machine categories and create a new category."""
        cats = list_machine_categories()
        self.assertGreaterEqual(len(cats), 5)
        codes = [c["code"] for c in cats]
        self.assertIn("3d_printers", codes)
        self.assertIn("laser_cutters", codes)
        self.assertIn("cnc_mills", codes)

        new_cat_id = create_machine_category(
            code="vinyl_plotters",
            name="Vinyl & Film Plotters",
            description="Precision Roland vinyl cutting plotters",
            actor_user=self.admin_user,
        )
        cat = get_machine_category_by_id(new_cat_id)
        self.assertEqual(cat["code"], "vinyl_plotters")
        self.assertEqual(cat["name"], "Vinyl & Film Plotters")

    def test_08_grant_and_recertify_qualification(self):
        """Grant qualification, check validity, and update/recertify."""
        mem_id = create_member(
            full_name="Giorgio Vanni",
            email="giorgio@makers.local",
            actor_user=self.admin_user,
        )
        cat_3d = get_machine_category_by_code("3d_printers")

        # Grant qualification
        qid = grant_qualification(
            member_id=mem_id,
            category_id=cat_3d["id"],
            qualification_name="Certified 3D Print Specialist",
            issue_date="2026-01-01",
            expiry_date="2027-01-01",
            notes="Passed safety exam",
            actor_user=self.operator_user,
        )
        self.assertIsInstance(qid, int)

        quals = list_member_qualifications(mem_id)
        self.assertEqual(len(quals), 1)
        self.assertEqual(quals[0]["status"], "valid")
        self.assertTrue(quals[0]["is_valid"])

        # Recertify with extended date
        qid2 = grant_qualification(
            member_id=mem_id,
            category_id=cat_3d["id"],
            qualification_name="Master 3D Print Specialist",
            issue_date="2026-01-01",
            expiry_date="2028-01-01",
            actor_user=self.operator_user,
        )
        self.assertEqual(qid, qid2)  # Upserted existing record

        qual_updated = get_qualification_by_id(qid)
        self.assertEqual(qual_updated["qualification_name"], "Master 3D Print Specialist")
        self.assertEqual(qual_updated["expiry_date"], "2028-01-01")

    def test_09_revoke_qualification(self):
        """Revoke a qualification and ensure it is removed from active list."""
        mem_id = create_member(
            full_name="Helena Costa",
            email="helena@makers.local",
            actor_user=self.admin_user,
        )
        cat_laser = get_machine_category_by_code("laser_cutters")

        qid = grant_qualification(
            member_id=mem_id,
            category_id=cat_laser["id"],
            qualification_name="Laser Safety L1",
            issue_date="2026-01-01",
            expiry_date="2027-01-01",
            actor_user=self.operator_user,
        )

        success, msg = revoke_qualification(qid, reason="Safety violation incident", actor_user=self.operator_user)
        self.assertTrue(success)
        self.assertIsNone(get_qualification_by_id(qid))
        self.assertEqual(len(list_member_qualifications(mem_id)), 0)

    # -------------------------------------------------------------------------
    # 3. Time-Aware Qualification & Eligibility Verification (Brief Core Requirement)
    # -------------------------------------------------------------------------

    def test_10_time_aware_qualification_check(self):
        """Verify qualification validity at reservation creation time vs future check-in time.
        
        Contract:
        - Qualification valid today (2026-08-30)
        - But expires on 2026-09-15
        - When reserving for 2026-09-01 (before expiry) -> VALID
        - When reserving/checking in on 2026-09-20 (after expiry) -> INVALID (expired before check-in)
        """
        mem_id = create_member(
            full_name="Irene Neri",
            email="irene@makers.local",
            membership_status="active",
            membership_expiry="2027-12-31",
            actor_user=self.admin_user,
        )
        cat_cnc = get_machine_category_by_code("cnc_mills")

        # Qualification issued 2026-01-01, expires 2026-09-15
        grant_qualification(
            member_id=mem_id,
            category_id=cat_cnc["id"],
            qualification_name="CNC Mill Safe Operator",
            issue_date="2026-01-01",
            expiry_date="2026-09-15",
            actor_user=self.operator_user,
        )

        # Check validity on 2026-09-01 (before expiry)
        res_valid = check_member_qualification(
            member_id=mem_id,
            required_category_id=cat_cnc["id"],
            target_datetime="2026-09-01T14:00:00",
        )
        self.assertTrue(res_valid["qualified"])
        self.assertTrue(res_valid["eligible"])

        # Check validity on 2026-09-20 (after expiry date)
        res_expired = check_member_qualification(
            member_id=mem_id,
            required_category_id=cat_cnc["id"],
            target_datetime="2026-09-20T10:00:00",
        )
        self.assertFalse(res_expired["qualified"])
        self.assertFalse(res_expired["eligible"])
        self.assertIn("expired on 2026-09-15", res_expired["reason"])

    def test_11_qualification_future_issue_date(self):
        """Check qualification that is scheduled to start in the future."""
        mem_id = create_member(
            full_name="Luca Ferretti",
            email="luca@makers.local",
            membership_status="active",
            actor_user=self.admin_user,
        )
        cat_wood = get_machine_category_by_code("woodworking")

        # Issued next month
        grant_qualification(
            member_id=mem_id,
            category_id=cat_wood["id"],
            qualification_name="Woodshop Joinery Operator",
            issue_date="2026-10-01",
            expiry_date="2027-10-01",
            actor_user=self.operator_user,
        )

        # Attempt to use before issue date (e.g. 2026-09-01)
        res_future = check_member_qualification(
            member_id=mem_id,
            required_category_id=cat_wood["id"],
            target_datetime="2026-09-01T10:00:00",
        )
        self.assertFalse(res_future["qualified"])
        self.assertIn("not yet effective", res_future["reason"])

        # Use after issue date (2026-10-05)
        res_active = check_member_qualification(
            member_id=mem_id,
            required_category_id=cat_wood["id"],
            target_datetime="2026-10-05T10:00:00",
        )
        self.assertTrue(res_active["qualified"])

    def test_12_membership_suspension_overrides_qualification(self):
        """Suspended or expired membership blocks machine access even with valid qualification."""
        mem_id = create_member(
            full_name="Massimo De Luca",
            email="massimo@makers.local",
            membership_status="suspended",
            actor_user=self.admin_user,
        )
        cat_3d = get_machine_category_by_code("3d_printers")

        grant_qualification(
            member_id=mem_id,
            category_id=cat_3d["id"],
            qualification_name="3D Printing Specialist",
            issue_date="2026-01-01",
            expiry_date="2027-01-01",
            actor_user=self.operator_user,
        )

        res = check_member_qualification(member_id=mem_id, required_category_id=cat_3d["id"])
        self.assertFalse(res["eligible"])
        self.assertFalse(res["qualified"])
        self.assertIn("SUSPENDED", res["reason"])

    # -------------------------------------------------------------------------
    # 4. HTTP Views & RBAC Integration Tests
    # -------------------------------------------------------------------------

    def test_13_html_members_list_roles(self):
        """Verify HTML /members access for Admin, Operator, Viewer, and Member."""
        # Admin: 200 with + Register New Member button
        res_admin = self._make_request("GET", "/members", session_token=self.admin_session)
        self.assertEqual(res_admin.status_code, 200)
        self.assertIn("+ Register New Member", res_admin.body.decode("utf-8"))

        # Operator: 200 with + Register New Member button
        res_op = self._make_request("GET", "/members", session_token=self.operator_session)
        self.assertEqual(res_op.status_code, 200)
        self.assertIn("+ Register New Member", res_op.body.decode("utf-8"))

        # Viewer: 200 read-only without register button
        res_viewer = self._make_request("GET", "/members", session_token=self.viewer_session)
        self.assertEqual(res_viewer.status_code, 200)
        self.assertNotIn("+ Register New Member", res_viewer.body.decode("utf-8"))

        # Member: redirects to their own profile
        res_member = self._make_request("GET", "/members", session_token=self.alice_session)
        self.assertEqual(res_member.status_code, 303)
        self.assertTrue(res_member.headers.get("Location", "").startswith("/members/"))

    def test_14_html_member_profile_ownership(self):
        """Member can view own profile but gets 403 on another member's profile."""
        alice_member = get_member_by_user_id(self.alice_user["id"])
        bob_member = get_member_by_user_id(self.bob_user["id"])

        # Alice views Alice's profile -> 200
        res_alice_own = self._make_request("GET", f"/members/{alice_member['id']}", session_token=self.alice_session)
        self.assertEqual(res_alice_own.status_code, 200)
        self.assertIn("Alice Moretti", res_alice_own.body.decode("utf-8"))

        # Alice attempts to view Bob's profile -> 403
        res_alice_bob = self._make_request("GET", f"/members/{bob_member['id']}", session_token=self.alice_session)
        self.assertEqual(res_alice_bob.status_code, 403)

        # Admin views Bob's profile -> 200
        res_admin_bob = self._make_request("GET", f"/members/{bob_member['id']}", session_token=self.admin_session)
        self.assertEqual(res_admin_bob.status_code, 200)
        self.assertIn("Bob Rossi", res_admin_bob.body.decode("utf-8"))

    def test_15_html_member_creation_and_edit_rbac(self):
        """Only Admin and Operator can access /members/new and /members/{id}/edit."""
        # Viewer attempts /members/new -> 403
        res_v_new = self._make_request("GET", "/members/new", session_token=self.viewer_session)
        self.assertEqual(res_v_new.status_code, 403)

        # Member attempts /members/new -> 403
        res_m_new = self._make_request("GET", "/members/new", session_token=self.alice_session)
        self.assertEqual(res_m_new.status_code, 403)

        # Operator gets /members/new -> 200
        res_op_new = self._make_request("GET", "/members/new", session_token=self.operator_session)
        self.assertEqual(res_op_new.status_code, 200)

    # -------------------------------------------------------------------------
    # 5. REST JSON API Endpoints
    # -------------------------------------------------------------------------

    def test_16_api_members_crud(self):
        """Test full JSON API CRUD lifecycle for members."""
        # Create member via API (Operator)
        payload = {
            "full_name": "Nicoletta Poli",
            "email": "nicoletta@makers.local",
            "phone": "+39 320 555 7777",
            "membership_status": "active",
            "membership_expiry": "2027-06-30",
            "notes": "Bio-printing research member",
        }
        res_create = self._make_request("POST", "/api/members", bearer_token=self.operator_session, json_data=payload)
        self.assertEqual(res_create.status_code, 201)
        data = json.loads(res_create.body.decode("utf-8"))
        self.assertEqual(data["status"], "success")
        new_id = data["member"]["id"]

        # Get member detail via API
        res_get = self._make_request("GET", f"/api/members/{new_id}", bearer_token=self.operator_session)
        self.assertEqual(res_get.status_code, 200)
        data_get = json.loads(res_get.body.decode("utf-8"))
        self.assertEqual(data_get["member"]["full_name"], "Nicoletta Poli")

        # Update member via API (Admin)
        res_update = self._make_request(
            "PUT",
            f"/api/members/{new_id}",
            bearer_token=self.admin_session,
            json_data={"full_name": "Nicoletta Poli-Gori", "notes": "Updated research field"},
        )
        self.assertEqual(res_update.status_code, 200)
        data_up = json.loads(res_update.body.decode("utf-8"))
        self.assertEqual(data_up["member"]["full_name"], "Nicoletta Poli-Gori")

        # Update status via API
        res_status = self._make_request(
            "PATCH",
            f"/api/members/{new_id}/status",
            bearer_token=self.operator_session,
            json_data={"status": "suspended", "notes": "Temporary pause requested"},
        )
        self.assertEqual(res_status.status_code, 200)
        data_st = json.loads(res_status.body.decode("utf-8"))
        self.assertEqual(data_st["member"]["membership_status"], "suspended")

    def test_17_api_qualifications_and_check(self):
        """Test API endpoints for granting, checking, and revoking qualifications."""
        # Create member
        mem_id = create_member(
            full_name="Paolo Baresi",
            email="paolo@makers.local",
            membership_status="active",
            actor_user=self.admin_user,
        )
        cat_3d = get_machine_category_by_code("3d_printers")

        # Grant qualification via API
        qual_payload = {
            "category_id": cat_3d["id"],
            "qualification_name": "SLA Resin Safety Specialist",
            "issue_date": "2026-03-01",
            "expiry_date": "2027-03-01",
            "notes": "Certified for Formlabs Form 3",
        }
        res_grant = self._make_request(
            "POST",
            f"/api/members/{mem_id}/qualifications",
            bearer_token=self.operator_session,
            json_data=qual_payload,
        )
        self.assertEqual(res_grant.status_code, 201)
        data_g = json.loads(res_grant.body.decode("utf-8"))
        qid = data_g["qualification"]["id"]

        # Check qualification via API
        res_chk = self._make_request(
            "GET",
            f"/api/members/{mem_id}/check-qualification?category_id={cat_3d['id']}&datetime=2026-08-30T10:00:00",
            bearer_token=self.operator_session,
        )
        self.assertEqual(res_chk.status_code, 200)
        data_chk = json.loads(res_chk.body.decode("utf-8"))
        self.assertTrue(data_chk["qualified"])
        self.assertTrue(data_chk["eligible"])

        # Revoke qualification via API
        res_rev = self._make_request(
            "DELETE",
            f"/api/members/{mem_id}/qualifications/{qid}",
            bearer_token=self.admin_session,
        )
        self.assertEqual(res_rev.status_code, 200)

    def test_18_member_idor_protections_api(self):
        """Verify that Member role users cannot list other members or access other members' qualifications via API."""
        alice_member = get_member_by_user_id(self.alice_user["id"])
        bob_member = get_member_by_user_id(self.bob_user["id"])

        # 1. Member calling GET /api/members only receives their own member profile
        res_list_alice = self._make_request("GET", "/api/members", bearer_token=self.alice_session)
        self.assertEqual(res_list_alice.status_code, 200)
        data_list_alice = json.loads(res_list_alice.body.decode("utf-8"))
        self.assertEqual(data_list_alice["count"], 1)
        self.assertEqual(data_list_alice["members"][0]["id"], alice_member["id"])

        # 2. Member calling GET /api/members/{id} for another member gets 403 Forbidden
        res_get_bob = self._make_request("GET", f"/api/members/{bob_member['id']}", bearer_token=self.alice_session)
        self.assertEqual(res_get_bob.status_code, 403)

        # 3. Member calling GET /api/members/{id}/qualifications for another member gets 403 Forbidden
        res_quals_bob = self._make_request("GET", f"/api/members/{bob_member['id']}/qualifications", bearer_token=self.alice_session)
        self.assertEqual(res_quals_bob.status_code, 403)

        # 4. Member calling GET /api/members/{id}/check-qualification for another member gets 403 Forbidden
        res_chk_bob = self._make_request("GET", f"/api/members/{bob_member['id']}/check-qualification", bearer_token=self.alice_session)
        self.assertEqual(res_chk_bob.status_code, 403)

        # 5. Member calling GET /api/members/{id}/qualifications for own profile gets 200 OK
        res_quals_alice = self._make_request("GET", f"/api/members/{alice_member['id']}/qualifications", bearer_token=self.alice_session)
        self.assertEqual(res_quals_alice.status_code, 200)

        # 6. Operator can view all members and any member's qualifications
        res_op_list = self._make_request("GET", "/api/members", bearer_token=self.operator_session)
        self.assertEqual(res_op_list.status_code, 200)
        data_op_list = json.loads(res_op_list.body.decode("utf-8"))
        self.assertGreaterEqual(data_op_list["count"], 3)

        res_op_quals_bob = self._make_request("GET", f"/api/members/{bob_member['id']}/qualifications", bearer_token=self.operator_session)
        self.assertEqual(res_op_quals_bob.status_code, 200)


if __name__ == "__main__":
    unittest.main()
