"""Comprehensive test suite for S08 Security Remediation.

Verifies fixes for:
1. SEC-REQ-BODY-UNBOUNDED: Bounded request bodies & 413 Payload Too Large rejection.
2. SEC-CSRF-MISSING: CSRF token validation on cookie-authenticated state-changing operations.
3. SEC-PASSWORD-SESSION-REVOCATION: Complete session revocation on password updates.
4. SEC-ERROR-DISCLOSURE: Sanitized 404/500 responses without tracebacks or unescaped HTML.
5. SEC-SESSION-TRANSPORT-ATREST: SHA-256 session token hashing at rest & Secure cookie flags.
"""

import io
import json
import sqlite3
import unittest

from app import create_application_router
from forgedesk.auth.middleware import COOKIE_SESSION_NAME
from forgedesk.auth.service import (
    authenticate_user,
    create_user_session,
    get_user_by_session,
    update_user_password,
)
from forgedesk.config import COOKIE_SECURE, MAX_REQUEST_BODY_SIZE, SECRET_KEY
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.db import get_connection, init_db, reset_database, seed_database
from forgedesk.server import ForgeDeskHTTPRequestHandler
from forgedesk.utils.crypto import generate_csrf_token, hash_session_token


class DummyRFile:
    """Mock rfile for testing BaseHTTPRequestHandler body reads."""
    def __init__(self, data: bytes):
        self.stream = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


class DummyHandler(ForgeDeskHTTPRequestHandler):
    """Subclass of ForgeDeskHTTPRequestHandler for unit testing request handling."""
    def __init__(self, method: str, path: str, headers: dict, body: bytes = b"", client_ip: str = "127.0.0.1"):
        self.command = method
        self.path = path
        self.headers = headers
        self.rfile = DummyRFile(body)
        self.wfile = io.BytesIO()
        self.client_address = (client_ip, 12345)
        self.sent_status = None
        self.sent_headers = {}
        self.sent_body = b""

    def send_response(self, code: int, message: str = None):
        self.sent_status = code

    def send_header(self, keyword: str, value: str):
        self.sent_headers[keyword] = value

    def end_headers(self):
        pass

    def _send_response(self, response: Response) -> None:
        self.sent_status = response.status_code
        self.sent_headers = response.headers
        self.sent_body = response.body


