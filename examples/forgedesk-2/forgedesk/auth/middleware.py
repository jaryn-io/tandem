import functools
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Union

from forgedesk.auth.permissions import (
    ALL_PERMISSIONS,
    ROLE_ADMIN,
    ROLES,
    get_user_permissions,
    get_user_role,
    has_all_permissions,
    has_any_permission,
    has_permission,
)
from forgedesk.auth.service import get_user_by_session
from forgedesk.config import COOKIE_SECURE, SECRET_KEY, SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS
from forgedesk.core.http import Request, Response
from forgedesk.core.templates import escape_html, render_page
from forgedesk.utils.crypto import generate_csrf_token, generate_token, verify_csrf_token

logger = logging.getLogger("forgedesk.auth.middleware")

COOKIE_SESSION_NAME = SESSION_COOKIE_NAME
COOKIE_CSRF_NAME = "fd_csrf"


def auth_middleware(req: Request, next_handler: Callable[[Request], Response]) -> Response:
    """Extract session token from cookie or Authorization header and load authenticated user."""
    session_token: Optional[str] = None
    auth_source = "none"
    set_anon_csrf = False
    anon_token: Optional[str] = None

    # 1. Check Cookie
    cookie_token = req.get_cookie(COOKIE_SESSION_NAME)
    if cookie_token:
        session_token = cookie_token
        auth_source = "cookie"

    # 2. Check Authorization Header (e.g. Bearer <token>) for API calls
    if not session_token:
        auth_header = req.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            session_token = auth_header[7:].strip()
            auth_source = "bearer"

    if session_token:
        user_context = get_user_by_session(session_token)
        if user_context:
            req.user = user_context
            req.session_id = user_context.get("session_id")
            req.session_token = session_token
            req.csrf_token = generate_csrf_token(session_token, SECRET_KEY)
            req.auth_source = auth_source
            req.anon_csrf_token = None
        else:
            req.user = None
            req.session_id = None
            req.session_token = None
            req.auth_source = "none"
            anon_token = req.get_cookie(COOKIE_CSRF_NAME)
            if not anon_token:
                anon_token = generate_token(32)
                set_anon_csrf = True
            req.anon_csrf_token = anon_token
            req.csrf_token = generate_csrf_token(anon_token, SECRET_KEY)
    else:
        req.user = None
        req.session_id = None
        req.session_token = None
        req.auth_source = "none"
        anon_token = req.get_cookie(COOKIE_CSRF_NAME)
        if not anon_token:
            anon_token = generate_token(32)
            set_anon_csrf = True
        req.anon_csrf_token = anon_token
        req.csrf_token = generate_csrf_token(anon_token, SECRET_KEY)

    response = next_handler(req)

    # Attach anonymous CSRF cookie to establish pre-session anti-CSRF protection (SEC-LOGIN-CSRF-QUICK-LOGIN)
    if set_anon_csrf and anon_token and not req.user:
        response.set_cookie(
            COOKIE_CSRF_NAME,
            anon_token,
            max_age=SESSION_MAX_AGE_SECONDS,
            path="/",
            http_only=True,
            same_site="Lax",
            secure=COOKIE_SECURE,
        )

    return response


