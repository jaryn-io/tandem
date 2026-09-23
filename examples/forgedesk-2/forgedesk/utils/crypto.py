"""Cryptographic and hashing utilities for ForgeDesk security."""

import hashlib
import hmac
import secrets
from typing import Tuple

PBKDF2_ITERATIONS = 120_000
SALT_SIZE_BYTES = 16


def hash_password(password: str, salt_hex: str = "") -> Tuple[str, str]:
    """Hash a password using PBKDF2-HMAC-SHA256 with a unique salt."""
    if not salt_hex:
        salt = secrets.token_bytes(SALT_SIZE_BYTES)
        salt_hex = salt.hex()
    else:
        salt = bytes.fromhex(salt_hex)

    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return key.hex(), salt_hex


def verify_password(password: str, expected_hash_hex: str, salt_hex: str) -> bool:
    """Verify a plain password against an existing hash and salt in constant time."""
    if not password or not expected_hash_hex or not salt_hex:
        return False
    try:
        calculated_hash_hex, _ = hash_password(password, salt_hex=salt_hex)
        return hmac.compare_digest(calculated_hash_hex, expected_hash_hex)
    except Exception:
        return False


def generate_token(nbytes: int = 32) -> str:
    """Generate a secure cryptographic random URL-safe hex token."""
    return secrets.token_hex(nbytes)


def generate_session_token() -> str:
    """Generate a secure session token."""
    return generate_token(32)


def hash_session_token(token: str) -> str:
    """Compute SHA-256 hash of a session token for secure storage at rest in the database."""
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_csrf_token(session_token: str, secret_key: str) -> str:
    """Generate a deterministic HMAC-SHA256 CSRF token bound to the current session."""
    if not session_token or not secret_key:
        return ""
    msg = f"csrf:{session_token}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_csrf_token(session_token: str, token_to_check: str, secret_key: str) -> bool:
    """Verify a submitted CSRF token against the current session token in constant time."""
    if not session_token or not token_to_check or not secret_key:
        return False
    expected = generate_csrf_token(session_token, secret_key)
    return hmac.compare_digest(expected, token_to_check)


def generate_member_number(sequence_num: int) -> str:
    """Generate a standardized member identification code."""
    return f"FD-MEM-{sequence_num:04d}"

