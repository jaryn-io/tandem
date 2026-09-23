"""Database migration engine and schema definitions for ForgeDesk."""

import logging
import sqlite3
from typing import Any, Callable, Dict, List, Optional

from forgedesk.db.connection import get_connection, transaction
from forgedesk.utils.datetime_tz import now_rome_iso

logger = logging.getLogger("forgedesk.migrations")


MIGRATION_001_SQL = """
-- Migration 001: Initial Core Schema for ForgeDesk Makerspace Management

-- 1. Users and Authentication
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'member', 'viewer')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

-- 2. Sessions
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_token TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(session_token);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- 3. Members
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE SET NULL,
    member_number TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT,
    membership_status TEXT NOT NULL DEFAULT 'active' CHECK (membership_status IN ('active', 'suspended', 'expired')),
    membership_expiry TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_members_user ON members(user_id);
CREATE INDEX IF NOT EXISTS idx_members_number ON members(member_number);
CREATE INDEX IF NOT EXISTS idx_members_status ON members(membership_status);

-- 4. Machine Categories
CREATE TABLE IF NOT EXISTS machine_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_categories_code ON machine_categories(code);

-- 5. Qualifications
CREATE TABLE IF NOT EXISTS qualifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES machine_categories(id) ON DELETE CASCADE,
    qualification_name TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    expiry_date TEXT,
    verified_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (member_id, category_id)
);

CREATE INDEX IF NOT EXISTS idx_qualifications_member ON qualifications(member_id);
CREATE INDEX IF NOT EXISTS idx_qualifications_category ON qualifications(category_id);
CREATE INDEX IF NOT EXISTS idx_qualifications_dates ON qualifications(issue_date, expiry_date);

-- 6. Machines
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES machine_categories(id) ON DELETE RESTRICT,
    required_qualification_category_id INTEGER REFERENCES machine_categories(id) ON DELETE SET NULL,
    capacity INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'available' CHECK (state IN ('available', 'temporarily_unavailable', 'under_maintenance', 'retired')),
    operating_hours_start TEXT NOT NULL DEFAULT '08:00',
    operating_hours_end TEXT NOT NULL DEFAULT '22:00',
    hourly_rate_cents INTEGER NOT NULL DEFAULT 0,
    minimum_charge_cents INTEGER NOT NULL DEFAULT 0,
    peak_hourly_rate_cents INTEGER NOT NULL DEFAULT 0,
    peak_hours_start TEXT DEFAULT '17:00',
    peak_hours_end TEXT DEFAULT '21:00',
    location TEXT,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_machines_category ON machines(category_id);
CREATE INDEX IF NOT EXISTS idx_machines_state ON machines(state);
CREATE INDEX IF NOT EXISTS idx_machines_code ON machines(code);

-- 7. Maintenance Windows
CREATE TABLE IF NOT EXISTS maintenance_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'in_progress', 'completed', 'cancelled')),
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_maint_win_machine ON maintenance_windows(machine_id);
CREATE INDEX IF NOT EXISTS idx_maint_win_times ON maintenance_windows(start_time, end_time);

-- 8. Reservations
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE RESTRICT,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    title TEXT,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('pending', 'confirmed', 'checked_in', 'checked_out', 'cancelled', 'late', 'no_show')),
    recurrence_group_id TEXT,
    recurrence_rule TEXT,
    actual_check_in TEXT,
    actual_check_out TEXT,
    cancellation_reason TEXT,
    cancelled_at TEXT,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_machine_time ON reservations(machine_id, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_reservations_member ON reservations(member_id);
CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);
CREATE INDEX IF NOT EXISTS idx_reservations_recurrence ON reservations(recurrence_group_id);

-- 9. Waiting Lists
CREATE TABLE IF NOT EXISTS waiting_list (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    desired_start_time TEXT NOT NULL,
    desired_end_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'promoted', 'expired', 'cancelled')),
    promoted_reservation_id INTEGER REFERENCES reservations(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_waiting_list_machine ON waiting_list(machine_id, status);
CREATE INDEX IF NOT EXISTS idx_waiting_list_member ON waiting_list(member_id);

-- 10. Inventory Items (Consumables and Replacement Parts)
CREATE TABLE IF NOT EXISTS inventory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'pcs',
    unit_cost_cents INTEGER NOT NULL DEFAULT 0,
    minimum_stock INTEGER NOT NULL DEFAULT 0,
    location TEXT,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_sku ON inventory_items(sku);
CREATE INDEX IF NOT EXISTS idx_inventory_category ON inventory_items(category);

-- 11. Immutable Inventory Movement Ledger
CREATE TABLE IF NOT EXISTS inventory_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES inventory_items(id) ON DELETE RESTRICT,
    movement_type TEXT NOT NULL CHECK (movement_type IN ('receipt', 'reservation', 'consumption', 'release', 'correction')),
    quantity INTEGER NOT NULL,
    unit_cost_cents INTEGER NOT NULL DEFAULT 0,
    reference_type TEXT NOT NULL DEFAULT 'manual' CHECK (reference_type IN ('manual', 'reservation', 'maintenance', 'adjustment', 'initial_stock', 'receipt')),
    reference_id TEXT,
    reason TEXT NOT NULL,
    actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor_name TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_item ON inventory_ledger(item_id);
CREATE INDEX IF NOT EXISTS idx_ledger_type ON inventory_ledger(movement_type);
CREATE INDEX IF NOT EXISTS idx_ledger_created ON inventory_ledger(created_at);

-- 12. Usage Charges
CREATE TABLE IF NOT EXISTS usage_charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER UNIQUE REFERENCES reservations(id) ON DELETE RESTRICT,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE RESTRICT,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    rate_breakdown_json TEXT NOT NULL,
    base_charge_cents INTEGER NOT NULL,
    final_charge_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'finalized' CHECK (status IN ('finalized', 'adjusted', 'voided')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_charges_reservation ON usage_charges(reservation_id);
CREATE INDEX IF NOT EXISTS idx_charges_member ON usage_charges(member_id);
CREATE INDEX IF NOT EXISTS idx_charges_machine ON usage_charges(machine_id);

-- 13. Charge Adjustments
CREATE TABLE IF NOT EXISTS charge_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    charge_id INTEGER NOT NULL REFERENCES usage_charges(id) ON DELETE RESTRICT,
    adjustment_cents INTEGER NOT NULL,
    reason TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_adjustments_charge ON charge_adjustments(charge_id);

-- 14. Maintenance Jobs
CREATE TABLE IF NOT EXISTS maintenance_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    priority TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'completed', 'cancelled')),
    assigned_to_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    opened_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    scheduled_date TEXT,
    completed_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_maint_jobs_machine ON maintenance_jobs(machine_id);
CREATE INDEX IF NOT EXISTS idx_maint_jobs_status ON maintenance_jobs(status);

-- 15. Incidents
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    severity TEXT NOT NULL DEFAULT 'minor' CHECK (severity IN ('minor', 'major', 'critical')),
    status TEXT NOT NULL DEFAULT 'reported' CHECK (status IN ('reported', 'investigating', 'resolved', 'closed')),
    reported_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    takes_machine_out_of_service INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incidents_machine ON incidents(machine_id);
CREATE INDEX IF NOT EXISTS idx_incidents_severity ON incidents(severity);

-- 16. Incident Attachments
CREATE TABLE IF NOT EXISTS incident_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    mime_type TEXT NOT NULL,
    uploaded_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attachments_incident ON incident_attachments(incident_id);

-- 17. Append-Only Audit Log
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor_name TEXT NOT NULL DEFAULT 'system',
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    details_json TEXT,
    before_json TEXT,
    after_json TEXT,
    ip_address TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_log(object_type, object_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id);
"""