def csrf_middleware(req: Request, next_handler: Callable[[Request], Response]) -> Response:
    """Validate CSRF token on state-changing requests (authenticated sessions and login flows)."""
    if req.method in ("POST", "PUT", "DELETE", "PATCH"):
        # 1. Enforce CSRF protection when authenticated via browser cookie
        if getattr(req, "auth_source", "") == "cookie" and req.user and req.session_token:
            submitted_token = (
                req.headers.get("x-csrf-token")
                or req.headers.get("x-csrftoken")
                or req.form_value("csrf_token")
            )
            if not submitted_token and req.headers.get("content-type", "").startswith("application/json"):
                try:
                    json_data = req.json()
                    if isinstance(json_data, dict):
                        submitted_token = json_data.get("csrf_token")
                except Exception:
                    pass

            if not submitted_token or not verify_csrf_token(req.session_token, submitted_token, SECRET_KEY):
                logger.warning(
                    "CSRF validation failed for user '%s' on %s %s from IP %s",
                    req.user.get("username"),
                    req.method,
                    req.path,
                    req.client_address[0] if req.client_address else "unknown",
                )
                if is_api_request(req):
                    return Response.json(
                        {
                            "error": "Forbidden",
                            "message": "CSRF verification failed. Missing, invalid, or cross-session CSRF token.",
                            "status_code": 403,
                        },
                        status_code=403,
                    )
                return Response.html(
                    "<h1>403 Forbidden</h1><p>CSRF verification failed. Please refresh the page and try again.</p>",
                    status_code=403,
                )

        # 2. Enforce pre-login CSRF protection on HTML form login and quick-login (SEC-LOGIN-CSRF-QUICK-LOGIN)
        elif req.path in ("/auth/login", "/auth/quick-login"):
            submitted_token = (
                req.headers.get("x-csrf-token")
                or req.headers.get("x-csrftoken")
                or req.form_value("csrf_token")
            )
            seed = getattr(req, "anon_csrf_token", "") or req.get_cookie(COOKIE_CSRF_NAME) or getattr(req, "session_token", "")
            if not submitted_token or not seed or not verify_csrf_token(seed, submitted_token, SECRET_KEY):
                logger.warning(
                    "Pre-login CSRF validation failed on %s %s from IP %s",
                    req.method,
                    req.path,
                    req.client_address[0] if req.client_address else "unknown",
                )
                return Response.html(
                    "<h1>403 Forbidden</h1><p>CSRF verification failed. Cross-site login and session confusion requests are rejected.</p>",
                    status_code=403,
                )

    return next_handler(req)



def is_api_request(req: Request) -> bool:
    """Determine if a request expects a JSON API response."""
    return (
        req.path.startswith("/api/")
        or "application/json" in req.headers.get("accept", "")
        or req.headers.get("content-type", "").startswith("application/json")
    )


def _render_forbidden_html(req: Request, message: str, required: str) -> Response:
    """Render a clean, responsive HTML 403 Forbidden page."""
    user = req.user or {}
    user_role = (user.get("role") or "unauthenticated").upper()
    username = user.get("username") or "Unknown"

    content = f"""
    <div class="card" style="max-width: 650px; margin: 3rem auto; text-align: center; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
        <div class="card-header" style="background-color: #fef2f2; color: #dc2626; border-bottom: 1px solid #fee2e2;">
            <h2 style="font-size: 1.5rem; margin: 0;">🛑 403 - Access Forbidden</h2>
        </div>
        <div class="card-body" style="padding: 2rem;">
            <p style="font-size: 1.05rem; color: #1e293b; margin-bottom: 1rem;">
                {escape_html(message)}
            </p>
            <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 1rem; margin: 1.5rem 0; text-align: left;">
                <p style="margin: 0 0 0.5rem 0;"><strong>Active User:</strong> <code>{escape_html(username)}</code></p>
                <p style="margin: 0 0 0.5rem 0;"><strong>Current Role:</strong> <span class="role-badge role-{escape_html(user_role.lower())}">{escape_html(user_role)}</span></p>
                <p style="margin: 0;"><strong>Required Authorization:</strong> <code style="color: #b91c1c;">{escape_html(required)}</code></p>
            </div>
            <div style="display: flex; justify-content: center; gap: 1rem; margin-top: 1.5rem;">
                <a href="/" class="btn btn-primary">Return to Dashboard</a>
                <a href="/auth/profile" class="btn btn-secondary">View Profile</a>
                <a href="/auth/login" class="btn btn-outline-danger">Switch Account</a>
            </div>
        </div>
    </div>
    """
    html_page = render_page("Access Forbidden", content, user=req.user, active_nav=None)
    return Response.html(html_page, status_code=403)


def require_auth(handler: Callable[[Request], Response]) -> Callable[[Request], Response]:
    """Decorator requiring an authenticated user. Redirects to login for HTML or returns 401 JSON for API."""
    @functools.wraps(handler)
    def wrapper(req: Request) -> Response:
        if not req.user:
            if is_api_request(req):
                return Response.json(
                    {
                        "error": "Unauthorized",
                        "message": "Authentication is required to access this endpoint.",
                        "status_code": 401,
                    },
                    status_code=401,
                )
            redirect_url = f"/auth/login?next={req.path}" if req.path != "/" else "/auth/login"
            return Response.redirect(redirect_url)
        return handler(req)
    return wrapper


