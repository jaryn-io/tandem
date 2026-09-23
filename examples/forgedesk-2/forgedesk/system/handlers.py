"""HTTP request handlers for System Data Management, CSV Import/Export, Backup, and Restore."""

import datetime
import html
import io
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlencode

from forgedesk.audit.service import export_audit_logs_csv, export_audit_logs_json
from forgedesk.auth.middleware import is_api_request, require_auth, require_permission
from forgedesk.auth.permissions import (
    PERM_AUDIT_VIEW,
    PERM_SYSTEM_BACKUP,
    PERM_SYSTEM_EXPORT,
    PERM_SYSTEM_IMPORT,
    PERM_SYSTEM_RESET,
    PERM_SYSTEM_RESTORE,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    get_user_role,
    has_permission,
    is_operator_or_admin,
)
from forgedesk.config import BACKUP_DIR, DB_PATH
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.core.templates import csrf_input, escape_html, render_page
from forgedesk.system.service import (
    create_database_backup,
    execute_csv_import,
    export_charges_csv,
    export_charges_json,
    export_full_system_json,
    export_inventory_csv,
    export_inventory_json,
    export_inventory_ledger_csv,
    export_machines_csv,
    export_machines_json,
    export_members_csv,
    export_members_json,
    export_reservations_csv,
    export_reservations_json,
    get_sample_csv,
    preview_csv_import,
    restore_database_backup,
    validate_backup_file,
)
from forgedesk.utils.datetime_tz import format_display, now_rome, now_rome_iso

logger = logging.getLogger("forgedesk.system.handlers")


