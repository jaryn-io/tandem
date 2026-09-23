"""Authentication, user session, and role management services for ForgeDesk."""

import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

from forgedesk.auth.permissions import (
    PERM_SECURITY_MANAGE,
    PERM_USERS_MANAGE_ROLES,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLES,
    get_user_permissions,
    get_user_role,
    has_permission,
)
from forgedesk.db.connection import get_connection, query_all, query_one, transaction
from forgedesk.utils.crypto import (
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)
from forgedesk.utils.datetime_tz import now_rome, now_rome_iso, parse_datetime

logger = logging.getLogger("forgedesk.auth.service")

# Default session duration in hours
DEFAULT_SESSION_HOURS = 24


def authenticate_user(username_or_email: str, password: str) -> Optional[Dict[str, Any]]:
    """Authenticate a user by username or email with constant-time password verification."""
    if not username_or_email or not password:
        return None

    clean_identifier = username_or_email.strip().lower()

    user_row = query_one(
        """
        SELECT u.id, u.username, u.email, u.password_hash, u.password_salt,
               u.full_name, u.role, u.is_active, u.created_at, u.updated_at,
               m.id AS member_id, m.member_number, m.membership_status, m.membership_expiry
        FROM users u
        LEFT JOIN members m ON m.user_id = u.id
        WHERE (LOWER(u.username) = ? OR LOWER(u.email) = ?);
        """,
        (clean_identifier, clean_identifier),
    )

    if not user_row:
        logger.debug("Authentication failed: User '%s' not found.", clean_identifier)
        return None

    if not user_row["is_active"]:
        logger.warning("Authentication failed: Account '%s' is inactive.", clean_identifier)
        return None

    if not verify_password(password, user_row["password_hash"], user_row["password_salt"]):
        logger.warning("Authentication failed: Password mismatch for user '%s'.", clean_identifier)
        return None

    user = dict(user_row)
    user.pop("password_hash", None)
    user.pop("password_salt", None)
    return user


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve user and linked member details by user ID."""
    user_row = query_one(
        """
        SELECT u.id, u.username, u.email, u.full_name, u.role, u.is_active,
               u.created_at, u.updated_at,
               m.id AS member_id, m.member_number, m.membership_status, m.membership_expiry
        FROM users u
        LEFT JOIN members m ON m.user_id = u.id
        WHERE u.id = ?;
        """,
        (user_id,),
    )
    return dict(user_row) if user_row else None


def create_user(
    username: str,
    email: str,
    password: str,
    full_name: str,
    role: str = "member",
    is_active: int = 1,
) -> int:
    """Create a new user account with secure PBKDF2 password hashing."""
    role_clean = role.strip().lower()
    if role_clean not in ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of: {', '.join(ROLES)}")

    username_clean = username.strip().lower()
    email_clean = email.strip().lower()
    if not username_clean or not email_clean or not password:
        raise ValueError("Username, email, and password are required.")

    pwd_hash, pwd_salt = hash_password(password)
    now_iso = now_rome_iso()

    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO users (username, email, password_hash, password_salt, full_name, role, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (username_clean, email_clean, pwd_hash, pwd_salt, full_name.strip(), role_clean, is_active, now_iso, now_iso),
        )
        uid = cursor.lastrowid
        from forgedesk.audit.service import record_audit_event
        record_audit_event(
            action="user.created",
            object_type="user",
            object_id=uid,
            actor_id=uid,
            actor_name=full_name.strip(),
            after={
                "username": username_clean,
                "email": email_clean,
                "full_name": full_name.strip(),
                "role": role_clean,
                "is_active": is_active,
            },
            conn=conn,
        )
        return uid


def update_user_password(user_id: int, new_password: str, invalidate_sessions: bool = True) -> bool:
    """Update user password with new salt and PBKDF2 hash, optionally revoking active sessions."""
    if not new_password or len(new_password) < 6:
        raise ValueError("Password must be at least 6 characters long.")

    pwd_hash, pwd_salt = hash_password(new_password)
    now_iso = now_rome_iso()

    with transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE users
            SET password_hash = ?, password_salt = ?, updated_at = ?
            WHERE id = ?;
            """,
            (pwd_hash, pwd_salt, now_iso, user_id),
        )
        if cursor.rowcount == 0:
            return False

        if invalidate_sessions:
            conn.execute("DELETE FROM sessions WHERE user_id = ?;", (user_id,))

        from forgedesk.audit.service import record_audit_event
        record_audit_event(
            action="user.password_updated",
            object_type="user",
            object_id=user_id,
            actor_id=user_id,
            details={"sessions_invalidated": invalidate_sessions},
            conn=conn,
        )

    return True


