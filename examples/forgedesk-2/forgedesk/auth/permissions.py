"""Role-Based Access Control (RBAC) and Granular Permissions for ForgeDesk.

Enforces the four-role authorization model:
- Administrator: full control over system, settings, users, and operations.
- Operator: manages daily operations (machines, reservations, members, maintenance,
  inventory) but cannot change system-wide security settings or user roles.
- Member: manages only their own reservations, profile, and usage.
- Viewer: read-only access to permitted operational information.
"""

from typing import Any, Dict, List, Optional, Set, Tuple

# -----------------------------------------------------------------------------
# Role Definitions
# -----------------------------------------------------------------------------
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_MEMBER = "member"
ROLE_VIEWER = "viewer"

ROLES: Tuple[str, ...] = (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_MEMBER,
    ROLE_VIEWER,
)

ROLE_DESCRIPTIONS: Dict[str, str] = {
    ROLE_ADMIN: "Full system control, security settings, user roles, billing adjustments, and database management.",
    ROLE_OPERATOR: "Daily workshop operations: machines, maintenance, reservations, inventory, and member qualifications. Cannot alter security settings or user roles.",
    ROLE_MEMBER: "Self-service reservation management, personal profile, machine usage, and own charges.",
    ROLE_VIEWER: "Read-only access to workshop schedule, public machine availability, and inventory catalog.",
}

# -----------------------------------------------------------------------------
# Permission Definitions
# -----------------------------------------------------------------------------
# System & Security Settings (Admin only)
PERM_SECURITY_MANAGE = "security:manage"
PERM_USERS_MANAGE_ROLES = "users:manage_roles"
PERM_SYSTEM_RESET = "system:reset"
PERM_SYSTEM_RESTORE = "system:restore"
PERM_SYSTEM_IMPORT = "system:import"

# User Management
PERM_USERS_VIEW = "users:view"
PERM_USERS_TOGGLE_ACTIVE = "users:toggle_active"

# Member Management
PERM_MEMBERS_VIEW_ALL = "members:view_all"
PERM_MEMBERS_VIEW_OWN = "members:view_own"
PERM_MEMBERS_MANAGE = "members:manage"

# Machines & Categories
PERM_MACHINES_VIEW = "machines:view"
PERM_MACHINES_MANAGE = "machines:manage"

# Reservations & Waiting List
PERM_RESERVATIONS_VIEW_ALL = "reservations:view_all"
PERM_RESERVATIONS_VIEW_OWN = "reservations:view_own"
PERM_RESERVATIONS_CREATE = "reservations:create"
PERM_RESERVATIONS_MANAGE_ALL = "reservations:manage_all"
PERM_RESERVATIONS_MANAGE_OWN = "reservations:manage_own"
PERM_RESERVATIONS_CHECKIN_ALL = "reservations:checkin_all"
PERM_RESERVATIONS_CHECKIN_OWN = "reservations:checkin_own"

# Inventory Ledger & Consumables
PERM_INVENTORY_VIEW = "inventory:view"
PERM_INVENTORY_MANAGE = "inventory:manage"
PERM_INVENTORY_CONSUME = "inventory:consume"

# Usage Charges & Financial Adjustments
PERM_CHARGES_VIEW_ALL = "charges:view_all"
PERM_CHARGES_VIEW_OWN = "charges:view_own"
PERM_CHARGES_MANAGE = "charges:manage"
PERM_CHARGES_ADJUST = "charges:adjust"

# Maintenance & Incident Tracking
PERM_MAINTENANCE_VIEW = "maintenance:view"
PERM_MAINTENANCE_MANAGE = "maintenance:manage"

# Audit Logging & System Inspection
PERM_AUDIT_VIEW = "audit:view"
PERM_SYSTEM_EXPORT = "system:export"
PERM_SYSTEM_BACKUP = "system:backup"