MIGRATION_002_SQL = """
-- Migration 002: Immutability triggers for append-only audit_log
CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Audit log is append-only: updates are prohibited.');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Audit log is append-only: deletions are prohibited.');
END;
"""

MIGRATION_003_SQL = """
-- Migration 003: Immutability triggers for usage charges, charge adjustments, and inventory ledger (FD-SEC-003)
CREATE TRIGGER IF NOT EXISTS trg_usage_charges_no_delete
BEFORE DELETE ON usage_charges
BEGIN
    SELECT RAISE(ABORT, 'Usage charges are immutable financial records: deletions are prohibited.');
END;

CREATE TRIGGER IF NOT EXISTS trg_usage_charges_protect_snapshot
BEFORE UPDATE ON usage_charges
BEGIN
    SELECT CASE
        WHEN OLD.id != NEW.id
          OR OLD.reservation_id != NEW.reservation_id
          OR OLD.machine_id != NEW.machine_id
          OR OLD.member_id != NEW.member_id
          OR OLD.start_time != NEW.start_time
          OR OLD.end_time != NEW.end_time
          OR OLD.duration_minutes != NEW.duration_minutes
          OR OLD.rate_breakdown_json != NEW.rate_breakdown_json
          OR OLD.base_charge_cents != NEW.base_charge_cents
          OR OLD.created_at != NEW.created_at
        THEN RAISE(ABORT, 'Usage charge original rate snapshot fields are immutable and cannot be updated.')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_charge_adjustments_no_update
BEFORE UPDATE ON charge_adjustments
BEGIN
    SELECT RAISE(ABORT, 'Charge adjustments ledger is append-only: updates are prohibited.');
END;

CREATE TRIGGER IF NOT EXISTS trg_charge_adjustments_no_delete
BEFORE DELETE ON charge_adjustments
BEGIN
    SELECT RAISE(ABORT, 'Charge adjustments ledger is append-only: deletions are prohibited.');
END;

CREATE TRIGGER IF NOT EXISTS trg_inventory_ledger_no_update
BEFORE UPDATE ON inventory_ledger
BEGIN
    SELECT RAISE(ABORT, 'Inventory ledger is append-only: updates are prohibited.');
END;

CREATE TRIGGER IF NOT EXISTS trg_inventory_ledger_no_delete
BEFORE DELETE ON inventory_ledger
BEGIN
    SELECT RAISE(ABORT, 'Inventory ledger is append-only: deletions are prohibited.');
END;
"""


