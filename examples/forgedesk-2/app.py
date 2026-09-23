#!/usr/bin/env python3
"""ForgeDesk - Main Application Entrypoint."""

import argparse
import datetime
import logging
import sys

from forgedesk.audit.handlers import register_audit_routes
from forgedesk.auth.handlers import register_auth_routes
from forgedesk.auth.middleware import auth_middleware, csrf_middleware
from forgedesk.billing.handlers import register_billing_routes
from forgedesk.inventory.handlers import register_inventory_routes
from forgedesk.inventory.service import list_inventory_items_with_stock
from forgedesk.machines.handlers import register_machine_routes
from forgedesk.members.handlers import register_member_routes
from forgedesk.reservations.handlers import register_reservation_routes
from forgedesk.system.handlers import register_system_routes
from forgedesk.system.service import create_database_backup, restore_database_backup
from forgedesk.config import (
    APP_TIMEZONE,
    APP_TIMEZONE_NAME,
    DB_PATH,
    HOST,
    PORT,
    ensure_directories,
)
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.core.templates import escape_html, render_page
from forgedesk.db import (
    get_applied_migrations,
    init_db,
    query_all,
    query_one,
    reset_database,
    seed_database,
)
from forgedesk.server import create_server
from forgedesk.utils.datetime_tz import now_rome

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("forgedesk")