# All defined permissions
ALL_PERMISSIONS: Set[str] = {
    PERM_SECURITY_MANAGE,
    PERM_USERS_MANAGE_ROLES,
    PERM_SYSTEM_RESET,
    PERM_SYSTEM_RESTORE,
    PERM_SYSTEM_IMPORT,
    PERM_USERS_VIEW,
    PERM_USERS_TOGGLE_ACTIVE,
    PERM_MEMBERS_VIEW_ALL,
    PERM_MEMBERS_VIEW_OWN,
    PERM_MEMBERS_MANAGE,
    PERM_MACHINES_VIEW,
    PERM_MACHINES_MANAGE,
    PERM_RESERVATIONS_VIEW_ALL,
    PERM_RESERVATIONS_VIEW_OWN,
    PERM_RESERVATIONS_CREATE,
    PERM_RESERVATIONS_MANAGE_ALL,
    PERM_RESERVATIONS_MANAGE_OWN,
    PERM_RESERVATIONS_CHECKIN_ALL,
    PERM_RESERVATIONS_CHECKIN_OWN,
    PERM_INVENTORY_VIEW,
    PERM_INVENTORY_MANAGE,
    PERM_INVENTORY_CONSUME,
    PERM_CHARGES_VIEW_ALL,
    PERM_CHARGES_VIEW_OWN,
    PERM_CHARGES_MANAGE,
    PERM_CHARGES_ADJUST,
    PERM_MAINTENANCE_VIEW,
    PERM_MAINTENANCE_MANAGE,
    PERM_AUDIT_VIEW,
    PERM_SYSTEM_EXPORT,
    PERM_SYSTEM_BACKUP,
}

# -----------------------------------------------------------------------------
# Role Permissions Mapping
# -----------------------------------------------------------------------------
ROLE_PERMISSIONS: Dict[str, Set[str]] = {
    ROLE_ADMIN: set(ALL_PERMISSIONS),  # Full control
    ROLE_OPERATOR: {
        # Users & Members
        PERM_USERS_VIEW,
        PERM_USERS_TOGGLE_ACTIVE,
        PERM_MEMBERS_VIEW_ALL,
        PERM_MEMBERS_VIEW_OWN,
        PERM_MEMBERS_MANAGE,
        # Machines
        PERM_MACHINES_VIEW,
        PERM_MACHINES_MANAGE,
        # Reservations
        PERM_RESERVATIONS_VIEW_ALL,
        PERM_RESERVATIONS_VIEW_OWN,
        PERM_RESERVATIONS_CREATE,
        PERM_RESERVATIONS_MANAGE_ALL,
        PERM_RESERVATIONS_CHECKIN_ALL,
        # Inventory
        PERM_INVENTORY_VIEW,
        PERM_INVENTORY_MANAGE,
        PERM_INVENTORY_CONSUME,
        # Charges (viewing only; adjustment reserved for Admin)
        PERM_CHARGES_VIEW_ALL,
        PERM_CHARGES_VIEW_OWN,
        # Maintenance
        PERM_MAINTENANCE_VIEW,
        PERM_MAINTENANCE_MANAGE,
        # Audit & Export
        PERM_AUDIT_VIEW,
        PERM_SYSTEM_EXPORT,
        PERM_SYSTEM_BACKUP,
    },
    ROLE_MEMBER: {
        PERM_MEMBERS_VIEW_OWN,
        PERM_MACHINES_VIEW,
        PERM_RESERVATIONS_VIEW_ALL,
        PERM_RESERVATIONS_VIEW_OWN,
        PERM_RESERVATIONS_CREATE,
        PERM_RESERVATIONS_MANAGE_OWN,
        PERM_RESERVATIONS_CHECKIN_OWN,
        PERM_INVENTORY_VIEW,
        PERM_INVENTORY_CONSUME,
        PERM_CHARGES_VIEW_OWN,
        PERM_MAINTENANCE_VIEW,
    },
    ROLE_VIEWER: {
        PERM_MEMBERS_VIEW_OWN,
        PERM_MACHINES_VIEW,
        PERM_RESERVATIONS_VIEW_ALL,
        PERM_INVENTORY_VIEW,
        PERM_MAINTENANCE_VIEW,
    },
}


# -----------------------------------------------------------------------------
# Permission Evaluation Functions
# -----------------------------------------------------------------------------
def get_user_role(user: Optional[Dict[str, Any]]) -> Optional[str]:
    """Extract and normalize user role from user context dict."""
    if not user or not isinstance(user, dict):
        return None
    role = user.get("role")
    return role.lower() if isinstance(role, str) else None


def get_user_permissions(user: Optional[Dict[str, Any]]) -> Set[str]:
    """Retrieve all effective permissions granted to a user context."""
    role = get_user_role(user)
    if not role or role not in ROLE_PERMISSIONS:
        return set()
    return set(ROLE_PERMISSIONS[role])