def count_active_admins() -> int:
    """Count how many active administrators exist in the database."""
    row = query_one("SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND is_active = 1;")
    return row["c"] if row else 0


def update_user_role(user_id: int, new_role: str, actor_user: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Assign a new role to a user, enforcing that only Administrators can modify user roles and security settings."""
    if not actor_user:
        return False, "Authentication required to modify user roles."

    # Enforce server-side security rule: Operators CANNOT modify roles or system security settings
    if not has_permission(actor_user, PERM_USERS_MANAGE_ROLES):
        actor_role = get_user_role(actor_user) or "unknown"
        logger.warning(
            "Access denied: User %s (role: %s) attempted to change role for user_id %d.",
            actor_user.get("username"),
            actor_role,
            user_id,
        )
        return False, f"Forbidden: Operators and non-administrators cannot modify user roles or security settings."

    new_role_clean = new_role.strip().lower()
    if new_role_clean not in ROLES:
        return False, f"Invalid role '{new_role}'. Allowed roles: {', '.join(ROLES)}."

    target_user = get_user_by_id(user_id)
    if not target_user:
        return False, f"Target user with ID {user_id} does not exist."

    current_target_role = target_user.get("role", "").lower()
    if current_target_role == new_role_clean:
        return True, f"User already has role '{new_role_clean}'."

    # Prevent demoting the last active administrator
    if current_target_role == ROLE_ADMIN and new_role_clean != ROLE_ADMIN:
        active_admins = count_active_admins()
        if active_admins <= 1:
            return False, "Cannot demote the last remaining active Administrator. Assign another Administrator first."

    now_iso = now_rome_iso()
    with transaction() as conn:
        conn.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE id = ?;",
            (new_role_clean, now_iso, user_id),
        )
        # Invalidate active sessions for target user to enforce immediate role update
        conn.execute("DELETE FROM sessions WHERE user_id = ?;", (user_id,))

        from forgedesk.audit.service import record_audit_event
        record_audit_event(
            action="user.role_changed",
            object_type="user",
            object_id=user_id,
            actor=actor_user,
            before={"role": current_target_role},
            after={"role": new_role_clean},
            details={"target_username": target_user.get("username")},
            conn=conn,
        )

    logger.info(
        "User role updated: user_id %d changed from %s to %s by admin %s (user_id %s).",
        user_id,
        current_target_role,
        new_role_clean,
        actor_user.get("username"),
        actor_user.get("id"),
    )
    return True, f"User role successfully changed to '{new_role_clean}'."


def toggle_user_active_status(user_id: int, actor_user: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Toggle active/disabled status for a user.
    
    Admins can toggle any user (except the last admin).
    Operators can toggle Member and Viewer accounts only.
    """
    if not actor_user:
        return False, "Authentication required."

    target_user = get_user_by_id(user_id)
    if not target_user:
        return False, f"Target user with ID {user_id} does not exist."

    actor_role = get_user_role(actor_user)
    target_role = get_user_role(target_user)

    # Server-side authorization check
    if actor_role == ROLE_OPERATOR:
        if target_role in (ROLE_ADMIN, ROLE_OPERATOR):
            return False, "Operators cannot disable Administrator or Operator accounts."
    elif actor_role != ROLE_ADMIN:
        return False, "Permission denied: Only Administrators and Operators can manage account activation."

    new_active = 0 if target_user["is_active"] else 1

    # Protect last active administrator from deactivation
    if target_role == ROLE_ADMIN and new_active == 0:
        active_admins = count_active_admins()
        if active_admins <= 1:
            return False, "Cannot disable the last active Administrator account."

    now_iso = now_rome_iso()
    status_str = "activated" if new_active == 1 else "disabled"

    with transaction() as conn:
        conn.execute("UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?;", (new_active, now_iso, user_id))
        if new_active == 0:
            conn.execute("DELETE FROM sessions WHERE user_id = ?;", (user_id,))

        from forgedesk.audit.service import record_audit_event
        record_audit_event(
            action="user.status_toggled",
            object_type="user",
            object_id=user_id,
            actor=actor_user,
            before={"is_active": target_user["is_active"]},
            after={"is_active": new_active},
            details={"target_username": target_user.get("username"), "new_status": status_str},
            conn=conn,
        )

    logger.info("User account ID %d %s by %s.", user_id, status_str, actor_user.get("username"))
    return True, f"User account successfully {status_str}."


def create_user_session(
    user_id: int,
    ip_address: str = "",
    user_agent: str = "",
    duration_hours: int = DEFAULT_SESSION_HOURS,
) -> str:
    """Generate and store a new secure session token for a user with SHA-256 hash at rest."""
    raw_token = generate_session_token()
    token_hash = hash_session_token(raw_token)
    now_dt = now_rome()
    created_at = now_dt.isoformat()
    expires_at = (now_dt + datetime.timedelta(hours=duration_hours)).isoformat()

    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_token, user_id, created_at, expires_at, ip_address, user_agent)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (token_hash, user_id, created_at, expires_at, ip_address[:128], user_agent[:255]),
        )

    logger.debug("Session created for user_id %d with expiry %s", user_id, expires_at)
    return raw_token


