"""Configuration settings for ForgeDesk makerspace management application."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

# Project Directories
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DATA_DIR = Path(os.environ.get("FORGEDESK_DATA_DIR", PROJECT_ROOT / "data")).resolve()
DB_PATH = Path(os.environ.get("FORGEDESK_DB_PATH", DATA_DIR / "forgedesk.db")).resolve()
UPLOAD_DIR = Path(os.environ.get("FORGEDESK_UPLOAD_DIR", DATA_DIR / "uploads")).resolve()
BACKUP_DIR = Path(os.environ.get("FORGEDESK_BACKUP_DIR", DATA_DIR / "backups")).resolve()
STATIC_DIR = (PACKAGE_DIR / "static").resolve()

# Server Configuration
HOST = os.environ.get("FORGEDESK_HOST", "127.0.0.1")
PORT = int(os.environ.get("FORGEDESK_PORT", "8080"))

def is_loopback_host(host: str) -> bool:
    """Check if the configured host address is a local loopback interface."""
    clean_host = (host or "").strip().lower()
    if clean_host in ("127.0.0.1", "localhost", "::1", "testclient", "test"):
        return True
    if clean_host.startswith("127."):
        return True
    return False

# Timezone Configuration (Canonical Timezone: Europe/Rome)
APP_TIMEZONE_NAME = "Europe/Rome"
APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)

# Security and Session Management
SECRET_KEY = os.environ.get("FORGEDESK_SECRET_KEY", "forgedesk-local-offline-secret-key-3914810")
SESSION_COOKIE_NAME = "fd_session"
SESSION_MAX_AGE_SECONDS = 86400 * 7  # 7 days

# Auto-enforce Secure cookie flag when bound to non-loopback hosts unless explicitly configured
_cookie_secure_env = os.environ.get("FORGEDESK_COOKIE_SECURE")
if _cookie_secure_env is not None:
    COOKIE_SECURE = _cookie_secure_env.lower() in ("1", "true", "yes")
else:
    COOKIE_SECURE = not is_loopback_host(HOST)


def validate_security_configuration(host: str = HOST, cookie_secure: bool = COOKIE_SECURE) -> None:
    """Validate transport security and reject insecure non-loopback configurations (SEC-SESSION-COOKIE-TRANSPORT)."""
    allow_insecure = os.environ.get("FORGEDESK_ALLOW_INSECURE_NON_LOOPBACK", "0").lower() in ("1", "true", "yes")
    if not is_loopback_host(host) and not cookie_secure and not allow_insecure:
        raise ValueError(
            f"Insecure transport configuration: Non-loopback host '{host}' requires COOKIE_SECURE=True / TLS "
            f"to prevent cleartext session bearer cookie transmission. Set FORGEDESK_COOKIE_SECURE=1 or bind to "
            f"loopback (127.0.0.1)."
        )

# Request and Upload Limits
MAX_REQUEST_BODY_SIZE = int(os.environ.get("FORGEDESK_MAX_REQUEST_BODY_SIZE", 15 * 1024 * 1024))  # 15 MB max HTTP request body
MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB limit
ALLOWED_UPLOAD_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".pdf", ".txt", ".csv", ".json", ".zip", ".log"
}

# Roles Definition
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_MEMBER = "member"
ROLE_VIEWER = "viewer"

ALL_ROLES = [ROLE_ADMIN, ROLE_OPERATOR, ROLE_MEMBER, ROLE_VIEWER]

def ensure_directories() -> None:
    """Create essential data, upload, and backup directories if they do not exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
