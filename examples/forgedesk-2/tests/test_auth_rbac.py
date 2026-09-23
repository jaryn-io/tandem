"""Comprehensive unit and integration test suite for RBAC, 4-role authorization, and server-side permission enforcement."""

import json
import unittest

from forgedesk.auth.middleware import (
    COOKIE_SESSION_NAME,
    require_auth,
    require_permission,
    require_role,
)
from forgedesk.auth.permissions import (
    ALL_PERMISSIONS,
    PERM_AUDIT_VIEW,
    PERM_CHARGES_ADJUST,
    PERM_CHARGES_MANAGE,
    PERM_CHARGES_VIEW_ALL,
    PERM_CHARGES_VIEW_OWN,
    PERM_INVENTORY_CONSUME,
    PERM_INVENTORY_MANAGE,
    PERM_INVENTORY_VIEW,
    PERM_MACHINES_MANAGE,
    PERM_MACHINES_VIEW,
    PERM_MAINTENANCE_MANAGE,
    PERM_MAINTENANCE_VIEW,
    PERM_MEMBERS_MANAGE,
    PERM_MEMBERS_VIEW_ALL,
    PERM_MEMBERS_VIEW_OWN,
    PERM_RESERVATIONS_CHECKIN_ALL,
    PERM_RESERVATIONS_CHECKIN_OWN,
    PERM_RESERVATIONS_CREATE,
    PERM_RESERVATIONS_MANAGE_ALL,
    PERM_RESERVATIONS_MANAGE_OWN,
    PERM_RESERVATIONS_VIEW_ALL,
    PERM_RESERVATIONS_VIEW_OWN,
    PERM_SECURITY_MANAGE,
    PERM_SYSTEM_BACKUP,
    PERM_SYSTEM_EXPORT,
    PERM_SYSTEM_IMPORT,
    PERM_SYSTEM_RESET,
    PERM_SYSTEM_RESTORE,
    PERM_USERS_MANAGE_ROLES,
    PERM_USERS_TOGGLE_ACTIVE,
    PERM_USERS_VIEW,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OPERATOR,
    ROLE_PERMISSIONS,
    ROLE_VIEWER,
    ROLES,
    can_adjust_charges,
    can_checkin_reservation,
    can_manage_reservation,
    can_manage_security_settings,
    can_manage_user_roles,
    can_view_charges,
    get_user_permissions,
    get_user_role,
    has_all_permissions,
    has_any_permission,
    has_permission,
    is_admin,
    is_operator,
    is_operator_or_admin,
)
from forgedesk.auth.service import (
    authenticate_user,
    create_user_session,
    get_user_by_id,
    get_user_by_session,
    list_users,
    toggle_user_active_status,
    update_user_role,
)
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.db import init_db, reset_database, seed_database
from app import create_application_router