def get_user_by_session(session_token: str) -> Optional[Dict[str, Any]]:
    """Retrieve active user and session info from session token, validating expiration."""
    if not session_token:
        return None

    token_hash = hash_session_token(session_token)
    now_iso = now_rome_iso()

    session_row = query_one(
        """
        SELECT s.id AS session_id, s.session_token AS session_token_hash, s.created_at AS session_created_at,
               s.expires_at AS session_expires_at, s.ip_address, s.user_agent,
               u.id, u.username, u.email, u.full_name, u.role, u.is_active,
               m.id AS member_id, m.member_number, m.membership_status, m.membership_expiry
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        LEFT JOIN members m ON m.user_id = u.id
        WHERE s.session_token = ?
          AND s.expires_at > ?
          AND u.is_active = 1;
        """,
        (token_hash, now_iso),
    )

    if not session_row:
        return None

    user_dict = dict(session_row)
    user_dict["session_token"] = session_token
    return user_dict


def invalidate_session(session_token: str) -> bool:
    """Invalidate (delete) a specific session token."""
    if not session_token:
        return False

    token_hash = hash_session_token(session_token)
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE session_token = ?;", (token_hash,))
        return cursor.rowcount > 0



def invalidate_user_sessions(user_id: int) -> int:
    """Invalidate all active sessions for a user."""
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE user_id = ?;", (user_id,))
        return cursor.rowcount


def purge_expired_sessions() -> int:
    """Delete all expired sessions from the database."""
    now_iso = now_rome_iso()
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE expires_at <= ?;", (now_iso,))
        count = cursor.rowcount
        if count > 0:
            logger.info("Purged %d expired sessions.", count)
        return count


def list_users() -> List[Dict[str, Any]]:
    """List all registered users with member association and active session count."""
    rows = query_all(
        """
        SELECT u.id, u.username, u.email, u.full_name, u.role, u.is_active,
               u.created_at, u.updated_at,
               m.id AS member_id, m.member_number, m.membership_status,
               (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id AND s.expires_at > datetime('now')) AS active_sessions_count
        FROM users u
        LEFT JOIN members m ON m.user_id = u.id
        ORDER BY u.id ASC;
        """
    )
    return [dict(r) for r in rows]