def register_system_routes(router: Router) -> None:
    """Register all HTML views and REST API endpoints for Import/Export, Backup & Restore."""

    # =========================================================================
    # 1. Central Data Management Hub & Export Center (HTML)
    # =========================================================================

    @router.get("/system/data")
    @require_permission(PERM_SYSTEM_EXPORT)
    def system_data_hub(req: Request) -> Response:
        user = req.user
        can_import = has_permission(user, PERM_SYSTEM_IMPORT)
        can_backup = has_permission(user, PERM_SYSTEM_BACKUP)
        can_restore = has_permission(user, PERM_SYSTEM_RESTORE)

        db_size_mb = DB_PATH.stat().st_size / (1024 * 1024) if DB_PATH.is_file() else 0.0

        content = f"""
        <div class="page-header" style="margin-bottom: 2rem;">
            <div class="header-content">
                <h1 class="page-title">Data Management &amp; System Operations</h1>
                <p class="page-subtitle">Export business records, bulk import CSV datasets, and manage offline SQLite backups.</p>
            </div>
            <div class="header-actions">
                <a href="/audit" class="btn btn-outline-primary">🔍 Search Audit Trail</a>
            </div>
        </div>

        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1.5rem; margin-bottom: 2rem;">
            <!-- Export Card -->
            <div class="card">
                <div class="card-header" style="background-color: #f8fafc; border-bottom: 1px solid #e2e8f0;">
                    <h3 class="card-title" style="font-size: 1.15rem; color: #1e293b;">📥 Data Export Center</h3>
                </div>
                <div class="card-body">
                    <p style="font-size: 0.9rem; color: #64748b; margin-bottom: 1.25rem;">
                        Download standardized RFC 4180 CSV spreadsheets or structured JSON documents for all core domains.
                    </p>
                    <div style="display: flex; flex-direction: column; gap: 0.75rem;">
                        <div style="display: flex; justify-content: space-between; align-items: center; padding: 0.5rem 0; border-bottom: 1px solid #f1f5f9;">
                            <span style="font-weight: 500;">👥 Members &amp; Qualifications</span>
                            <div style="display: flex; gap: 0.4rem;">
                                <a href="/members/export?format=csv" class="btn btn-sm btn-outline-primary" download>CSV</a>
                                <a href="/members/export?format=json" class="btn btn-sm btn-outline-primary" download>JSON</a>
                            </div>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: center; padding: 0.5rem 0; border-bottom: 1px solid #f1f5f9;">
                            <span style="font-weight: 500;">⚙️ Machines &amp; Configurations</span>
                            <div style="display: flex; gap: 0.4rem;">
                                <a href="/machines/export?format=csv" class="btn btn-sm btn-outline-primary" download>CSV</a>
                                <a href="/machines/export?format=json" class="btn btn-sm btn-outline-primary" download>JSON</a>
                            </div>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: center; padding: 0.5rem 0; border-bottom: 1px solid #f1f5f9;">
                            <span style="font-weight: 500;">📅 Reservations &amp; Usage</span>
                            <div style="display: flex; gap: 0.4rem;">
                                <a href="/reservations/export?format=csv" class="btn btn-sm btn-outline-primary" download>CSV</a>
                                <a href="/reservations/export?format=json" class="btn btn-sm btn-outline-primary" download>JSON</a>
                            </div>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: center; padding: 0.5rem 0; border-bottom: 1px solid #f1f5f9;">
                            <span style="font-weight: 500;">📦 Inventory Items &amp; Stock</span>
                            <div style="display: flex; gap: 0.4rem;">
                                <a href="/inventory/export?format=csv" class="btn btn-sm btn-outline-primary" download>CSV</a>
                                <a href="/inventory/export?format=json" class="btn btn-sm btn-outline-primary" download>JSON</a>
                            </div>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: center; padding: 0.5rem 0; border-bottom: 1px solid #f1f5f9;">
                            <span style="font-weight: 500;">📜 Immutable Inventory Ledger</span>
                            <div style="display: flex; gap: 0.4rem;">
                                <a href="/inventory/ledger/export?format=csv" class="btn btn-sm btn-outline-primary" download>CSV</a>
                            </div>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: center; padding: 0.5rem 0; border-bottom: 1px solid #f1f5f9;">
                            <span style="font-weight: 500;">💶 Charges &amp; Adjustments</span>
                            <div style="display: flex; gap: 0.4rem;">
                                <a href="/charges/export?format=csv" class="btn btn-sm btn-outline-primary" download>CSV</a>
                                <a href="/charges/export?format=json" class="btn btn-sm btn-outline-primary" download>JSON</a>
                            </div>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: center; padding: 0.5rem 0; border-bottom: 1px solid #f1f5f9;">
                            <span style="font-weight: 500;">🛡️ Audit Activity Trail</span>
                            <div style="display: flex; gap: 0.4rem;">
                                <a href="/audit/export?format=csv" class="btn btn-sm btn-outline-primary" download>CSV</a>
                                <a href="/audit/export?format=json" class="btn btn-sm btn-outline-primary" download>JSON</a>
                            </div>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: center; padding: 0.5rem 0;">
                            <span style="font-weight: 600; color: #2563eb;">💾 Full Database JSON Dump</span>
                            <div style="display: flex; gap: 0.4rem;">
                                <a href="/system/export/all" class="btn btn-sm btn-primary" download>Download JSON</a>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- CSV Import Card -->
            <div class="card">
                <div class="card-header" style="background-color: #f8fafc; border-bottom: 1px solid #e2e8f0;">
                    <h3 class="card-title" style="font-size: 1.15rem; color: #1e293b;">📤 CSV Bulk Import</h3>
                </div>
                <div class="card-body">
                    <p style="font-size: 0.9rem; color: #64748b; margin-bottom: 1.25rem;">
                        Import new members, machines, or inventory items from CSV spreadsheets with interactive row-by-row preview, constraint validation, and atomic rollback.
                    </p>
                    <div style="display: flex; flex-direction: column; gap: 0.75rem;">
                        <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 0.85rem; font-size: 0.85rem; color: #166534;">
                            <strong>🛡️ Atomic Failure-Safety:</strong> If any single row fails validation or database insertion, the entire batch rolls back completely.
                        </div>
                        <div style="margin-top: 1rem;">
                            <a href="/system/import" class="btn btn-primary" style="width: 100%; text-align: center;">Go to CSV Import Tool &rarr;</a>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Backup & Restore Card -->
            <div class="card">
                <div class="card-header" style="background-color: #f8fafc; border-bottom: 1px solid #e2e8f0;">
                    <h3 class="card-title" style="font-size: 1.15rem; color: #1e293b;">💽 Backup &amp; Disaster Recovery</h3>
                </div>
                <div class="card-body">
                    <p style="font-size: 0.9rem; color: #64748b; margin-bottom: 1.25rem;">
                        Create live, non-blocking online SQLite database backups or restore from a verified backup file.
                    </p>
                    <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 0.85rem; margin-bottom: 1.25rem; font-size: 0.85rem;">
                        <div><strong>Active Database:</strong> <code>{escape_html(DB_PATH.name)}</code> ({db_size_mb:.2f} MB)</div>
                        <div style="margin-top: 0.35rem;"><strong>Timezone:</strong> Europe/Rome</div>
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 0.6rem;">
                        <a href="/system/backup" class="btn btn-outline-primary" style="width: 100%; text-align: center;">Backup &amp; Restore Manager &rarr;</a>
                    </div>
                </div>
            </div>
        </div>
        """
        return Response.html(render_page("Data Management Hub", content, user=user, active_nav="audit"))

    # =========================================================================
    # 2. CSV Bulk Import UI & Preview (HTML)
    # =========================================================================

    @router.get("/system/import")
    @require_permission(PERM_SYSTEM_IMPORT)
    def system_import_view(req: Request) -> Response:
        user = req.user
        selected_type = (req.query("type") or "members").lower()
        if selected_type not in ("members", "machines", "inventory"):
            selected_type = "members"

        sample_csv = get_sample_csv(selected_type)

        content = f"""
        <div class="page-header" style="margin-bottom: 1.5rem;">
            <div class="header-content">
                <h1 class="page-title">CSV Bulk Data Import</h1>
                <p class="page-subtitle">Upload or paste CSV datasets with interactive row-by-row validation, preview, and atomic rollback guarantee.</p>
            </div>
            <div class="header-actions">
                <a href="/system/data" class="btn btn-outline-secondary">&larr; Back to Data Hub</a>
            </div>
        </div>

        <div style="display: flex; gap: 0.5rem; margin-bottom: 1.5rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">
            <a href="/system/import?type=members" class="btn btn-sm {'btn-primary' if selected_type == 'members' else 'btn-outline-secondary'}">👥 Members</a>
            <a href="/system/import?type=machines" class="btn btn-sm {'btn-primary' if selected_type == 'machines' else 'btn-outline-secondary'}">⚙️ Machines</a>
            <a href="/system/import?type=inventory" class="btn btn-sm {'btn-primary' if selected_type == 'inventory' else 'btn-outline-secondary'}">📦 Inventory Items</a>
        </div>

        <div class="card" style="margin-bottom: 2rem;">
            <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
                <h3 class="card-title" style="font-size: 1.1rem; text-transform: capitalize;">Import {escape_html(selected_type)} from CSV</h3>
                <a href="/system/import/sample?type={selected_type}" class="btn btn-sm btn-outline-primary" download="sample_{selected_type}.csv">📄 Download Sample CSV</a>
            </div>
            <div class="card-body">
                <form action="/system/import/preview" method="POST" enctype="multipart/form-data">
                    {csrf_input(req.cookies.get("fd_csrf", ""))}
                    <input type="hidden" name="entity_type" value="{escape_html(selected_type)}">

                    <div class="form-group" style="margin-bottom: 1.25rem;">
                        <label class="form-label" style="font-weight: 600;">Option A: Upload CSV File</label>
                        <input type="file" name="csv_file" accept=".csv,text/csv,text/plain" class="form-control" style="padding: 0.5rem;">
                    </div>

                    <div class="form-group" style="margin-bottom: 1.25rem;">
                        <label class="form-label" style="font-weight: 600;">Option B: Or Paste CSV Text Below</label>
                        <textarea name="csv_text" rows="8" class="form-control" placeholder="{escape_html(sample_csv)}" style="font-family: monospace; font-size: 0.85rem;">{escape_html(sample_csv)}</textarea>
                    </div>

                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span style="font-size: 0.85rem; color: #64748b;">Step 1 of 2: Click Preview to validate all records before saving.</span>
                        <button type="submit" class="btn btn-primary">🔍 Validate &amp; Preview CSV</button>
                    </div>
                </form>
            </div>
        </div>
        """
        return Response.html(render_page(f"Import {selected_type.title()} (CSV)", content, user=user, active_nav="audit"))

    @router.get("/system/import/sample")
    @require_permission(PERM_SYSTEM_IMPORT)
    def system_import_sample_download(req: Request) -> Response:
        entity_type = (req.query("type") or "members").lower()
        sample = get_sample_csv(entity_type)
        if not sample:
            return Response.html("<h1>Unknown Entity Type</h1>", status_code=404)

        headers = {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="sample_{entity_type}.csv"',
        }
        return Response(body=sample, status_code=200, content_type="text/csv", headers=headers)

    @router.post("/system/import/preview")
    @require_permission(PERM_SYSTEM_IMPORT)
    def system_import_preview_handler(req: Request) -> Response:
        user = req.user
        entity_type = (req.form("entity_type") or "members").lower()
        csv_text = req.form("csv_text") or ""

        # Check uploaded file
        uploaded_file = req.file("csv_file")
        if uploaded_file and uploaded_file.data:
            try:
                csv_text = uploaded_file.data.decode("utf-8", errors="replace")
            except Exception as e:
                return Response.html(f"<h1>Could not read uploaded file: {e}</h1>", status_code=400)

        if not csv_text.strip():
            flash = [{"category": "danger", "text": "Please provide a valid CSV file or paste CSV text."}]
            return system_import_view(req)

        try:
            preview = preview_csv_import(entity_type=entity_type, csv_text=csv_text, actor_user=user)
        except Exception as e:
            flash = [{"category": "danger", "text": f"CSV Parse Error: {e}"}]
            content = f"""
            <div class="alert alert-danger">
                <h4>CSV Validation Error</h4>
                <p>{escape_html(str(e))}</p>
            </div>
            <div style="margin-top: 1.5rem;">
                <a href="/system/import?type={entity_type}" class="btn btn-outline-secondary">&larr; Return to CSV Import</a>
            </div>
            """
            return Response.html(render_page("CSV Import Error", content, user=user, flash_messages=flash, active_nav="audit"))

        # Render Preview Table
        status_banner = ""
        if preview["can_import"]:
            status_banner = f"""
            <div class="alert alert-success" style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <strong>✅ Validation Passed:</strong> All {preview['valid_rows_count']} rows are valid and ready to be imported atomically.
                </div>
            </div>
            """
        else:
            status_banner = f"""
            <div class="alert alert-danger">
                <strong>❌ Validation Failed:</strong> {preview['invalid_rows_count']} out of {preview['total_rows']} rows contained errors.
                Because ForgeDesk enforces atomic imports, you must fix all errors in your CSV before executing the import.
            </div>
            """

        # Table rows
        table_rows = []
        for r in preview["rows"]:
            row_idx = r["row_index"]
            is_valid = r["is_valid"]
            badge = '<span class="badge badge-success">VALID</span>' if is_valid else '<span class="badge badge-danger">INVALID</span>'

            errors_html = ""
            if r["errors"]:
                err_items = "".join([f"<li>{escape_html(err)}</li>" for err in r["errors"]])
                errors_html = f'<ul style="margin: 0; padding-left: 1.2rem; color: #dc2626; font-size: 0.85rem;">{err_items}</ul>'

            raw_summary = ", ".join([f"<strong>{escape_html(k)}</strong>: {escape_html(v)}" for k, v in list(r["raw"].items())[:4]])

            table_rows.append(f"""
            <tr style="background-color: {'#ffffff' if is_valid else '#fef2f2'};">
                <td style="font-weight: 600; text-align: center;">#{row_idx}</td>
                <td style="text-align: center;">{badge}</td>
                <td><div style="font-size: 0.85rem; font-family: monospace;">{raw_summary}</div></td>
                <td>{errors_html if errors_html else '<span style="color: #166534; font-size: 0.85rem;">No errors</span>'}</td>
            </tr>
            """)

        rows_html = "\n".join(table_rows)

        # Form for Execution
        execution_section = ""
        if preview["can_import"]:
            execution_section = f"""
            <div class="card" style="border: 2px solid #22c55e; margin-top: 2rem;">
                <div class="card-body" style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <h4 style="margin: 0 0 0.35rem 0; color: #15803d;">Ready to Import {preview['valid_rows_count']} Records</h4>
                        <p style="margin: 0; font-size: 0.85rem; color: #64748b;">
                            All records will be committed to the database inside a single atomic transaction.
                        </p>
                    </div>
                    <form action="/system/import/execute" method="POST" style="margin: 0;">
                        {csrf_input(req.cookies.get("fd_csrf", ""))}
                        <input type="hidden" name="entity_type" value="{escape_html(entity_type)}">
                        <input type="hidden" name="csv_text" value="{escape_html(csv_text)}">
                        <button type="submit" class="btn btn-success btn-lg">🚀 Confirm &amp; Execute Import</button>
                    </form>
                </div>
            </div>
            """

        content = f"""
        <div class="page-header" style="margin-bottom: 1.5rem;">
            <div class="header-content">
                <h1 class="page-title">CSV Import Preview &amp; Validation Report</h1>
                <p class="page-subtitle">Entity: <strong>{escape_html(entity_type.upper())}</strong> &bull; Total Rows: <strong>{preview['total_rows']}</strong></p>
            </div>
            <div class="header-actions">
                <a href="/system/import?type={entity_type}" class="btn btn-outline-secondary">&larr; Back to CSV Editor</a>
            </div>
        </div>

        {status_banner}

        <div class="card" style="margin-bottom: 2rem;">
            <div class="card-header">
                <h3 class="card-title">Row-by-Row Verification Results</h3>
            </div>
            <div class="table-responsive">
                <table class="table" style="width: 100%; border-collapse: collapse;">
                    <thead>
                        <tr style="background-color: #f8fafc; border-bottom: 2px solid #e2e8f0;">
                            <th style="width: 60px; text-align: center;">Row</th>
                            <th style="width: 90px; text-align: center;">Status</th>
                            <th>Parsed Row Data</th>
                            <th>Validation Diagnostics</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        {execution_section}
        """
        return Response.html(render_page(f"Preview {entity_type.title()} Import", content, user=user, active_nav="audit"))

    @router.post("/system/import/execute")
    @require_permission(PERM_SYSTEM_IMPORT)
    def system_import_execute_handler(req: Request) -> Response:
        user = req.user
        entity_type = (req.form("entity_type") or "members").lower()
        csv_text = req.form("csv_text") or ""
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            result = execute_csv_import(
                entity_type=entity_type,
                csv_text=csv_text,
                actor_user=user,
                ip_address=client_ip,
            )
            flash = [{
                "category": "success",
                "text": f"Successfully imported {result['imported_count']} {entity_type} records atomically!",
            }]
            # Redirect to corresponding domain view
            redirect_map = {
                "members": "/members",
                "machines": "/machines",
                "inventory": "/inventory",
            }
            target_url = redirect_map.get(entity_type, "/system/data")
            return Response.redirect(target_url)
        except Exception as e:
            logger.error("Failed to execute CSV import: %s", e)
            flash = [{"category": "danger", "text": f"Import Execution Error: {e}"}]
            content = f"""
            <div class="alert alert-danger">
                <h4>Import Failed (Transaction Rolled Back)</h4>
                <p>{escape_html(str(e))}</p>
                <p style="font-size: 0.85rem; margin-top: 0.5rem;">
                    No partial changes were written to the database.
                </p>
            </div>
            <div style="margin-top: 1.5rem;">
                <a href="/system/import?type={entity_type}" class="btn btn-outline-secondary">&larr; Return to CSV Import</a>
            </div>
            """
            return Response.html(render_page("Import Failed", content, user=user, flash_messages=flash, active_nav="audit"))

    # =========================================================================
    # 3. Domain Export Handlers (CSV & JSON Downloads)
    # =========================================================================

    def _serve_export(filename: str, content: str, content_type: str = "text/csv") -> Response:
        headers = {
            "Content-Type": f"{content_type}; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        }
        return Response(body=content, status_code=200, content_type=content_type, headers=headers)

    @router.get("/members/export")
    @require_permission(PERM_SYSTEM_EXPORT)
    def export_members_handler(req: Request) -> Response:
        fmt = (req.query("format") or "csv").lower()
        if fmt == "json":
            return _serve_export("forgedesk_members.json", export_members_json(), "application/json")
        return _serve_export("forgedesk_members.csv", export_members_csv(), "text/csv")

    @router.get("/machines/export")
    @require_permission(PERM_SYSTEM_EXPORT)
    def export_machines_handler(req: Request) -> Response:
        fmt = (req.query("format") or "csv").lower()
        if fmt == "json":
            return _serve_export("forgedesk_machines.json", export_machines_json(), "application/json")
        return _serve_export("forgedesk_machines.csv", export_machines_csv(), "text/csv")

    @router.get("/reservations/export")
    @require_permission(PERM_SYSTEM_EXPORT)
    def export_reservations_handler(req: Request) -> Response:
        fmt = (req.query("format") or "csv").lower()
        if fmt == "json":
            return _serve_export("forgedesk_reservations.json", export_reservations_json(), "application/json")
        return _serve_export("forgedesk_reservations.csv", export_reservations_csv(), "text/csv")

    @router.get("/inventory/export")
    @require_permission(PERM_SYSTEM_EXPORT)
    def export_inventory_handler(req: Request) -> Response:
        fmt = (req.query("format") or "csv").lower()
        if fmt == "json":
            return _serve_export("forgedesk_inventory.json", export_inventory_json(), "application/json")
        return _serve_export("forgedesk_inventory.csv", export_inventory_csv(), "text/csv")

    @router.get("/inventory/ledger/export")
    @require_permission(PERM_SYSTEM_EXPORT)
    def export_inventory_ledger_handler(req: Request) -> Response:
        return _serve_export("forgedesk_inventory_ledger.csv", export_inventory_ledger_csv(), "text/csv")

    @router.get("/charges/export")
    @require_permission(PERM_SYSTEM_EXPORT)
    def export_charges_handler(req: Request) -> Response:
        fmt = (req.query("format") or "csv").lower()
        if fmt == "json":
            return _serve_export("forgedesk_charges.json", export_charges_json(), "application/json")
        return _serve_export("forgedesk_charges.csv", export_charges_csv(), "text/csv")

    @router.get("/system/export/all")
    @require_permission(PERM_SYSTEM_EXPORT)
    def export_all_database_handler(req: Request) -> Response:
        timestamp_str = now_rome().strftime("%Y%m%d_%H%M%S")
        return _serve_export(f"forgedesk_full_dump_{timestamp_str}.json", export_full_system_json(), "application/json")

    # =========================================================================
    # 4. Backup & Disaster Recovery UI & Handlers
    # =========================================================================

    @router.get("/system/backup")
    @require_permission(PERM_SYSTEM_BACKUP)
    def system_backup_view(req: Request) -> Response:
        user = req.user
        can_restore = has_permission(user, PERM_SYSTEM_RESTORE)

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_files = sorted(BACKUP_DIR.glob("forgedesk_backup_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)

        backup_rows = []
        for b in backup_files:
            size_mb = b.stat().st_size / (1024 * 1024)
            mtime = datetime.datetime.fromtimestamp(b.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            restore_btn = ""
            if can_restore:
                restore_btn = f"""
                <form action="/system/restore" method="POST" style="display:inline;" onsubmit="return confirm('WARNING: Restoring will overwrite the current database. Proceed?');">
                    {csrf_input(req.cookies.get("fd_csrf", ""))}
                    <input type="hidden" name="backup_filename" value="{escape_html(b.name)}">
                    <button type="submit" class="btn btn-sm btn-outline-danger">Restore</button>
                </form>
                """

            backup_rows.append(f"""
            <tr>
                <td><strong><code>{escape_html(b.name)}</code></strong></td>
                <td>{size_mb:.2f} MB</td>
                <td>{mtime}</td>
                <td style="text-align: right; display: flex; gap: 0.4rem; justify-content: flex-end;">
                    <a href="/system/backup/download/{quote(b.name)}" class="btn btn-sm btn-outline-primary" download>Download</a>
                    {restore_btn}
                </td>
            </tr>
            """)

        backup_table_html = "\n".join(backup_rows) if backup_rows else '<tr><td colspan="4" style="text-align: center; color: #64748b; padding: 2rem;">No local backup files created yet.</td></tr>'

        restore_section = ""
        if can_restore:
            restore_section = f"""
            <div class="card" style="border-left: 4px solid #ef4444; margin-top: 2rem;">
                <div class="card-header">
                    <h3 class="card-title" style="color: #b91c1c;">⚠️ Restore Database from Uploaded Backup</h3>
                </div>
                <div class="card-body">
                    <p style="font-size: 0.85rem; color: #64748b; margin-bottom: 1.25rem;">
                        Restoring replaces the live SQLite database with the selected backup snapshot. A safety snapshot of the active database is automatically archived before restoring.
                    </p>
                    <form action="/system/restore" method="POST" enctype="multipart/form-data">
                        {csrf_input(req.cookies.get("fd_csrf", ""))}
                        <div class="form-group" style="margin-bottom: 1rem;">
                            <label class="form-label" style="font-weight: 600;">Select SQLite Backup File (.db)</label>
                            <input type="file" name="backup_file" accept=".db,.sqlite,.sqlite3" class="form-control" required style="padding: 0.5rem;">
                        </div>
                        <div class="form-group" style="margin-bottom: 1.25rem;">
                            <label style="display: flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; color: #991b1b;">
                                <input type="checkbox" name="confirm_restore" value="yes" required>
                                I understand that this operation will replace the active database.
                            </label>
                        </div>
                        <button type="submit" class="btn btn-danger">🚨 Verify &amp; Execute Restore</button>
                    </form>
                </div>
            </div>
            """

        content = f"""
        <div class="page-header" style="margin-bottom: 1.5rem;">
            <div class="header-content">
                <h1 class="page-title">Backup &amp; Disaster Recovery</h1>
                <p class="page-subtitle">Perform live online SQLite backups with zero downtime or restore from verified backup snapshots.</p>
            </div>
            <div class="header-actions">
                <a href="/system/data" class="btn btn-outline-secondary">&larr; Back to Data Hub</a>
            </div>
        </div>

        <div class="card" style="margin-bottom: 2rem; border-left: 4px solid #3b82f6;">
            <div class="card-body" style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h3 style="margin: 0 0 0.35rem 0; font-size: 1.15rem;">Live Online SQLite Database Backup</h3>
                    <p style="margin: 0; font-size: 0.85rem; color: #64748b;">
                        Uses SQLite's online backup API to stream an exact, uncorrupted copy of the active database directly to disk or download.
                    </p>
                </div>
                <a href="/system/backup/create" class="btn btn-primary btn-lg">💾 Create &amp; Download Live Backup</a>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <h3 class="card-title">Local Backup Snapshots ({len(backup_files)})</h3>
            </div>
            <div class="table-responsive">
                <table class="table" style="width: 100%; border-collapse: collapse;">
                    <thead>
                        <tr style="background-color: #f8fafc; border-bottom: 2px solid #e2e8f0;">
                            <th>Filename</th>
                            <th>Size</th>
                            <th>Created Timestamp</th>
                            <th style="text-align: right;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {backup_table_html}
                    </tbody>
                </table>
            </div>
        </div>

        {restore_section}
        """
        return Response.html(render_page("Backup & Restore", content, user=user, active_nav="audit"))

    @router.get("/system/backup/create")
    @require_permission(PERM_SYSTEM_BACKUP)
    def system_backup_create_handler(req: Request) -> Response:
        user = req.user
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            backup_path = create_database_backup(actor_user=user, ip_address=client_ip)
            data = backup_path.read_bytes()
            headers = {
                "Content-Type": "application/x-sqlite3",
                "Content-Disposition": f'attachment; filename="{backup_path.name}"',
                "Content-Length": str(len(data)),
            }
            return Response(body=data, status_code=200, content_type="application/x-sqlite3", headers=headers)
        except Exception as e:
            logger.error("Database backup creation failed: %s", e)
            return Response.html(f"<h1>Backup Creation Failed: {escape_html(str(e))}</h1>", status_code=500)

    @router.get("/system/backup/download/{filename}")
    @require_permission(PERM_SYSTEM_BACKUP)
    def system_backup_download_file(req: Request) -> Response:
        filename = unquote(req.route_params.get("filename", ""))
        safe_filename = Path(filename).name
        target = BACKUP_DIR / safe_filename

        if not target.is_file():
            return Response.html("<h1>Backup file not found</h1>", status_code=404)

        return Response.file(target, content_type="application/x-sqlite3", download_filename=safe_filename)

    @router.post("/system/restore")
    @require_permission(PERM_SYSTEM_RESTORE)
    def system_restore_handler(req: Request) -> Response:
        user = req.user
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        # Check local filename restore
        local_filename = req.form("backup_filename")
        if local_filename:
            target_path = BACKUP_DIR / Path(local_filename).name
            if not target_path.is_file():
                return Response.html("<h1>Selected backup file does not exist</h1>", status_code=404)
        else:
            # Check uploaded file
            uploaded = req.file("backup_file")
            if not uploaded or not uploaded.data:
                flash = [{"category": "danger", "text": "No backup file was uploaded."}]
                return Response.redirect("/system/backup")

            timestamp_str = now_rome().strftime("%Y%m%d_%H%M%S")
            target_path = BACKUP_DIR / f"uploaded_restore_{timestamp_str}_{Path(uploaded.filename).name}"
            target_path.write_bytes(uploaded.data)

        try:
            result = restore_database_backup(target_path, actor_user=user, ip_address=client_ip)
            flash = [{
                "category": "success",
                "text": f"Database successfully restored from '{result['restored_from']}'! "
                        f"({result['validation']['table_count']} tables verified).",
            }]
            return Response.redirect("/system/data")
        except Exception as e:
            logger.error("Restore failed: %s", e)
            content = f"""
            <div class="alert alert-danger">
                <h4>Database Restore Rejected</h4>
                <p>{escape_html(str(e))}</p>
                <p style="font-size: 0.85rem;">
                    The active database was NOT modified.
                </p>
            </div>
            <div style="margin-top: 1.5rem;">
                <a href="/system/backup" class="btn btn-outline-secondary">&larr; Return to Backup Manager</a>
            </div>
            """
            return Response.html(render_page("Restore Failed", content, user=user, active_nav="audit"))

    # =========================================================================
    # 5. REST API Endpoints (/api/system/*)
    # =========================================================================

    @router.post("/api/system/import/preview")
    @require_permission(PERM_SYSTEM_IMPORT)
    def api_import_preview(req: Request) -> Response:
        body = req.json()
        entity_type = body.get("entity_type", "members")
        csv_text = body.get("csv_content") or body.get("csv_text") or ""
        try:
            preview = preview_csv_import(entity_type=entity_type, csv_text=csv_text, actor_user=req.user)
            return Response.json({"status": "success", "preview": preview}, status_code=200)
        except Exception as e:
            return Response.json({"status": "error", "message": str(e)}, status_code=400)

    @router.post("/api/system/import/execute")
    @require_permission(PERM_SYSTEM_IMPORT)
    def api_import_execute(req: Request) -> Response:
        body = req.json()
        entity_type = body.get("entity_type", "members")
        csv_text = body.get("csv_content") or body.get("csv_text") or ""
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            result = execute_csv_import(entity_type=entity_type, csv_text=csv_text, actor_user=req.user, ip_address=client_ip)
            return Response.json(result, status_code=200)
        except Exception as e:
            return Response.json({"status": "error", "message": str(e)}, status_code=400)

    @router.get("/api/system/backup")
    @require_permission(PERM_SYSTEM_BACKUP)
    def api_backup_create(req: Request) -> Response:
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"
        try:
            backup_path = create_database_backup(actor_user=req.user, ip_address=client_ip)
            data = backup_path.read_bytes()
            headers = {
                "Content-Type": "application/x-sqlite3",
                "Content-Disposition": f'attachment; filename="{backup_path.name}"',
                "Content-Length": str(len(data)),
            }
            return Response(body=data, status_code=200, content_type="application/x-sqlite3", headers=headers)
        except Exception as e:
            return Response.json({"status": "error", "message": str(e)}, status_code=500)