def has_permission(user: Optional[Dict[str, Any]], permission: str) -> bool:
    """Check if the user's role grants a specific permission."""
    if not user:
        return False
    role = get_user_role(user)
    if not role or role not in ROLE_PERMISSIONS:
        return False
    return permission in ROLE_PERMISSIONS[role]


def has_any_permission(user: Optional[Dict[str, Any]], *permissions: str) -> bool:
    """Check if user has at least one of the specified permissions."""
    if not user:
        return False
    user_perms = get_user_permissions(user)
    return any(p in user_perms for p in permissions)


def has_all_permissions(user: Optional[Dict[str, Any]], *permissions: str) -> bool:
    """Check if user has all of the specified permissions."""
    if not user:
        return False
    user_perms = get_user_permissions(user)
    return all(p in user_perms for p in permissions)


def is_admin(user: Optional[Dict[str, Any]]) -> bool:
    """Check if user is an Administrator."""
    return get_user_role(user) == ROLE_ADMIN


def is_operator(user: Optional[Dict[str, Any]]) -> bool:
    """Check if user is an Operator."""
    return get_user_role(user) == ROLE_OPERATOR


def is_operator_or_admin(user: Optional[Dict[str, Any]]) -> bool:
    """Check if user is either Operator or Administrator."""
    role = get_user_role(user)
    return role in (ROLE_ADMIN, ROLE_OPERATOR)


# -----------------------------------------------------------------------------
# Resource-Level Ownership and Action Checks
# -----------------------------------------------------------------------------
def can_manage_reservation(
    user: Optional[Dict[str, Any]],
    reservation_member_id: Optional[int] = None,
    reservation_user_id: Optional[int] = None,
) -> bool:
    """Check if user can modify or cancel a reservation.
    
    Admins and Operators can manage any reservation.
    Members can only manage their own reservations.
    Viewers cannot manage any reservation.
    """
    if not user:
        return False

    if has_permission(user, PERM_RESERVATIONS_MANAGE_ALL):
        return True

    if has_permission(user, PERM_RESERVATIONS_MANAGE_OWN):
        # Match by member_id
        user_member_id = user.get("member_id")
        if user_member_id is not None and reservation_member_id is not None:
            if user_member_id == reservation_member_id:
                return True
        # Match by user_id
        if reservation_user_id is not None and user.get("id") == reservation_user_id:
            return True

    return False


def can_checkin_reservation(
    user: Optional[Dict[str, Any]],
    reservation_member_id: Optional[int] = None,
    reservation_user_id: Optional[int] = None,
) -> bool:
    """Check if user can check-in or checkout a reservation."""
    if not user:
        return False

    if has_permission(user, PERM_RESERVATIONS_CHECKIN_ALL):
        return True

    if has_permission(user, PERM_RESERVATIONS_CHECKIN_OWN):
        user_member_id = user.get("member_id")
        if user_member_id is not None and reservation_member_id is not None:
            if user_member_id == reservation_member_id:
                return True
        if reservation_user_id is not None and user.get("id") == reservation_user_id:
            return True

    return False


def can_view_charges(
    user: Optional[Dict[str, Any]],
    target_member_id: Optional[int] = None,
    target_user_id: Optional[int] = None,
) -> bool:
    """Check if user can view charges for a member/user."""
    if not user:
        return False

    if has_permission(user, PERM_CHARGES_VIEW_ALL):
        return True

    if has_permission(user, PERM_CHARGES_VIEW_OWN):
        user_member_id = user.get("member_id")
        if user_member_id is not None and target_member_id is not None:
            return user_member_id == target_member_id
        if target_user_id is not None:
            return user.get("id") == target_user_id

    return False


def can_adjust_charges(user: Optional[Dict[str, Any]]) -> bool:
    """Check if user can issue financial charge adjustments (Admin only)."""
    return has_permission(user, PERM_CHARGES_ADJUST)


def can_manage_security_settings(user: Optional[Dict[str, Any]]) -> bool:
    """Check if user can modify system-wide security settings (Admin only)."""
    return has_permission(user, PERM_SECURITY_MANAGE)


def can_manage_user_roles(user: Optional[Dict[str, Any]]) -> bool:
    """Check if user can assign or modify user roles (Admin only)."""
    return has_permission(user, PERM_USERS_MANAGE_ROLES)