MIGRATIONS: List[Dict[str, Any]] = [
    {
        "version": 1,
        "name": "001_initial_core_schema",
        "sql": MIGRATION_001_SQL,
    },
    {
        "version": 2,
        "name": "002_audit_immutability_triggers",
        "sql": MIGRATION_002_SQL,
    },
    {
        "version": 3,
        "name": "003_charges_and_ledger_immutability_triggers",
        "sql": MIGRATION_003_SQL,
    },
]


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    """Create schema_migrations table if it does not already exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
    """)


def get_applied_migrations(conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """Return all currently recorded applied migration records."""
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        ensure_migration_table(conn)
        cur = conn.cursor()
        cur.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version ASC;")
        return [dict(r) for r in cur.fetchall()]
    finally:
        if owns_conn:
            conn.close()


def apply_migrations(conn: Optional[sqlite3.Connection] = None) -> List[int]:
    """Execute all unapplied database migrations inside atomic transactions."""
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    applied_versions: List[int] = []
    try:
        ensure_migration_table(conn)
        existing_versions = {
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations;").fetchall()
        }

        for migration in MIGRATIONS:
            ver = migration["version"]
            if ver in existing_versions:
                continue

            logger.info("Applying database migration %03d: %s", ver, migration["name"])
            with transaction(conn) as tx_conn:
                tx_conn.executescript(migration["sql"])
                tx_conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?);",
                    (ver, migration["name"], now_rome_iso()),
                )
            applied_versions.append(ver)

        return applied_versions
    finally:
        if owns_conn:
            conn.close()


def reset_database(conn: Optional[sqlite3.Connection] = None) -> None:
    """Drop all application tables and reset migrations (useful for testing and fresh installs)."""
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        with transaction(conn) as tx_conn:
            tables = [
                "schema_migrations", "audit_log", "incident_attachments", "incidents",
                "maintenance_jobs", "charge_adjustments", "usage_charges", "inventory_ledger",
                "inventory_items", "waiting_list", "reservations", "maintenance_windows",
                "machines", "qualifications", "machine_categories", "members", "sessions", "users"
            ]
            for t in tables:
                tx_conn.execute(f"DROP TABLE IF EXISTS {t};")
        logger.info("Database reset completed.")
    finally:
        if owns_conn:
            conn.close()