class TestAuthRBAC(unittest.TestCase):
    """Test RBAC role models, permissions matrices, and resource-level access control."""

    @classmethod
    def setUpClass(cls):
        reset_database()
        init_db(seed_if_empty=False)
        seed_database(force=True)

    def setUp(self):
        self.router = create_application_router()

    def test_01_role_definitions_and_hierarchy(self):
        """Verify the 4 standard roles and their assigned permission sets."""
        self.assertEqual(len(ROLES), 4)
        self.assertIn(ROLE_ADMIN, ROLES)
        self.assertIn(ROLE_OPERATOR, ROLES)
        self.assertIn(ROLE_MEMBER, ROLES)
        self.assertIn(ROLE_VIEWER, ROLES)

        # Admin must have all permissions
        admin_perms = ROLE_PERMISSIONS[ROLE_ADMIN]
        self.assertEqual(admin_perms, ALL_PERMISSIONS)

        # Operator must have operational permissions, but CANNOT manage security settings or user roles
        op_perms = ROLE_PERMISSIONS[ROLE_OPERATOR]
        self.assertIn(PERM_MACHINES_MANAGE, op_perms)
        self.assertIn(PERM_RESERVATIONS_MANAGE_ALL, op_perms)
        self.assertIn(PERM_INVENTORY_MANAGE, op_perms)
        self.assertIn(PERM_MAINTENANCE_MANAGE, op_perms)
        self.assertIn(PERM_AUDIT_VIEW, op_perms)
        self.assertNotIn(PERM_SECURITY_MANAGE, op_perms)
        self.assertNotIn(PERM_USERS_MANAGE_ROLES, op_perms)
        self.assertNotIn(PERM_CHARGES_ADJUST, op_perms)
        self.assertNotIn(PERM_SYSTEM_RESET, op_perms)
        self.assertNotIn(PERM_SYSTEM_RESTORE, op_perms)

        # Member can manage own reservations/usage, view machines/catalog
        member_perms = ROLE_PERMISSIONS[ROLE_MEMBER]
        self.assertIn(PERM_RESERVATIONS_CREATE, member_perms)
        self.assertIn(PERM_RESERVATIONS_MANAGE_OWN, member_perms)
        self.assertIn(PERM_RESERVATIONS_CHECKIN_OWN, member_perms)
        self.assertIn(PERM_CHARGES_VIEW_OWN, member_perms)
        self.assertNotIn(PERM_RESERVATIONS_MANAGE_ALL, member_perms)
        self.assertNotIn(PERM_MACHINES_MANAGE, member_perms)
        self.assertNotIn(PERM_INVENTORY_MANAGE, member_perms)

        # Viewer has read-only access to permitted operational info
        viewer_perms = ROLE_PERMISSIONS[ROLE_VIEWER]
        self.assertIn(PERM_MACHINES_VIEW, viewer_perms)
        self.assertIn(PERM_RESERVATIONS_VIEW_ALL, viewer_perms)
        self.assertIn(PERM_INVENTORY_VIEW, viewer_perms)
        self.assertNotIn(PERM_RESERVATIONS_CREATE, viewer_perms)
        self.assertNotIn(PERM_RESERVATIONS_MANAGE_OWN, viewer_perms)
        self.assertNotIn(PERM_INVENTORY_CONSUME, viewer_perms)

    def test_02_permission_helper_functions(self):
        """Verify has_permission, has_any_permission, and has_all_permissions with user contexts."""
        admin_user = {"id": 1, "username": "admin", "role": "admin"}
        operator_user = {"id": 2, "username": "marina_ops", "role": "operator"}
        member_user = {"id": 3, "username": "alice", "role": "member", "member_id": 1}
        viewer_user = {"id": 6, "username": "giorgio_viewer", "role": "viewer"}

        # Admin checks
        self.assertTrue(is_admin(admin_user))
        self.assertTrue(is_operator_or_admin(admin_user))
        self.assertTrue(has_permission(admin_user, PERM_SECURITY_MANAGE))
        self.assertTrue(has_permission(admin_user, PERM_USERS_MANAGE_ROLES))
        self.assertTrue(has_permission(admin_user, PERM_CHARGES_ADJUST))

        # Operator checks
        self.assertFalse(is_admin(operator_user))
        self.assertTrue(is_operator(operator_user))
        self.assertTrue(is_operator_or_admin(operator_user))
        self.assertFalse(has_permission(operator_user, PERM_SECURITY_MANAGE))
        self.assertFalse(has_permission(operator_user, PERM_USERS_MANAGE_ROLES))
        self.assertFalse(has_permission(operator_user, PERM_CHARGES_ADJUST))
        self.assertTrue(has_permission(operator_user, PERM_RESERVATIONS_MANAGE_ALL))

        # Member checks
        self.assertFalse(is_operator_or_admin(member_user))
        self.assertTrue(has_permission(member_user, PERM_RESERVATIONS_CREATE))
        self.assertTrue(has_permission(member_user, PERM_RESERVATIONS_MANAGE_OWN))
        self.assertFalse(has_permission(member_user, PERM_RESERVATIONS_MANAGE_ALL))

        # Viewer checks
        self.assertFalse(is_operator_or_admin(viewer_user))
        self.assertTrue(has_permission(viewer_user, PERM_MACHINES_VIEW))
        self.assertFalse(has_permission(viewer_user, PERM_RESERVATIONS_CREATE))

    def test_03_resource_level_authorization(self):
        """Verify granular resource-level ownership checks (e.g. reservations, charges)."""
        admin_user = {"id": 1, "username": "admin", "role": "admin", "member_id": None}
        operator_user = {"id": 2, "username": "marina_ops", "role": "operator", "member_id": None}
        alice_user = {"id": 3, "username": "alice", "role": "member", "member_id": 1}
        bob_user = {"id": 4, "username": "bob", "role": "member", "member_id": 2}
        viewer_user = {"id": 6, "username": "giorgio_viewer", "role": "viewer", "member_id": None}

        # Reservation ownership checks
        # Alice's reservation (member_id=1, user_id=3)
        self.assertTrue(can_manage_reservation(alice_user, reservation_member_id=1, reservation_user_id=3))
        self.assertTrue(can_checkin_reservation(alice_user, reservation_member_id=1, reservation_user_id=3))

        # Bob cannot manage Alice's reservation
        self.assertFalse(can_manage_reservation(bob_user, reservation_member_id=1, reservation_user_id=3))
        self.assertFalse(can_checkin_reservation(bob_user, reservation_member_id=1, reservation_user_id=3))

        # Viewer cannot manage Alice's reservation
        self.assertFalse(can_manage_reservation(viewer_user, reservation_member_id=1, reservation_user_id=3))

        # Admin and Operator CAN manage Alice's reservation
        self.assertTrue(can_manage_reservation(admin_user, reservation_member_id=1, reservation_user_id=3))
        self.assertTrue(can_manage_reservation(operator_user, reservation_member_id=1, reservation_user_id=3))
        self.assertTrue(can_checkin_reservation(operator_user, reservation_member_id=1, reservation_user_id=3))

        # Charges viewing checks
        self.assertTrue(can_view_charges(alice_user, target_member_id=1, target_user_id=3))
        self.assertFalse(can_view_charges(bob_user, target_member_id=1, target_user_id=3))
        self.assertFalse(can_view_charges(viewer_user, target_member_id=1, target_user_id=3))
        self.assertTrue(can_view_charges(operator_user, target_member_id=1, target_user_id=3))
        self.assertTrue(can_view_charges(admin_user, target_member_id=1, target_user_id=3))

        # Charges adjustment check (Admin only)
        self.assertTrue(can_adjust_charges(admin_user))
        self.assertFalse(can_adjust_charges(operator_user))
        self.assertFalse(can_adjust_charges(alice_user))
        self.assertFalse(can_adjust_charges(viewer_user))

    def test_04_server_side_role_modification_rules(self):
        """Enforce that only Admin can modify user roles, and cannot demote the last admin."""
        admin_user = authenticate_user("admin", "admin123")
        operator_user = authenticate_user("operator", "operator123")
        member_user = authenticate_user("member_alice", "member123")

        self.assertIsNotNone(admin_user)
        self.assertIsNotNone(operator_user)
        self.assertIsNotNone(member_user)

        # 1. Operator attempts to change a user's role -> Rejected server-side
        success, msg = update_user_role(member_user["id"], "operator", operator_user)
        self.assertFalse(success)
        self.assertIn("Forbidden", msg)

        # 2. Member attempts to change a user's role -> Rejected server-side
        success, msg = update_user_role(member_user["id"], "admin", member_user)
        self.assertFalse(success)

        # 3. Admin attempts to demote the ONLY active admin -> Rejected server-side
        success, msg = update_user_role(admin_user["id"], "member", admin_user)
        self.assertFalse(success)
        self.assertIn("Cannot demote the last remaining active Administrator", msg)

        # 4. Admin updates member to viewer -> Succeeds
        success, msg = update_user_role(member_user["id"], "viewer", admin_user)
        self.assertTrue(success)
        updated = get_user_by_id(member_user["id"])
        self.assertEqual(updated["role"], "viewer")

        # Restore back to member
        success, msg = update_user_role(member_user["id"], "member", admin_user)
        self.assertTrue(success)

    def test_05_server_side_active_status_toggle_rules(self):
        """Enforce account activation rules between Admin and Operator."""
        admin_user = authenticate_user("admin", "admin123")
        operator_user = authenticate_user("operator", "operator123")
        member_user = authenticate_user("member_clara", "member123")

        self.assertIsNotNone(admin_user)
        self.assertIsNotNone(operator_user)
        self.assertIsNotNone(member_user)

        # 1. Operator attempts to disable Admin account -> Rejected
        success, msg = toggle_user_active_status(admin_user["id"], operator_user)
        self.assertFalse(success)
        self.assertIn("Operators cannot disable Administrator", msg)

        # 2. Operator toggles Member account -> Allowed
        success, msg = toggle_user_active_status(member_user["id"], operator_user)
        self.assertTrue(success)
        self.assertIn("disabled", msg)

        # Verify disabled member cannot login
        auth_res = authenticate_user("member_clara", "member123")
        self.assertIsNone(auth_res)

        # Operator re-enables Member account
        success, msg = toggle_user_active_status(member_user["id"], operator_user)
        self.assertTrue(success)
        self.assertIn("activated", msg)

        # Verify active member can login again
        auth_res = authenticate_user("member_clara", "member123")
        self.assertIsNotNone(auth_res)

    def test_06_http_middleware_and_api_server_side_enforcement(self):
        """Test HTTP responses for unauthenticated, unauthorized, and authorized requests."""
        # Unauthenticated request to /auth/users -> Redirect to /auth/login
        req_unauth = Request(method="GET", path="/auth/users", headers={})
        res_unauth = self.router.dispatch(req_unauth)
        self.assertEqual(res_unauth.status_code, 303)
        self.assertIn("/auth/login", res_unauth.headers.get("Location", ""))

        # Unauthenticated JSON API request to /api/auth/users -> 401 Unauthorized JSON
        req_api_unauth = Request(method="GET", path="/api/auth/users", headers={"Accept": "application/json"})
        res_api_unauth = self.router.dispatch(req_api_unauth)
        self.assertEqual(res_api_unauth.status_code, 401)
        data = json.loads(res_api_unauth.body.decode("utf-8"))
        self.assertEqual(data.get("error"), "Unauthorized")

        # Create session tokens for Alice (Member) and Marina (Operator) and Admin
        alice_token = create_user_session(3)
        marina_token = create_user_session(2)
        admin_token = create_user_session(1)

        # Alice (Member) tries to access /auth/users (HTML) -> 403 Forbidden HTML
        req_alice_html = Request(method="GET", path="/auth/users", headers={"Cookie": f"fd_session={alice_token}"})
        res_alice_html = self.router.dispatch(req_alice_html)
        self.assertEqual(res_alice_html.status_code, 403)
        self.assertIn("403 - Access Forbidden", res_alice_html.body.decode("utf-8"))

        # Alice (Member) tries to access /api/auth/users (API) -> 403 Forbidden JSON
        req_alice_api = Request(
            method="GET",
            path="/api/auth/users",
            headers={"Authorization": f"Bearer {alice_token}", "Accept": "application/json"},
        )
        res_alice_api = self.router.dispatch(req_alice_api)
        self.assertEqual(res_alice_api.status_code, 403)
        data = json.loads(res_alice_api.body.decode("utf-8"))
        self.assertEqual(data.get("error"), "Forbidden")
        self.assertEqual(data.get("current_role"), "member")

        # Marina (Operator) accesses /api/auth/users (API) -> 200 OK
        req_marina_api = Request(
            method="GET",
            path="/api/auth/users",
            headers={"Authorization": f"Bearer {marina_token}", "Accept": "application/json"},
        )
        res_marina_api = self.router.dispatch(req_marina_api)
        self.assertEqual(res_marina_api.status_code, 200)
        data = json.loads(res_marina_api.body.decode("utf-8"))
        self.assertIn("users", data)
        self.assertTrue(len(data["users"]) >= 6)

        # Marina (Operator) tries to change Alice's role via API -> 403 Forbidden JSON
        body_json = json.dumps({"role": "admin"}).encode("utf-8")
        req_op_change_role = Request(
            method="POST",
            path="/api/auth/users/3/role",
            headers={
                "Authorization": f"Bearer {marina_token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body_json)),
            },
            body=body_json,
        )
        res_op_change_role = self.router.dispatch(req_op_change_role)
        self.assertEqual(res_op_change_role.status_code, 403)

        # Admin changes Alice's role via API -> 200 OK
        req_admin_change_role = Request(
            method="POST",
            path="/api/auth/users/3/role",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body_json)),
            },
            body=body_json,
        )
        res_admin_change_role = self.router.dispatch(req_admin_change_role)
        self.assertEqual(res_admin_change_role.status_code, 200)
        data = json.loads(res_admin_change_role.body.decode("utf-8"))
        self.assertTrue(data.get("success"))

        # Restore Alice back to Member
        body_restore = json.dumps({"role": "member"}).encode("utf-8")
        req_admin_restore = Request(
            method="POST",
            path="/api/auth/users/3/role",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body_restore)),
            },
            body=body_restore,
        )
        self.router.dispatch(req_admin_restore)

    def test_07_introspection_and_permission_check_endpoints(self):
        """Test the introspection endpoints /api/auth/permissions and /api/auth/check-permission."""
        alice_token = create_user_session(3)

        # GET /api/auth/permissions as Alice
        req_perms = Request(
            method="GET",
            path="/api/auth/permissions",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        res_perms = self.router.dispatch(req_perms)
        self.assertEqual(res_perms.status_code, 200)
        data = json.loads(res_perms.body.decode("utf-8"))
        self.assertEqual(data.get("current_user_role"), "member")
        self.assertIn(PERM_RESERVATIONS_CREATE, data.get("current_user_permissions", []))
        self.assertNotIn(PERM_SECURITY_MANAGE, data.get("current_user_permissions", []))

        # POST /api/auth/check-permission for reservations:create
        body_check = json.dumps({"permission": "reservations:create"}).encode("utf-8")
        req_check = Request(
            method="POST",
            path="/api/auth/check-permission",
            headers={
                "Authorization": f"Bearer {alice_token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body_check)),
            },
            body=body_check,
        )
        res_check = self.router.dispatch(req_check)
        self.assertEqual(res_check.status_code, 200)
        data = json.loads(res_check.body.decode("utf-8"))
        self.assertTrue(data.get("allowed"))

        # POST /api/auth/check-permission for security:manage (must be False for Member)
        body_check2 = json.dumps({"permission": "security:manage"}).encode("utf-8")
        req_check2 = Request(
            method="POST",
            path="/api/auth/check-permission",
            headers={
                "Authorization": f"Bearer {alice_token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body_check2)),
            },
            body=body_check2,
        )
        res_check2 = self.router.dispatch(req_check2)
        self.assertEqual(res_check2.status_code, 200)
        data2 = json.loads(res_check2.body.decode("utf-8"))
        self.assertFalse(data2.get("allowed"))


if __name__ == "__main__":
    unittest.main()