def require_role(*allowed_roles: str) -> Callable[[Callable[[Request], Response]], Callable[[Request], Response]]:
    """Decorator requiring one of the specified roles (e.g. 'admin', 'operator')."""
    roles_set = {r.strip().lower() for r in allowed_roles}

    def decorator(handler: Callable[[Request], Response]) -> Callable[[Request], Response]:
        @functools.wraps(handler)
        def wrapper(req: Request) -> Response:
            if not req.user:
                if is_api_request(req):
                    return Response.json(
                        {
                            "error": "Unauthorized",
                            "message": "Authentication is required.",
                            "status_code": 401,
                        },
                        status_code=401,
                    )
                return Response.redirect(f"/auth/login?next={req.path}")

            user_role = get_user_role(req.user)
            if not user_role or user_role not in roles_set:
                required_str = ", ".join(sorted(roles_set)).upper()
                msg = f"Your role ({user_role or 'none'}) does not have permission to access this operation."
                if is_api_request(req):
                    return Response.json(
                        {
                            "error": "Forbidden",
                            "message": msg,
                            "required_roles": list(roles_set),
                            "current_role": user_role,
                            "status_code": 403,
                        },
                        status_code=403,
                    )
                return _render_forbidden_html(req, msg, f"Role: {required_str}")

            return handler(req)
        return wrapper
    return decorator


def require_permission(*permissions: str) -> Callable[[Callable[[Request], Response]], Callable[[Request], Response]]:
    """Decorator requiring ALL specified permissions."""
    req_perms = list(permissions)

    def decorator(handler: Callable[[Request], Response]) -> Callable[[Request], Response]:
        @functools.wraps(handler)
        def wrapper(req: Request) -> Response:
            if not req.user:
                if is_api_request(req):
                    return Response.json(
                        {
                            "error": "Unauthorized",
                            "message": "Authentication is required.",
                            "status_code": 401,
                        },
                        status_code=401,
                    )
                return Response.redirect(f"/auth/login?next={req.path}")

            if not has_all_permissions(req.user, *req_perms):
                user_role = get_user_role(req.user)
                required_str = ", ".join(req_perms)
                msg = f"Permission denied for operation. Required permissions: {required_str}."
                if is_api_request(req):
                    return Response.json(
                        {
                            "error": "Forbidden",
                            "message": msg,
                            "required_permissions": req_perms,
                            "current_role": user_role,
                            "status_code": 403,
                        },
                        status_code=403,
                    )
                return _render_forbidden_html(req, msg, required_str)

            return handler(req)
        return wrapper
    return decorator


def require_any_permission(*permissions: str) -> Callable[[Callable[[Request], Response]], Callable[[Request], Response]]:
    """Decorator requiring AT LEAST ONE of the specified permissions."""
    req_perms = list(permissions)

    def decorator(handler: Callable[[Request], Response]) -> Callable[[Request], Response]:
        @functools.wraps(handler)
        def wrapper(req: Request) -> Response:
            if not req.user:
                if is_api_request(req):
                    return Response.json(
                        {
                            "error": "Unauthorized",
                            "message": "Authentication is required.",
                            "status_code": 401,
                        },
                        status_code=401,
                    )
                return Response.redirect(f"/auth/login?next={req.path}")

            if not has_any_permission(req.user, *req_perms):
                user_role = get_user_role(req.user)
                required_str = " OR ".join(req_perms)
                msg = f"Permission denied. Must have at least one of: {required_str}."
                if is_api_request(req):
                    return Response.json(
                        {
                            "error": "Forbidden",
                            "message": msg,
                            "required_permissions": req_perms,
                            "current_role": user_role,
                            "status_code": 403,
                        },
                        status_code=403,
                    )
                return _render_forbidden_html(req, msg, required_str)

            return handler(req)
        return wrapper
    return decorator