class TestSecurityRemediation(unittest.TestCase):
    """Test suite covering the 5 security remediations implemented in S08."""

    @classmethod
    def setUpClass(cls):
        reset_database()
        init_db(seed_if_empty=False)
        seed_database(force=True)

    def setUp(self):
        self.router = create_application_router()
        ForgeDeskHTTPRequestHandler.router = self.router

    # -------------------------------------------------------------------------
    # 1. SEC-REQ-BODY-UNBOUNDED Tests
    # -------------------------------------------------------------------------

    def test_01_request_body_oversized_rejected_with_413(self):
        """Oversized request bodies exceeding MAX_REQUEST_BODY_SIZE must be rejected with 413 without reading."""
        oversized_len = MAX_REQUEST_BODY_SIZE + 1024
        handler = DummyHandler(
            method="POST",
            path="/api/auth/login",
            headers={"Content-Length": str(oversized_len), "Content-Type": "application/json"},
            body=b"A" * 100,
        )
        handler._handle_request("POST")
        self.assertEqual(handler.sent_status, 413)
        self.assertIn(b"413 Payload Too Large", handler.sent_body)

    def test_02_invalid_content_length_header_rejected_with_400(self):
        """Malformed or negative Content-Length headers must be rejected with 400 Bad Request."""
        handler_invalid = DummyHandler(
            method="POST",
            path="/api/auth/login",
            headers={"Content-Length": "not-a-number", "Content-Type": "application/json"},
            body=b"",
        )
        handler_invalid._handle_request("POST")
        self.assertEqual(handler_invalid.sent_status, 400)

        handler_negative = DummyHandler(
            method="POST",
            path="/api/auth/login",
            headers={"Content-Length": "-500", "Content-Type": "application/json"},
            body=b"",
        )
        handler_negative._handle_request("POST")
        self.assertEqual(handler_negative.sent_status, 400)

    def test_03_bounded_request_body_accepted_and_parsed(self):
        """Normal request bodies within bounded limit must be parsed and processed successfully."""
        login_payload = json.dumps({"username": "admin", "password": "admin123"}).encode("utf-8")
        handler = DummyHandler(
            method="POST",
            path="/api/auth/login",
            headers={"Content-Length": str(len(login_payload)), "Content-Type": "application/json"},
            body=login_payload,
        )
        handler._handle_request("POST")
        self.assertEqual(handler.sent_status, 200)

    # -------------------------------------------------------------------------
    # 2. SEC-CSRF-MISSING Tests
    # -------------------------------------------------------------------------

    def test_04_csrf_required_for_cookie_authenticated_state_changing_requests(self):
        """State-changing requests authenticated via session cookie must require valid CSRF token."""
        admin_token = create_user_session(1)
        valid_csrf = generate_csrf_token(admin_token, SECRET_KEY)

        # 1. Missing CSRF token on cookie-authenticated role update -> 403 Forbidden
        req_missing_csrf = Request(
            method="POST",
            path="/api/auth/users/3/role",
            headers={
                "Cookie": f"{COOKIE_SESSION_NAME}={admin_token}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"role": "viewer"}).encode("utf-8"),
        )
        res_missing = self.router.dispatch(req_missing_csrf)
        self.assertEqual(res_missing.status_code, 403)
        data_missing = json.loads(res_missing.body.decode("utf-8"))
        self.assertIn("CSRF verification failed", data_missing.get("message", ""))

        # 2. Invalid CSRF token on cookie-authenticated request -> 403 Forbidden
        req_invalid_csrf = Request(
            method="POST",
            path="/api/auth/users/3/role",
            headers={
                "Cookie": f"{COOKIE_SESSION_NAME}={admin_token}",
                "Content-Type": "application/json",
                "X-CSRF-Token": "invalid_csrf_token_value_xyz",
            },
            body=json.dumps({"role": "viewer"}).encode("utf-8"),
        )
        res_invalid = self.router.dispatch(req_invalid_csrf)
        self.assertEqual(res_invalid.status_code, 403)

        # 3. Cross-session CSRF token (belonging to another session) -> 403 Forbidden
        other_session_token = create_user_session(2)
        other_csrf = generate_csrf_token(other_session_token, SECRET_KEY)

        req_cross_csrf = Request(
            method="POST",
            path="/api/auth/users/3/role",
            headers={
                "Cookie": f"{COOKIE_SESSION_NAME}={admin_token}",
                "Content-Type": "application/json",
                "X-CSRF-Token": other_csrf,
            },
            body=json.dumps({"role": "viewer"}).encode("utf-8"),
        )
        res_cross = self.router.dispatch(req_cross_csrf)
        self.assertEqual(res_cross.status_code, 403)

        # 4. Valid CSRF token in header -> 200 OK
        req_valid_csrf_header = Request(
            method="POST",
            path="/api/auth/users/3/role",
            headers={
                "Cookie": f"{COOKIE_SESSION_NAME}={admin_token}",
                "Content-Type": "application/json",
                "X-CSRF-Token": valid_csrf,
            },
            body=json.dumps({"role": "viewer"}).encode("utf-8"),
        )
        res_valid_header = self.router.dispatch(req_valid_csrf_header)
        self.assertEqual(res_valid_header.status_code, 200)

        # 5. Valid CSRF token in JSON body -> 200 OK
        req_valid_csrf_body = Request(
            method="POST",
            path="/api/auth/users/3/role",
            headers={
                "Cookie": f"{COOKIE_SESSION_NAME}={admin_token}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"role": "member", "csrf_token": valid_csrf}).encode("utf-8"),
        )
        res_valid_body = self.router.dispatch(req_valid_csrf_body)
        self.assertEqual(res_valid_body.status_code, 200)

    def test_05_csrf_not_required_for_bearer_token_api_requests(self):
        """API requests using pure Authorization: Bearer <token> headers are not subject to ambient cookie CSRF."""
        admin_token = create_user_session(1)

        req_bearer = Request(
            method="POST",
            path="/api/auth/users/3/role",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"role": "member"}).encode("utf-8"),
        )
        res_bearer = self.router.dispatch(req_bearer)
        self.assertEqual(res_bearer.status_code, 200)

    # -------------------------------------------------------------------------
    # 3. SEC-PASSWORD-SESSION-REVOCATION Tests
    # -------------------------------------------------------------------------

    def test_06_password_change_invalidates_all_prior_sessions(self):
        """When a user changes their password, all previous sessions must be revoked immediately."""
        user = authenticate_user("member_alice", "member123")
        self.assertIsNotNone(user)
        user_id = user["id"]

        # Create two existing sessions (e.g. laptop and mobile)
        session_1 = create_user_session(user_id)
        session_2 = create_user_session(user_id)

        self.assertIsNotNone(get_user_by_session(session_1))
        self.assertIsNotNone(get_user_by_session(session_2))

        # Perform password update via profile action
        csrf_tok = generate_csrf_token(session_1, SECRET_KEY)
        body = f"current_password=member123&new_password=newSecretPass123&csrf_token={csrf_tok}".encode("utf-8")
        req_pwd_change = Request(
            method="POST",
            path="/auth/profile/password",
            headers={
                "Cookie": f"{COOKIE_SESSION_NAME}={session_1}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=body,
        )
        res_pwd = self.router.dispatch(req_pwd_change)
        self.assertEqual(res_pwd.status_code, 303)

        # 1. Verify that the old sessions are completely invalidated
        self.assertIsNone(get_user_by_session(session_1))
        self.assertIsNone(get_user_by_session(session_2))

        # 2. Verify that a new session cookie was set in the response
        cookie_header = next((c for c in res_pwd._cookies if COOKIE_SESSION_NAME in c), None)
        self.assertIsNotNone(cookie_header)

        # Extract new token from cookie string (e.g., fd_session=<token>; Path=/...)
        new_token = cookie_header.split(";")[0].split("=")[1].strip()
        new_session_user = get_user_by_session(new_token)
        self.assertIsNotNone(new_session_user)
        self.assertEqual(new_session_user["id"], user_id)

        # Restore original password for other tests
        update_user_password(user_id, "member123", invalidate_sessions=True)

    # -------------------------------------------------------------------------
    # 4. SEC-ERROR-DISCLOSURE Tests
    # -------------------------------------------------------------------------

    def test_07_not_found_escapes_html_and_prevents_xss(self):
        """404 error responses must properly escape requested paths to prevent HTML/XSS injection."""
        xss_path = '/nonexistent/<script>alert("xss")</script>'
        req = Request(method="GET", path=xss_path, headers={})
        res = self.router.dispatch(req)
        self.assertEqual(res.status_code, 404)
        body_text = res.body.decode("utf-8")
        self.assertNotIn("<script>", body_text)
        self.assertIn("&lt;script&gt;", body_text)

    def test_08_internal_errors_do_not_disclose_traceback(self):
        """500 Internal Server Errors must return generic error messages without exposing tracebacks or queries."""
        # Create a test router with a handler that intentionally raises an exception
        test_router = Router()

        @test_router.get("/error-test")
        def crashing_handler(req: Request) -> Response:
            raise RuntimeError("Database connection string leaked /sensitive/path/database.sqlite")

        @test_router.get("/api/error-test")
        def crashing_api_handler(req: Request) -> Response:
            raise RuntimeError("Database connection string leaked /sensitive/path/database.sqlite")

        # HTML Request from loopback client
        req_html = Request(method="GET", path="/error-test", headers={}, client_address=("127.0.0.1", 54321))
        res_html = test_router.dispatch(req_html)
        self.assertEqual(res_html.status_code, 500)
        body_html = res_html.body.decode("utf-8")
        self.assertNotIn("/sensitive/path/database.sqlite", body_html)
        self.assertNotIn("RuntimeError", body_html)
        self.assertNotIn("Traceback (most recent call last)", body_html)
        self.assertIn("500 Internal Server Error", body_html)

        # JSON API Request
        req_api = Request(method="GET", path="/api/error-test", headers={"Accept": "application/json"})
        res_api = test_router.dispatch(req_api)
        self.assertEqual(res_api.status_code, 500)
        data_api = json.loads(res_api.body.decode("utf-8"))
        self.assertEqual(data_api.get("error"), "Internal Server Error")
        self.assertNotIn("sensitive", data_api.get("message", ""))


    # -------------------------------------------------------------------------
    # 5. SEC-SESSION-TRANSPORT-ATREST Tests
    # -------------------------------------------------------------------------

    def test_09_session_tokens_hashed_at_rest_in_database(self):
        """Session bearer tokens must be stored as SHA-256 hashes in SQLite, never as plaintext."""
        user = authenticate_user("admin", "admin123")
        raw_token = create_user_session(user["id"])
        expected_hash = hash_session_token(raw_token)

        conn = get_connection()
        try:
            row = conn.execute("SELECT session_token FROM sessions WHERE session_token = ?;", (expected_hash,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["session_token"], expected_hash)

            # Confirm raw token is nowhere in the sessions table
            raw_match = conn.execute("SELECT id FROM sessions WHERE session_token = ?;", (raw_token,)).fetchone()
            self.assertIsNone(raw_match)
        finally:
            conn.close()

    def test_10_cookie_security_flag_configuration(self):
        """Verify Response.set_cookie correctly sets secure=True when configured."""
        res = Response.html("<h1>OK</h1>")
        res.set_cookie("test_cookie", "val", secure=True, http_only=True, same_site="Lax")
        cookie_header = res._cookies[0].lower()
        self.assertIn("secure", cookie_header)
        self.assertIn("httponly", cookie_header)
        self.assertIn("samesite=lax", cookie_header)


if __name__ == "__main__":
    unittest.main()