def create_application_router() -> Router:
    """Instantiate and configure application route handlers."""
    router = Router()

    # Register global middleware
    router.use(auth_middleware)
    router.use(csrf_middleware)

    # Register authentication routes (/auth/login, /auth/logout, /auth/profile, /api/auth/*, etc.)
    register_auth_routes(router)

    # Register audit logging & inspection routes (/audit, /audit/{id}, /audit/export, /api/audit/*)
    register_audit_routes(router)

    # Register members & qualifications routes (/members, /members/{id}, /api/members/*, etc.)
    register_member_routes(router)

    # Register machines & maintenance routes (/machines, /machines/{id}, /api/machines/*, etc.)
    register_machine_routes(router)

    # Register reservations, availability & timeline routes (/reservations, /availability, /api/reservations/*, etc.)
    register_reservation_routes(router)

    # Register inventory & immutable movement ledger routes (/inventory, /inventory/{id}, /api/inventory/*)
    register_inventory_routes(router)

    # Register usage charges, pricing & billing routes (/charges, /charges/{id}, /api/charges/*)
    register_billing_routes(router)

    # Register system data management, CSV import/export, and backup/restore routes (/system/*)
    register_system_routes(router)

    @router.get("/")
    def index_handler(req: Request) -> Response:
        now_rome_dt = now_rome()
        user = req.user

        # Query database metrics for live dashboard
        stats = {
            "users": 0,
            "members": 0,
            "machines": 0,
            "machines_available": 0,
            "reservations": 0,
            "reservations_active": 0,
            "inventory_items": 0,
            "low_stock_count": 0,
            "open_incidents": 0,
            "open_maintenance": 0,
            "total_charges_cents": 0,
        }
        upcoming_reservations = []
        fleet_machines = []
        open_incidents = []
        open_maintenance = []
        low_stock_items = []
        recent_audit_events = []

        try:
            r_users = query_one("SELECT COUNT(*) AS c FROM users;")
            if r_users:
                stats["users"] = r_users["c"]

            r_members = query_one("SELECT COUNT(*) AS c FROM members WHERE membership_status = 'active';")
            if r_members:
                stats["members"] = r_members["c"]

            r_machines = query_one("SELECT COUNT(*) AS c FROM machines WHERE state != 'retired';")
            if r_machines:
                stats["machines"] = r_machines["c"]

            r_avail = query_one("SELECT COUNT(*) AS c FROM machines WHERE state = 'available';")
            if r_avail:
                stats["machines_available"] = r_avail["c"]

            r_reservations = query_one("SELECT COUNT(*) AS c FROM reservations;")
            if r_reservations:
                stats["reservations"] = r_reservations["c"]

            r_res_act = query_one("SELECT COUNT(*) AS c FROM reservations WHERE status IN ('confirmed', 'in_progress');")
            if r_res_act:
                stats["reservations_active"] = r_res_act["c"]

            r_items = query_one("SELECT COUNT(*) AS c FROM inventory_items;")
            if r_items:
                stats["inventory_items"] = r_items["c"]

            all_low_items = list_inventory_items_with_stock(low_stock_only=True)
            stats["low_stock_count"] = len(all_low_items)
            low_stock_items = all_low_items[:5]

            r_inc = query_one("SELECT COUNT(*) AS c FROM incidents WHERE status != 'closed';")
            if r_inc:
                stats["open_incidents"] = r_inc["c"]

            r_maint = query_one("SELECT COUNT(*) AS c FROM maintenance_jobs WHERE status != 'completed';")
            if r_maint:
                stats["open_maintenance"] = r_maint["c"]

            r_rev = query_one("SELECT COALESCE(SUM(final_charge_cents), 0) AS c FROM usage_charges WHERE status = 'finalized';")
            if r_rev:
                stats["total_charges_cents"] = r_rev["c"]

            # Query lists for dashboard widgets
            upcoming_reservations = query_all("""
                SELECT r.id, r.title, r.start_time, r.end_time, r.status,
                       m.name AS machine_name, m.code AS machine_code,
                       mem.full_name AS member_name, mem.member_number
                FROM reservations r
                JOIN machines m ON r.machine_id = m.id
                JOIN members mem ON r.member_id = mem.id
                ORDER BY r.start_time DESC
                LIMIT 6;
            """)

            fleet_machines = query_all("""
                SELECT m.id, m.code, m.name, m.state, m.capacity, m.location, m.hourly_rate_cents,
                       mc.name AS category_name
                FROM machines m
                LEFT JOIN machine_categories mc ON m.category_id = mc.id
                WHERE m.state != 'retired'
                ORDER BY m.code ASC
                LIMIT 8;
            """)

            open_incidents = query_all("""
                SELECT inc.id, inc.title, inc.severity, inc.status, inc.takes_machine_out_of_service, inc.created_at,
                       m.name AS machine_name, m.code AS machine_code,
                       u.username AS reporter_name
                FROM incidents inc
                JOIN machines m ON inc.machine_id = m.id
                LEFT JOIN users u ON inc.reported_by_user_id = u.id
                WHERE inc.status != 'closed'
                ORDER BY inc.created_at DESC
                LIMIT 5;
            """)

            open_maintenance = query_all("""
                SELECT mj.id, mj.title, mj.priority, mj.status, mj.scheduled_date,
                       m.name AS machine_name, m.code AS machine_code,
                       u.username AS assignee_name
                FROM maintenance_jobs mj
                JOIN machines m ON mj.machine_id = m.id
                LEFT JOIN users u ON mj.assigned_to_user_id = u.id
                WHERE mj.status != 'completed'
                ORDER BY mj.created_at DESC
                LIMIT 5;
            """)

            recent_audit_events = query_all("""
                SELECT id, created_at, actor_name, action, object_type, object_id
                FROM audit_log
                ORDER BY id DESC
                LIMIT 5;
            """)
        except Exception as e:
            logger.warning("Could not fetch dashboard database statistics: %s", e)

        # Build Quick Actions Toolbar
        quick_actions_html = """
        <div style="display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 1rem;">
            <a href="/reservations/new" class="btn btn-primary btn-sm">+ New Reservation</a>
            <a href="/reservations/recurring" class="btn btn-secondary btn-sm">+ Recurring Booking</a>
            <a href="/availability" class="btn btn-secondary btn-sm">Availability Timeline</a>
            <a href="/incidents/new" class="btn btn-danger btn-sm">+ Report Incident</a>
            <a href="/maintenance/jobs/new" class="btn btn-warning btn-sm">+ Log Maintenance</a>
            <a href="/members/new" class="btn btn-secondary btn-sm">+ Register Member</a>
            <a href="/machines/new" class="btn btn-secondary btn-sm">+ Add Machine</a>
            <a href="/audit" class="btn btn-secondary btn-sm">Audit Log</a>
        </div>
        """

        # Format Reservations Widget rows
        if upcoming_reservations:
            res_rows = []
            for r in upcoming_reservations:
                st = r.get("status", "pending")
                pill_class = f"status-pill status-{st}"
                res_rows.append(f"""
                <tr>
                    <td><a href="/reservations/{r['id']}" style="font-family: var(--font-mono); font-weight: 600; color: #2563eb; text-decoration: none;">#{r['id']}</a></td>
                    <td><strong>{escape_html(r['machine_name'])}</strong> <span style="font-size: 0.75rem; color: #64748b;">({escape_html(r['machine_code'])})</span></td>
                    <td>{escape_html(r['member_name'])}</td>
                    <td style="font-size: 0.85rem; color: #475569;">{escape_html(r['start_time'])} &rarr; {escape_html(r['end_time'])}</td>
                    <td><span class="{pill_class}">{escape_html(st.replace('_', ' ').title())}</span></td>
                    <td style="text-align: right;"><a href="/reservations/{r['id']}" class="btn btn-sm btn-secondary">View</a></td>
                </tr>
                """)
            res_table_html = f"""
            <div class="table-responsive">
                <table class="table">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Machine</th>
                            <th>Member</th>
                            <th>Time Window</th>
                            <th>Status</th>
                            <th style="text-align: right;">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(res_rows)}
                    </tbody>
                </table>
            </div>
            """
        else:
            res_table_html = '<p style="color: #64748b; padding: 1rem 0;">No recent or upcoming reservations found. Use the quick actions above to book machine time.</p>'

        # Format Machines Fleet Widget rows
        if fleet_machines:
            m_rows = []
            for m in fleet_machines:
                mst = m.get("state", "available")
                pill_class = f"status-pill status-{mst}"
                rate_eur = f"{m.get('hourly_rate_cents', 0) / 100:.2f} €/h"
                m_rows.append(f"""
                <tr>
                    <td><a href="/machines/{m['id']}" style="font-family: var(--font-mono); font-weight: 600; color: #2563eb; text-decoration: none;">{escape_html(m['code'])}</a></td>
                    <td>{escape_html(m['name'])}</td>
                    <td><span style="font-size: 0.85rem; color: #64748b;">{escape_html(m.get('category_name') or 'General')}</span></td>
                    <td><span class="{pill_class}">{escape_html(mst.replace('_', ' ').title())}</span></td>
                    <td>{m.get('capacity', 1)} slot(s)</td>
                    <td style="font-family: var(--font-mono); font-size: 0.85rem;">{rate_eur}</td>
                    <td style="text-align: right;"><a href="/machines/{m['id']}" class="btn btn-sm btn-secondary">Profile</a></td>
                </tr>
                """)
            m_table_html = f"""
            <div class="table-responsive">
                <table class="table">
                    <thead>
                        <tr>
                            <th>Code</th>
                            <th>Machine</th>
                            <th>Category</th>
                            <th>Status</th>
                            <th>Capacity</th>
                            <th>Rate</th>
                            <th style="text-align: right;">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(m_rows)}
                    </tbody>
                </table>
            </div>
            """
        else:
            m_table_html = '<p style="color: #64748b; padding: 1rem 0;">No active machines configured in the workshop fleet.</p>'

        # Format Incidents & Maintenance Widget
        alerts_items = []
        for inc in open_incidents:
            sev_class = "status-danger" if inc["severity"] in ("critical", "major") else "status-warning"
            alerts_items.append(f"""
            <div style="padding: 0.75rem; border-bottom: 1px solid #e2e8f0; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <span class="status-pill {sev_class}" style="font-size: 0.7rem; margin-right: 0.5rem;">{escape_html(inc['severity'].upper())} INCIDENT</span>
                    <a href="/incidents/{inc['id']}" style="font-weight: 600; color: #0f172a; text-decoration: none;">{escape_html(inc['title'])}</a>
                    <div style="font-size: 0.8rem; color: #64748b; margin-top: 0.25rem;">
                        Target: <strong>{escape_html(inc['machine_name'])}</strong> &bull; Reporter: {escape_html(inc.get('reporter_name') or 'Staff')} &bull; Status: {escape_html(inc['status'])}
                    </div>
                </div>
                <a href="/incidents/{inc['id']}" class="btn btn-sm btn-danger">Inspect</a>
            </div>
            """)

        for mj in open_maintenance:
            prio_class = "status-danger" if mj["priority"] == "critical" else ("status-warning" if mj["priority"] == "high" else "status-info")
            alerts_items.append(f"""
            <div style="padding: 0.75rem; border-bottom: 1px solid #e2e8f0; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <span class="status-pill {prio_class}" style="font-size: 0.7rem; margin-right: 0.5rem;">{escape_html(mj['priority'].upper())} MAINT</span>
                    <a href="/maintenance-jobs/{mj['id']}" style="font-weight: 600; color: #0f172a; text-decoration: none;">{escape_html(mj['title'])}</a>
                    <div style="font-size: 0.8rem; color: #64748b; margin-top: 0.25rem;">
                        Machine: <strong>{escape_html(mj['machine_name'])}</strong> &bull; Assignee: {escape_html(mj.get('assignee_name') or 'Unassigned')} &bull; Status: {escape_html(mj['status'])}
                    </div>
                </div>
                <a href="/maintenance-jobs/{mj['id']}" class="btn btn-sm btn-warning">View</a>
            </div>
            """)

        if alerts_items:
            alerts_html = "".join(alerts_items)
        else:
            alerts_html = '<p style="color: #16a34a; padding: 1rem 0; font-weight: 500;">&check; All machines operational. Zero open incidents or pending maintenance tasks.</p>'

        # Format Low Stock Inventory Widget
        if low_stock_items:
            stock_rows = []
            for item in low_stock_items:
                stock_rows.append(f"""
                <tr>
                    <td><span style="font-family: var(--font-mono); font-size: 0.85rem; font-weight: 600;">{escape_html(item['sku'])}</span></td>
                    <td>{escape_html(item['name'])}</td>
                    <td style="color: #dc2626; font-weight: 700;">{item.get('available', 0)} {escape_html(item.get('unit') or 'pcs')}</td>
                    <td style="color: #64748b;">{item['minimum_stock']} {escape_html(item.get('unit') or 'pcs')}</td>
                    <td style="text-align: right;"><a href="/inventory/{item['id']}" class="btn btn-sm btn-secondary">Restock</a></td>
                </tr>
                """)
            stock_table_html = f"""
            <div class="table-responsive">
                <table class="table">
                    <thead>
                        <tr>
                            <th>SKU</th>
                            <th>Item Name</th>
                            <th>Current Stock</th>
                            <th>Min Stock</th>
                            <th style="text-align: right;">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(stock_rows)}
                    </tbody>
                </table>
            </div>
            """
        else:
            stock_table_html = '<p style="color: #16a34a; padding: 1rem 0; font-weight: 500;">&check; Inventory levels healthy. No low-stock items.</p>'

        # Format Recent Audit Events
        if recent_audit_events:
            audit_rows = []
            for a in recent_audit_events:
                audit_rows.append(f"""
                <tr>
                    <td style="font-size: 0.8rem; color: #64748b;">{escape_html(a['created_at'])}</td>
                    <td><strong>{escape_html(a.get('actor_name') or 'system')}</strong></td>
                    <td><span class="status-pill status-info" style="font-size: 0.7rem;">{escape_html(a['action'])}</span></td>
                    <td style="font-size: 0.85rem;">{escape_html(a['object_type'])} #{a.get('object_id') or ''}</td>
                    <td style="text-align: right;"><a href="/audit/{a['id']}" class="btn btn-sm btn-secondary">Inspect</a></td>
                </tr>
                """)
            audit_table_html = f"""
            <div class="table-responsive">
                <table class="table">
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Actor</th>
                            <th>Action</th>
                            <th>Target</th>
                            <th style="text-align: right;">Details</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(audit_rows)}
                    </tbody>
                </table>
            </div>
            """
        else:
            audit_table_html = '<p style="color: #64748b; padding: 1rem 0;">No audit events recorded yet.</p>'

        rev_eur = f"{stats['total_charges_cents'] / 100:.2f} €"

        content = f"""
        <div class="card" style="margin-bottom: 1.5rem;">
            <div class="card-header" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem;">
                <div>
                    <h2 class="card-title" style="margin: 0; font-size: 1.5rem;">ForgeDesk Makerspace Operations</h2>
                    <p style="color: #64748b; margin: 0.25rem 0 0; font-size: 0.9rem;">
                        Real-time workshop status operating in <strong>{APP_TIMEZONE_NAME}</strong> &bull; Current Time: <strong>{now_rome_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}</strong>
                    </p>
                </div>
            </div>
            <div class="card-body">
                {quick_actions_html}

                <div class="metrics-row" style="margin-top: 1.5rem;">
                    <div class="metric-card">
                        <span class="metric-label">Active Members</span>
                        <span class="metric-value">{stats['members']}</span>
                        <span class="metric-subtext">{stats['users']} Accounts Registered</span>
                    </div>
                    <div class="metric-card">
                        <span class="metric-label">Operating Machines</span>
                        <span class="metric-value" style="color: #16a34a;">{stats['machines_available']} / {stats['machines']}</span>
                        <span class="metric-subtext">Fleet Online & Available</span>
                    </div>
                    <div class="metric-card">
                        <span class="metric-label">Active Bookings</span>
                        <span class="metric-value">{stats['reservations_active']}</span>
                        <span class="metric-subtext">{stats['reservations']} Total Historical</span>
                    </div>
                    <div class="metric-card">
                        <span class="metric-label">Open Incidents / Maint</span>
                        <span class="metric-value" style="color: {'#dc2626' if (stats['open_incidents'] + stats['open_maintenance'] > 0) else '#16a34a'};">
                            {stats['open_incidents']} / {stats['open_maintenance']}
                        </span>
                        <span class="metric-subtext">Tasks Requiring Attention</span>
                    </div>
                    <div class="metric-card">
                        <span class="metric-label">Low Stock Alerts</span>
                        <span class="metric-value" style="color: {'#eab308' if stats['low_stock_count'] > 0 else '#16a34a'};">
                            {stats['low_stock_count']}
                        </span>
                        <span class="metric-subtext">{stats['inventory_items']} Total Inventory SKUs</span>
                    </div>
                    <div class="metric-card">
                        <span class="metric-label">Settled Usage Revenue</span>
                        <span class="metric-value" style="font-size: 1.25rem; font-family: var(--font-mono);">{rev_eur}</span>
                        <span class="metric-subtext">Finalized Billing Charges</span>
                    </div>
                </div>
            </div>
        </div>

        <div style="display: grid; grid-template-columns: 2fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem;">
            <div class="card">
                <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
                    <h3 class="card-title" style="font-size: 1.15rem;">Recent & Upcoming Reservations</h3>
                    <a href="/reservations" style="font-size: 0.85rem; color: #2563eb; text-decoration: none; font-weight: 600;">View All &rarr;</a>
                </div>
                <div class="card-body">
                    {res_table_html}
                </div>
            </div>

            <div class="card">
                <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
                    <h3 class="card-title" style="font-size: 1.15rem;">Incidents & Maintenance</h3>
                    <a href="/incidents" style="font-size: 0.85rem; color: #2563eb; text-decoration: none; font-weight: 600;">All Incidents &rarr;</a>
                </div>
                <div class="card-body" style="padding: 0;">
                    {alerts_html}
                </div>
            </div>
        </div>

        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem;">
            <div class="card">
                <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
                    <h3 class="card-title" style="font-size: 1.15rem;">Workshop Machine Fleet</h3>
                    <a href="/machines" style="font-size: 0.85rem; color: #2563eb; text-decoration: none; font-weight: 600;">Manage Fleet &rarr;</a>
                </div>
                <div class="card-body">
                    {m_table_html}
                </div>
            </div>

            <div class="card">
                <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
                    <h3 class="card-title" style="font-size: 1.15rem;">Low-Stock Consumables Alert</h3>
                    <a href="/inventory" style="font-size: 0.85rem; color: #2563eb; text-decoration: none; font-weight: 600;">Full Inventory &rarr;</a>
                </div>
                <div class="card-body">
                    {stock_table_html}
                </div>
            </div>
        </div>

        <div class="card">
            <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
                <h3 class="card-title" style="font-size: 1.15rem;">Immutable Audit Activity Trail</h3>
                <a href="/audit" style="font-size: 0.85rem; color: #2563eb; text-decoration: none; font-weight: 600;">Full Audit Log &rarr;</a>
            </div>
            <div class="card-body">
                {audit_table_html}
            </div>
        </div>
        """
        html = render_page("Dashboard", content, user=req.user, active_nav="dashboard")
        return Response.html(html)

    @router.get("/health")
    def health_handler(req: Request) -> Response:
        now_rome_dt = now_rome()
        db_ok = False
        user_count = 0
        migration_count = 0
        try:
            r = query_one("SELECT COUNT(*) AS c FROM users;")
            if r:
                user_count = r["c"]
                db_ok = True
            migrations = get_applied_migrations()
            migration_count = len(migrations)
        except Exception as e:
            logger.error("Health check database query failed: %s", e)

        return Response.json({
            "status": "healthy" if db_ok else "degraded",
            "app": "ForgeDesk",
            "timezone": APP_TIMEZONE_NAME,
            "server_time": now_rome_dt.isoformat(),
            "database": {
                "status": "connected" if db_ok else "error",
                "path": str(DB_PATH),
                "users_count": user_count,
                "migrations_applied": migration_count,
            },
        })

    return router


