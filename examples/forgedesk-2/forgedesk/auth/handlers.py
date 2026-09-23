"""HTTP Request handlers for Authentication, Login, Logout, Roles, and User Management."""

import html
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from forgedesk.audit.service import record_audit_event
from forgedesk.auth.middleware import (
    COOKIE_SESSION_NAME,
    is_api_request,
    require_auth,
    require_permission,
    require_role,
)
from forgedesk.auth.permissions import (
    ALL_PERMISSIONS,
    PERM_SECURITY_MANAGE,
    PERM_USERS_MANAGE_ROLES,
    PERM_USERS_VIEW,
    ROLE_ADMIN,
    ROLE_DESCRIPTIONS,
    ROLE_MEMBER,
    ROLE_OPERATOR,
    ROLE_PERMISSIONS,
    ROLE_VIEWER,
    ROLES,
    get_user_permissions,
    get_user_role,
    has_permission,
    is_admin,
    is_operator,
)
from forgedesk.auth.service import (
    authenticate_user,
    create_user_session,
    get_user_by_id,
    invalidate_session,
    list_users,
    toggle_user_active_status,
    update_user_password,
    update_user_role,
)
from forgedesk.config import COOKIE_SECURE, SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.core.templates import csrf_input, escape_html, render_page
from forgedesk.db.connection import query_all, query_one
from forgedesk.db.seed import DEMO_USERS
from forgedesk.utils.crypto import hash_session_token, verify_password
from forgedesk.utils.datetime_tz import format_display, now_rome_iso

logger = logging.getLogger("forgedesk.auth.handlers")



def is_safe_redirect_url(url: Optional[str]) -> bool:
    """Validate redirect URL to prevent open redirect vulnerabilities."""
    if not url:
        return False
    # Must start with single / and not // (protocol-relative) or contain scheme
    if url.startswith("/") and not url.startswith("//") and not url.startswith("/\\"):
        parsed = urlparse(url)
        return not parsed.netloc and not parsed.scheme
    return False