def main() -> None:
    """Parse CLI arguments, prepare directories, apply DB migrations/seeds, and start server."""
    parser = argparse.ArgumentParser(description="ForgeDesk Makerspace Management Web Application")
    parser.add_argument("--host", default=HOST, help=f"Host address to bind (default: {HOST})")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port number to listen on (default: {PORT})")
    parser.add_argument("--init-db", action="store_true", help="Apply pending database schema migrations and exit")
    parser.add_argument("--seed", action="store_true", help="Seed database with representative data and exit")
    parser.add_argument("--reset-db", action="store_true", help="Reset all database tables and exit")
    parser.add_argument("--backup", nargs="?", const="default", help="Perform live online SQLite database backup and exit")
    parser.add_argument("--restore", type=str, help="Restore database from validated backup file and exit")
    args = parser.parse_args()

    ensure_directories()

    if args.reset_db:
        print("Resetting database...")
        reset_database()
        print("Database reset complete.")
        sys.exit(0)

    if args.init_db:
        print("Initializing database migrations...")
        init_result = init_db(seed_if_empty=False)
        print(f"Migrations applied: {init_result['applied_migrations']}")
        sys.exit(0)

    if args.seed:
        print("Seeding database...")
        init_db(seed_if_empty=False)
        seed_result = seed_database(force=True)
        print(f"Seed result: {seed_result}")
        sys.exit(0)

    if args.backup:
        dest = None if args.backup == "default" else args.backup
        print("Creating online database backup...")
        backup_path = create_database_backup(destination_path=dest)
        print(f"Backup successfully created at: {backup_path}")
        sys.exit(0)

    if args.restore:
        print(f"Restoring database from '{args.restore}'...")
        res = restore_database_backup(backup_path=args.restore, actor_user={"id": 1, "username": "cli_admin", "role": "admin"})
        print(f"Database successfully restored! Summary: {res}")
        sys.exit(0)

    # Automatic startup DB migration and seed check
    init_db(seed_if_empty=True)

    router = create_application_router()
    server = create_server(router, host=args.host, port=args.port)

    print("=" * 65)
    print("  ⚡ ForgeDesk - Makerspace Management System (Linux Offline)")
    print(f"  ⚡ Running at: http://{args.host}:{args.port}/")
    print(f"  ⚡ Timezone:   {APP_TIMEZONE_NAME}")
    print(f"  ⚡ Database:   {DB_PATH}")
    print("=" * 65)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down ForgeDesk server...")
        server.shutdown()
        server.server_close()
        print("ForgeDesk server stopped.")


if __name__ == "__main__":
    main()