def register_auth_routes(router: Router) -> None:
    """Register all authentication and user management routes onto the given router."""

    # -------------------------------------------------------------------------
    # HTML View Handlers
    # -------------------------------------------------------------------------

    @router.get("/auth/login")
    def login_view(req: Request) -> Response:
        """Render the login page with demo accounts and direct login form."""
        if req.user:
            return Response.redirect("/")

        next_url = req.query("next", "")
        safe_next = escape_html(next_url if is_safe_redirect_url(next_url) else "")
        flash_error = req.query("error", "")
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        demo_cards_html = []
        for demo in DEMO_USERS:
            username = escape_html(demo["username"])
            role = escape_html(demo["role"].upper())
            full_name = escape_html(demo["full_name"])
            password = escape_html(demo["password"])
            role_class = f"role-badge role-{demo['role']}"

            demo_cards_html.append(
                f"""
                <div class="demo-account-item">
                    <div class="demo-account-header">
                        <span class="{role_class}">{role}</span>
                        <span class="demo-username"><code>{username}</code></span>
                    </div>
                    <div class="demo-fullname">{full_name}</div>
                    <div class="demo-password">Password: <code>{password}</code></div>
                    <form action="/auth/quick-login" method="POST" style="margin-top: 0.5rem;">
                        {csrf_field}
                        <input type="hidden" name="username" value="{username}">
                        <input type="hidden" name="next" value="{safe_next}">
                        <button type="submit" class="btn btn-sm btn-outline-primary" style="width: 100%;">
                            Instant 1-Click Login &rarr;
                        </button>
                    </form>
                </div>
                """
            )

        error_banner = ""
        if flash_error:
            error_banner = f"""
            <div class="alert alert-danger" style="margin-bottom: 1.25rem;">
                <strong>Authentication Error:</strong> {escape_html(flash_error)}
            </div>
            """

        content = f"""
        <div class="auth-page-container" style="max-width: 960px; margin: 2rem auto;">
            {error_banner}

            <div class="auth-grid" style="display: grid; grid-template-columns: 1fr 1.2fr; gap: 2rem;">
                <!-- Standard Login Form -->
                <div class="card">
                    <div class="card-header">
                        <h2 class="card-title">User Login</h2>
                    </div>
                    <div class="card-body">
                        <p style="color: #64748b; font-size: 0.9rem; margin-bottom: 1.25rem;">
                            Enter your credentials to access the ForgeDesk workspace.
                        </p>

                        <form action="/auth/login" method="POST">
                            {csrf_field}
                            <input type="hidden" name="next" value="{safe_next}">

                            <div class="form-group" style="margin-bottom: 1rem;">
                                <label class="form-label" for="username">Username or Email</label>
                                <input type="text" id="username" name="username" class="form-control" required autofocus placeholder="e.g. admin or alice@makers.local">
                            </div>

                            <div class="form-group" style="margin-bottom: 1.5rem;">
                                <label class="form-label" for="password">Password</label>
                                <input type="password" id="password" name="password" class="form-control" required placeholder="Enter your password">
                            </div>

                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <button type="submit" class="btn btn-primary" style="width: 100%;">
                                    Log In to ForgeDesk
                                </button>
                            </div>
                        </form>
                    </div>
                </div>

                <!-- Demonstration Accounts Panel -->
                <div class="card" style="background-color: #fafafa; border: 1px solid #cbd5e1;">
                    <div class="card-header" style="background-color: #f1f5f9;">
                        <h3 class="card-title" style="font-size: 1rem;">⚡ Documented Demonstration Accounts</h3>
                    </div>
                    <div class="card-body">
                        <p style="font-size: 0.85rem; color: #475569; margin-bottom: 1rem;">
                            Select any pre-configured role account to immediately test authorization rules, permissions, reservations, or ledger operations:
                        </p>
                        <div class="demo-accounts-grid" style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem;">
                            {"".join(demo_cards_html)}
                        </div>
                    </div>
                </div>
            </div>
        </div>
        """
        html_doc = render_page("Sign In", content, user=None, active_nav=None, csrf_token=csrf_token)
        return Response.html(html_doc)

    @router.post("/auth/login")
    def login_action(req: Request) -> Response:
        """Process login credentials and establish session."""
        username = req.form_value("username", "").strip()
        password = req.form_value("password", "")
        next_url = req.form_value("next", "")
        ip_addr = req.client_address[0] if req.client_address else "127.0.0.1"
        user_agent = req.headers.get("user-agent", "")

        user = authenticate_user(username, password)
        if not user:
            record_audit_event(
                action="auth.login_failed",
                object_type="user",
                object_id=username or "unknown",
                actor_name="anonymous",
                ip_address=ip_addr,
                details={"attempted_identifier": username, "method": "html_form", "reason": "invalid_credentials"},
            )
            error_msg = "Invalid credentials. Please verify your username and password."
            redirect_target = f"/auth/login?error={error_msg}"
            if is_safe_redirect_url(next_url):
                redirect_target += f"&next={next_url}"
            return Response.redirect(redirect_target, status_code=303)

        # Create persistent session
        session_token = create_user_session(user["id"], ip_address=ip_addr, user_agent=user_agent)

        record_audit_event(
            action="auth.login_success",
            object_type="user",
            object_id=user["id"],
            actor=user,
            ip_address=ip_addr,
            details={"method": "html_form", "user_agent": user_agent[:128]},
        )

        target = next_url if is_safe_redirect_url(next_url) else "/"
        res = Response.redirect(target, status_code=303)
        res.set_cookie(
            COOKIE_SESSION_NAME,
            session_token,
            max_age=SESSION_MAX_AGE_SECONDS,
            path="/",
            http_only=True,
            same_site="Lax",
            secure=COOKIE_SECURE,
        )
        return res

    @router.post("/auth/quick-login")
    def quick_login_action(req: Request) -> Response:
        """1-Click quick login handler for demo accounts."""
        username = req.form_value("username", "").strip().lower()
        next_url = req.form_value("next", "")
        ip_addr = req.client_address[0] if req.client_address else "127.0.0.1"
        user_agent = req.headers.get("user-agent", "")

        # Lookup demo user
        demo_user = next((d for d in DEMO_USERS if d["username"].lower() == username), None)
        if not demo_user:
            return Response.redirect("/auth/login?error=Unknown+demonstration+account", status_code=303)

        user = authenticate_user(demo_user["username"], demo_user["password"])
        if not user:
            return Response.redirect("/auth/login?error=Demonstration+account+authentication+failed", status_code=303)

        session_token = create_user_session(user["id"], ip_address=ip_addr, user_agent=user_agent)

        record_audit_event(
            action="auth.quick_login",
            object_type="user",
            object_id=user["id"],
            actor=user,
            ip_address=ip_addr,
            details={"demo_username": username, "user_agent": user_agent[:128]},
        )

        target = next_url if is_safe_redirect_url(next_url) else "/"
        res = Response.redirect(target, status_code=303)
        res.set_cookie(
            COOKIE_SESSION_NAME,
            session_token,
            max_age=SESSION_MAX_AGE_SECONDS,
            path="/",
            http_only=True,
            same_site="Lax",
            secure=COOKIE_SECURE,
        )
        return res


    @router.post("/auth/logout")
    def logout_action(req: Request) -> Response:
        """Terminate current session and clear session cookie."""
        ip_addr = req.client_address[0] if req.client_address else "127.0.0.1"
        if req.user:
            record_audit_event(
                action="auth.logout",
                object_type="user",
                object_id=req.user["id"],
                actor=req.user,
                ip_address=ip_addr,
                details={"session_id": req.session_id},
            )

        if req.session_token:
            invalidate_session(req.session_token)

        res = Response.redirect("/auth/login", status_code=303)
        res.delete_cookie(COOKIE_SESSION_NAME, path="/")
        return res

    @router.get("/auth/profile")
    @require_auth
    def profile_view(req: Request) -> Response:
        """Display the authenticated user profile and membership/qualifications."""
        user = req.user
        user_id = user["id"]

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"

        # Fetch active sessions for this user
        current_token_hash = hash_session_token(req.session_token) if req.session_token else ""
        sessions = query_all(
            """
            SELECT id, created_at, expires_at, ip_address, user_agent,
                   (session_token = ?) AS is_current
            FROM sessions
            WHERE user_id = ? AND expires_at > datetime('now')
            ORDER BY created_at DESC;
            """,
            (current_token_hash, user_id),
        )

        # Fetch member qualifications if applicable
        qualifications = []
        if user.get("member_id"):
            qualifications = query_all(
                """
                SELECT q.id, q.qualification_name, q.issue_date, q.expiry_date,
                       mc.name AS category_name, mc.code AS category_code
                FROM qualifications q
                JOIN machine_categories mc ON mc.id = q.category_id
                WHERE q.member_id = ?
                ORDER BY q.issue_date DESC;
                """,
                (user["member_id"],),
            )

        # Build qualifications table
        if qualifications:
            q_rows = []
            for q in qualifications:
                is_expired = False
                now_str = now_rome_iso()[:10]
                if q["expiry_date"] and q["expiry_date"] < now_str:
                    is_expired = True
                    status_badge = '<span class="badge badge-danger">Expired</span>'
                else:
                    status_badge = '<span class="badge badge-success">Valid</span>'

                q_rows.append(
                    f"""
                    <tr>
                        <td><strong>{escape_html(q['qualification_name'])}</strong></td>
                        <td>{escape_html(q['category_name'])} (<code>{escape_html(q['category_code'])}</code>)</td>
                        <td>{escape_html(q['issue_date'])}</td>
                        <td>{escape_html(q['expiry_date'] or 'Permanent')}</td>
                        <td>{status_badge}</td>
                    </tr>
                    """
                )
            qual_html = f"""
            <table class="table" style="width: 100%; margin-top: 0.5rem;">
                <thead>
                    <tr>
                        <th>Qualification</th>
                        <th>Category</th>
                        <th>Issued</th>
                        <th>Expires</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(q_rows)}
                </tbody>
            </table>
            """
        else:
            qual_html = '<p style="color: #64748b; font-style: italic;">No specific machine qualifications registered on this account.</p>'

        # Build sessions table
        sess_rows = []
        for s in sessions:
            current_tag = '<span class="badge badge-primary">Current Session</span>' if s["is_current"] else ""
            sess_rows.append(
                f"""
                <tr>
                    <td>{escape_html(s['created_at'][:19].replace('T', ' '))}</td>
                    <td>{escape_html(s['expires_at'][:19].replace('T', ' '))}</td>
                    <td><code>{escape_html(s['ip_address'] or '127.0.0.1')}</code></td>
                    <td>{current_tag}</td>
                </tr>
                """
            )
        sess_html = f"""
        <table class="table" style="width: 100%; margin-top: 0.5rem;">
            <thead>
                <tr>
                    <th>Created</th>
                    <th>Expires</th>
                    <th>IP Address</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
                {"".join(sess_rows)}
            </tbody>
        </table>
        """

        role = user.get("role", "viewer")
        member_number = user.get("member_number") or "N/A"
        membership_status = user.get("membership_status") or "N/A"
        user_perms = sorted(list(get_user_permissions(user)))

        banner_html = ""
        if flash_msg:
            banner_html = f'<div class="alert alert-{flash_class}" style="margin-bottom: 1.25rem;">{escape_html(flash_msg)}</div>'

        csrf_token = getattr(req, "csrf_token", "")
        csrf_form_field = csrf_input(csrf_token)

        content = f"""
        <div style="max-width: 900px; margin: 0 auto;">
            {banner_html}
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title">User Profile: {escape_html(user.get('full_name', ''))}</h2>
                    <span class="role-badge role-{role}">{role.upper()}</span>
                </div>
                <div class="card-body">
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem;">
                        <div>
                            <h4 style="margin-bottom: 0.5rem; color: #334155;">Account Information</h4>
                            <p><strong>Username:</strong> <code>{escape_html(user.get('username'))}</code></p>
                            <p><strong>Email:</strong> {escape_html(user.get('email'))}</p>
                            <p><strong>Role:</strong> <span class="role-badge role-{role}">{role.upper()}</span></p>
                            <p><strong>Account Status:</strong> <span class="badge badge-success">Active</span></p>
                        </div>
                        <div>
                            <h4 style="margin-bottom: 0.5rem; color: #334155;">Makerspace Membership</h4>
                            <p><strong>Member Number:</strong> <code>{escape_html(member_number)}</code></p>
                            <p><strong>Membership Status:</strong> <span class="badge badge-info">{escape_html(membership_status.capitalize())}</span></p>
                            <p><strong>Timezone:</strong> Europe/Rome</p>
                        </div>
                    </div>

                    <p style="color: #64748b; font-size: 0.85rem; background: #f8fafc; padding: 0.75rem; border-radius: 4px; border: 1px solid #e2e8f0;">
                        <strong>Role Description:</strong> {escape_html(ROLE_DESCRIPTIONS.get(role, ''))}
                    </p>

                    <hr style="border: 0; border-top: 1px solid #e2e8f0; margin: 1.5rem 0;">

                    <h4 style="margin-bottom: 0.75rem; color: #334155;">Machine Qualifications</h4>
                    {qual_html}

                    <hr style="border: 0; border-top: 1px solid #e2e8f0; margin: 1.5rem 0;">

                    <h4 style="margin-bottom: 0.75rem; color: #334155;">Active Browser Sessions</h4>
                    {sess_html}

                    <hr style="border: 0; border-top: 1px solid #e2e8f0; margin: 1.5rem 0;">

                    <h4 style="margin-bottom: 0.75rem; color: #334155;">Change Password</h4>
                    <form action="/auth/profile/password" method="POST" style="max-width: 450px;">
                        {csrf_form_field}
                        <div class="form-group" style="margin-bottom: 0.75rem;">
                            <label class="form-label" for="current_password">Current Password</label>
                            <input type="password" id="current_password" name="current_password" class="form-control" required>
                        </div>
                        <div class="form-group" style="margin-bottom: 0.75rem;">
                            <label class="form-label" for="new_password">New Password (min 6 characters)</label>
                            <input type="password" id="new_password" name="new_password" class="form-control" minlength="6" required>
                        </div>
                        <button type="submit" class="btn btn-primary">Update Password</button>
                    </form>
                </div>
            </div>
        </div>
        """
        return Response.html(render_page("User Profile", content, user=user, active_nav=None, csrf_token=csrf_token))

    @router.post("/auth/profile/password")
    @require_auth
    def update_password_action(req: Request) -> Response:
        """Update authenticated user's password, invalidating all prior active sessions (SEC-PASSWORD-SESSION-REVOCATION)."""
        user = req.user
        current_pwd = req.form_value("current_password", "")
        new_pwd = req.form_value("new_password", "")
        ip_addr = req.client_address[0] if req.client_address else "127.0.0.1"
        user_agent = req.headers.get("user-agent", "")

        # Verify current password
        user_row = query_one("SELECT password_hash, password_salt FROM users WHERE id = ?;", (user["id"],))
        if not user_row or not verify_password(current_pwd, user_row["password_hash"], user_row["password_salt"]):
            return Response.redirect("/auth/profile?error=Current+password+incorrect", status_code=303)

        if len(new_pwd) < 6:
            return Response.redirect("/auth/profile?error=Password+too+short", status_code=303)

        # Invalidate all prior sessions upon credential rotation
        update_user_password(user["id"], new_pwd, invalidate_sessions=True)

        # Create fresh session for current active connection
        new_session_token = create_user_session(user["id"], ip_address=ip_addr, user_agent=user_agent)

        res = Response.redirect("/auth/profile?success=Password+successfully+updated", status_code=303)
        res.set_cookie(
            COOKIE_SESSION_NAME,
            new_session_token,
            max_age=SESSION_MAX_AGE_SECONDS,
            path="/",
            http_only=True,
            same_site="Lax",
            secure=COOKIE_SECURE,
        )
        return res


    @router.get("/auth/users")
    @require_role("admin", "operator")
    def users_list_view(req: Request) -> Response:
        """Administrative view listing all users with role management controls."""
        users = list_users()
        current_actor = req.user
        actor_is_admin = is_admin(current_actor)

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        rows = []
        for u in users:
            role = u.get("role", "viewer")
            role_class = f"role-badge role-{role}"
            member_tag = f"<code>{escape_html(u['member_number'])}</code>" if u.get("member_number") else '<span style="color:#94a3b8;">None</span>'
            status_tag = '<span class="badge badge-success">Active</span>' if u.get("is_active") else '<span class="badge badge-danger">Disabled</span>'

            # Action controls
            action_buttons = []
            if actor_is_admin:
                # Admin can change roles and toggle status
                role_options = "".join(
                    f'<option value="{r}" {"selected" if r == role else ""}>{r.upper()}</option>'
                    for r in ROLES
                )
                role_form = f"""
                <form action="/auth/users/{u['id']}/role" method="POST" style="display: inline-flex; gap: 0.25rem; align-items: center;">
                    {csrf_field}
                    <select name="role" class="form-control form-control-sm" style="width: auto; padding: 0.2rem 0.4rem; font-size: 0.8rem;">
                        {role_options}
                    </select>
                    <button type="submit" class="btn btn-sm btn-outline-primary" style="padding: 0.2rem 0.4rem; font-size: 0.8rem;">Save</button>
                </form>
                """
                toggle_btn_text = "Disable" if u.get("is_active") else "Activate"
                toggle_btn_class = "btn-outline-danger" if u.get("is_active") else "btn-outline-success"
                toggle_form = f"""
                <form action="/auth/users/{u['id']}/toggle-active" method="POST" style="display: inline;">
                    {csrf_field}
                    <button type="submit" class="btn btn-sm {toggle_btn_class}" style="padding: 0.2rem 0.4rem; font-size: 0.8rem;">{toggle_btn_text}</button>
                </form>
                """
                action_buttons.append(role_form)
                action_buttons.append(toggle_form)
            else:
                # Operator view: read-only for admin/operator accounts, status toggle allowed for members
                if role in (ROLE_MEMBER, ROLE_VIEWER):
                    toggle_btn_text = "Disable" if u.get("is_active") else "Activate"
                    toggle_btn_class = "btn-outline-danger" if u.get("is_active") else "btn-outline-success"
                    action_buttons.append(
                        f"""
                        <form action="/auth/users/{u['id']}/toggle-active" method="POST" style="display: inline;">
                            {csrf_field}
                            <button type="submit" class="btn btn-sm {toggle_btn_class}" style="padding: 0.2rem 0.4rem; font-size: 0.8rem;">{toggle_btn_text}</button>
                        </form>
                        """
                    )
                else:
                    action_buttons.append('<span style="color: #94a3b8; font-size: 0.8rem;">Protected</span>')

            rows.append(
                f"""
                <tr>
                    <td>{u['id']}</td>
                    <td><strong>{escape_html(u['username'])}</strong></td>
                    <td>{escape_html(u['full_name'])}</td>
                    <td>{escape_html(u['email'])}</td>
                    <td><span class="{role_class}">{role.upper()}</span></td>
                    <td>{member_tag}</td>
                    <td>{status_tag}</td>
                    <td>{u.get('active_sessions_count', 0)}</td>
                    <td>{escape_html(u['created_at'][:10])}</td>
                    <td style="white-space: nowrap;">{" ".join(action_buttons)}</td>
                </tr>
                """
            )

        operator_notice = ""
        if not actor_is_admin:
            operator_notice = """
            <div class="alert alert-info" style="margin-bottom: 1.25rem;">
                <strong>Operator Access:</strong> You have operational visibility over user accounts. System-wide security settings and role assignments are restricted to Administrators.
            </div>
            """

        banner_html = ""
        if flash_msg:
            banner_html = f'<div class="alert alert-{flash_class}" style="margin-bottom: 1.25rem;">{escape_html(flash_msg)}</div>'

        content = f"""
        {banner_html}
        {operator_notice}
        <div class="card">
            <div class="card-header">
                <h2 class="card-title">User Accounts & Authorization</h2>
            </div>
            <div class="card-body">
                <p style="color: #64748b; margin-bottom: 1.25rem;">
                    Listing all registered system users and their assigned roles in ForgeDesk. Server-side authorization rules are strictly enforced across all operations.
                </p>
                <div style="overflow-x: auto;">
                    <table class="table" style="width: 100%;">
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Username</th>
                                <th>Full Name</th>
                                <th>Email</th>
                                <th>Role</th>
                                <th>Linked Member</th>
                                <th>Status</th>
                                <th>Sessions</th>
                                <th>Created</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {"".join(rows)}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 4-Role Permission Matrix Card -->
        <div class="card" style="margin-top: 1.5rem;">
            <div class="card-header">
                <h3 class="card-title" style="font-size: 1.1rem;">Four-Role Access Control Matrix</h3>
            </div>
            <div class="card-body">
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem;">
                    <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 1rem;">
                        <h4 style="margin: 0 0 0.5rem 0;"><span class="role-badge role-admin">ADMINISTRATOR</span></h4>
                        <p style="font-size: 0.85rem; color: #475569; margin-bottom: 0.5rem;">Full system authority.</p>
                        <ul style="font-size: 0.8rem; color: #334155; padding-left: 1.2rem; margin: 0;">
                            <li>Manage security & user roles</li>
                            <li>Financial charge adjustments</li>
                            <li>System reset, import & restore</li>
                            <li>Full workshop operations</li>
                        </ul>
                    </div>
                    <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 1rem;">
                        <h4 style="margin: 0 0 0.5rem 0;"><span class="role-badge role-operator">OPERATOR</span></h4>
                        <p style="font-size: 0.85rem; color: #475569; margin-bottom: 0.5rem;">Workshop operations manager.</p>
                        <ul style="font-size: 0.8rem; color: #334155; padding-left: 1.2rem; margin: 0;">
                            <li>Manage machines & maintenance</li>
                            <li>Manage all reservations & waitlist</li>
                            <li>Manage inventory movements</li>
                            <li>Cannot change roles/security</li>
                        </ul>
                    </div>
                    <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 1rem;">
                        <h4 style="margin: 0 0 0.5rem 0;"><span class="role-badge role-member">MEMBER</span></h4>
                        <p style="font-size: 0.85rem; color: #475569; margin-bottom: 0.5rem;">Self-service makerspace user.</p>
                        <ul style="font-size: 0.8rem; color: #334155; padding-left: 1.2rem; margin: 0;">
                            <li>Manage own reservations</li>
                            <li>View personal profile & charges</li>
                            <li>Self check-in & checkout</li>
                            <li>Consume materials at checkout</li>
                        </ul>
                    </div>
                    <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 1rem;">
                        <h4 style="margin: 0 0 0.5rem 0;"><span class="role-badge role-viewer">VIEWER</span></h4>
                        <p style="font-size: 0.85rem; color: #475569; margin-bottom: 0.5rem;">Read-only observer.</p>
                        <ul style="font-size: 0.8rem; color: #334155; padding-left: 1.2rem; margin: 0;">
                            <li>View calendar & availability</li>
                            <li>View machine catalog & status</li>
                            <li>View public inventory catalog</li>
                            <li>No write/booking operations</li>
                        </ul>
                    </div>
                </div>
            </div>
        </div>
        """
        return Response.html(render_page("User Management", content, user=req.user, active_nav=None, csrf_token=csrf_token))

    @router.post("/auth/users/{id}/role")
    @require_role("admin")
    def update_role_action(req: Request) -> Response:
        """HTML endpoint to update a user's role (Admin only)."""
        target_id_str = req.route_params.get("id", "")
        try:
            target_id = int(target_id_str)
        except ValueError:
            return Response.redirect("/auth/users?error=Invalid+user+ID", status_code=303)

        new_role = req.form_value("role", "")
        success, message = update_user_role(target_id, new_role, req.user)
        param = "success" if success else "error"
        return Response.redirect(f"/auth/users?{param}={message.replace(' ', '+')}", status_code=303)

    @router.post("/auth/users/{id}/toggle-active")
    @require_role("admin", "operator")
    def toggle_active_action(req: Request) -> Response:
        """HTML endpoint to toggle user account status (Admin or Operator for members)."""
        target_id_str = req.route_params.get("id", "")
        try:
            target_id = int(target_id_str)
        except ValueError:
            return Response.redirect("/auth/users?error=Invalid+user+ID", status_code=303)

        success, message = toggle_user_active_status(target_id, req.user)
        param = "success" if success else "error"
        return Response.redirect(f"/auth/users?{param}={message.replace(' ', '+')}", status_code=303)

    # -------------------------------------------------------------------------
    # JSON API Handlers
    # -------------------------------------------------------------------------

    @router.post("/api/auth/login")
    def api_login(req: Request) -> Response:
        """API Login endpoint returning JWT/Session token in JSON response."""
        data = req.json()
        username = data.get("username", "").strip()
        password = data.get("password", "")
        ip_addr = req.client_address[0] if req.client_address else "127.0.0.1"
        user_agent = req.headers.get("user-agent", "")

        user = authenticate_user(username, password)
        if not user:
            record_audit_event(
                action="auth.login_failed",
                object_type="user",
                object_id=username or "unknown",
                actor_name="anonymous",
                ip_address=ip_addr,
                details={"attempted_identifier": username, "method": "json_api", "reason": "invalid_credentials"},
            )
            return Response.json(
                {"error": "Unauthorized", "message": "Invalid username or password."},
                status_code=401,
            )

        token = create_user_session(user["id"], ip_address=ip_addr, user_agent=user_agent)

        record_audit_event(
            action="auth.login_success",
            object_type="user",
            object_id=user["id"],
            actor=user,
            ip_address=ip_addr,
            details={"method": "json_api", "user_agent": user_agent[:128]},
        )

        res = Response.json({
            "success": True,
            "token": token,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
                "full_name": user["full_name"],
                "role": user["role"],
                "member_id": user.get("member_id"),
                "member_number": user.get("member_number"),
                "permissions": sorted(list(get_user_permissions(user))),
            },
        })
        res.set_cookie(
            COOKIE_SESSION_NAME,
            token,
            max_age=SESSION_MAX_AGE_SECONDS,
            path="/",
            http_only=True,
            same_site="Lax",
            secure=COOKIE_SECURE,
        )
        return res


    @router.post("/api/auth/logout")
    def api_logout(req: Request) -> Response:
        """API Logout endpoint invalidating session."""
        ip_addr = req.client_address[0] if req.client_address else "127.0.0.1"
        if req.user:
            record_audit_event(
                action="auth.logout",
                object_type="user",
                object_id=req.user["id"],
                actor=req.user,
                ip_address=ip_addr,
                details={"session_id": req.session_id, "method": "json_api"},
            )

        if req.session_token:
            invalidate_session(req.session_token)

        res = Response.json({"success": True, "message": "Logged out successfully."})
        res.delete_cookie(COOKIE_SESSION_NAME, path="/")
        return res

    @router.get("/api/auth/me")
    def api_me(req: Request) -> Response:
        """API endpoint returning currently authenticated user and effective permissions."""
        if not req.user:
            return Response.json({"authenticated": False, "user": None}, status_code=401)

        user = req.user
        return Response.json({
            "authenticated": True,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
                "full_name": user["full_name"],
                "role": user["role"],
                "member_id": user.get("member_id"),
                "member_number": user.get("member_number"),
                "session_id": req.session_id,
                "permissions": sorted(list(get_user_permissions(user))),
            },
        })

    @router.get("/api/auth/users")
    @require_role("admin", "operator")
    def api_users_list(req: Request) -> Response:
        """API endpoint listing all users (Admin and Operator)."""
        users = list_users()
        cleaned_users = []
        for u in users:
            cleaned_users.append({
                "id": u["id"],
                "username": u["username"],
                "email": u["email"],
                "full_name": u["full_name"],
                "role": u["role"],
                "is_active": bool(u["is_active"]),
                "member_id": u.get("member_id"),
                "member_number": u.get("member_number"),
                "active_sessions_count": u.get("active_sessions_count", 0),
                "created_at": u["created_at"],
            })
        return Response.json({"users": cleaned_users})

    @router.post("/api/auth/users/{id}/role")
    @require_role("admin")
    def api_update_user_role(req: Request) -> Response:
        """API endpoint to update user role (Admin only). Server-side authorization strictly enforced."""
        target_id_str = req.route_params.get("id", "")
        try:
            target_id = int(target_id_str)
        except ValueError:
            return Response.json({"error": "Bad Request", "message": "Invalid user ID."}, status_code=400)

        data = req.json()
        new_role = data.get("role", "")

        success, message = update_user_role(target_id, new_role, req.user)
        if not success:
            return Response.json({"error": "Forbidden", "message": message}, status_code=403)

        return Response.json({"success": True, "message": message, "user_id": target_id, "new_role": new_role})

    @router.post("/api/auth/users/{id}/toggle-active")
    @require_role("admin", "operator")
    def api_toggle_user_active(req: Request) -> Response:
        """API endpoint to toggle user active status."""
        target_id_str = req.route_params.get("id", "")
        try:
            target_id = int(target_id_str)
        except ValueError:
            return Response.json({"error": "Bad Request", "message": "Invalid user ID."}, status_code=400)

        success, message = toggle_user_active_status(target_id, req.user)
        if not success:
            return Response.json({"error": "Forbidden", "message": message}, status_code=403)

        return Response.json({"success": True, "message": message, "user_id": target_id})

    @router.get("/api/auth/permissions")
    def api_permissions(req: Request) -> Response:
        """API endpoint returning role permissions matrix and active user permissions."""
        role_map = {r: sorted(list(perms)) for r, perms in ROLE_PERMISSIONS.items()}
        current_perms = sorted(list(get_user_permissions(req.user))) if req.user else []
        return Response.json({
            "roles": list(ROLES),
            "role_descriptions": ROLE_DESCRIPTIONS,
            "role_permissions": role_map,
            "current_user_role": get_user_role(req.user),
            "current_user_permissions": current_perms,
        })

    @router.post("/api/auth/check-permission")
    def api_check_permission(req: Request) -> Response:
        """API endpoint to test server-side permission check for a given permission name."""
        data = req.json()
        permission = data.get("permission", "")

        if not permission:
            return Response.json({"error": "Bad Request", "message": "Permission parameter required."}, status_code=400)

        allowed = has_permission(req.user, permission)
        return Response.json({
            "permission": permission,
            "allowed": allowed,
            "user_role": get_user_role(req.user),
        })

    @router.get("/api/auth/demo-accounts")
    def api_demo_accounts(req: Request) -> Response:
        """API endpoint listing available demo accounts and credentials."""
        accounts = []
        for d in DEMO_USERS:
            accounts.append({
                "username": d["username"],
                "role": d["role"],
                "full_name": d["full_name"],
                "password": d["password"],
                "email": d["email"],
                "permissions": sorted(list(ROLE_PERMISSIONS.get(d["role"], set()))),
            })
        return Response.json({"demo_accounts": accounts})
