"""HTTP Request Handlers for Machines, Operating Hours, Categories, and Maintenance Windows."""

import datetime
import html
import logging
from typing import Any, Dict, Optional

from forgedesk.audit.service import record_audit_event
from forgedesk.auth.middleware import (
    is_api_request,
    require_auth,
    require_permission,
    require_role,
)
from forgedesk.auth.permissions import (
    PERM_MACHINES_MANAGE,
    PERM_MACHINES_VIEW,
    PERM_MAINTENANCE_MANAGE,
    PERM_MAINTENANCE_VIEW,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    get_user_role,
    has_permission,
    is_admin,
    is_operator,
    is_operator_or_admin,
)
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.core.templates import csrf_input, escape_html, render_page
from forgedesk.machines.service import (
    VALID_INCIDENT_SEVERITIES,
    VALID_INCIDENT_STATUSES,
    VALID_MACHINE_STATES,
    VALID_MAINTENANCE_JOB_STATUSES,
    VALID_MAINTENANCE_PRIORITIES,
    VALID_MAINTENANCE_STATUSES,
    add_incident_attachment,
    change_incident_status,
    change_machine_state,
    change_maintenance_job_status,
    change_maintenance_window_status,
    check_machine_availability,
    create_incident,
    create_machine,
    create_maintenance_job,
    create_maintenance_window,
    delete_incident,
    delete_incident_attachment,
    delete_machine,
    delete_maintenance_job,
    delete_maintenance_window,
    get_incident_attachment_by_id,
    get_incident_by_id,
    get_machine_by_code,
    get_machine_by_id,
    get_machines_summary_stats,
    get_maintenance_job_by_id,
    get_maintenance_window_by_id,
    list_incident_attachments,
    list_incidents,
    list_machines,
    list_maintenance_jobs,
    list_maintenance_windows,
    retire_machine,
    update_incident,
    update_machine,
    update_maintenance_job,
    update_maintenance_window,
)
from forgedesk.members.service import (
    create_machine_category,
    get_machine_category_by_id,
    list_machine_categories,
)
from forgedesk.utils.datetime_tz import format_display, now_rome, now_rome_iso

logger = logging.getLogger("forgedesk.machines.handlers")


def register_machine_routes(router: Router) -> None:
    """Register all machine, category, and maintenance window routes."""

    # -------------------------------------------------------------------------
    # HTML Views: Machines List & Catalog
    # -------------------------------------------------------------------------

    @router.get("/machines")
    @require_auth
    def machines_list_view(req: Request) -> Response:
        """Render machines catalog with filtering, state indicators, and maintenance alerts."""
        user = req.user
        can_manage = has_permission(user, PERM_MACHINES_MANAGE)
        can_maint_manage = has_permission(user, PERM_MAINTENANCE_MANAGE)

        state_filter = req.query("state", "").strip().lower()
        cat_filter = req.query("category", "").strip()
        search_query = req.query("q", "").strip()

        cat_id_int: Optional[int] = None
        if cat_filter and cat_filter.isdigit():
            cat_id_int = int(cat_filter)

        machines = list_machines(
            category_id=cat_id_int,
            state=state_filter if state_filter in VALID_MACHINE_STATES else None,
            search=search_query if search_query else None,
            include_retired=True,
        )

        categories = list_machine_categories()
        stats = get_machines_summary_stats()

        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"

        # Build table rows
        rows = []
        for m in machines:
            m_id = m["id"]
            code = escape_html(m["code"])
            name = escape_html(m["name"])
            cat_name = escape_html(m.get("category_name") or "Uncategorized")
            capacity = m.get("capacity", 1)
            op_hours = f"{escape_html(m.get('operating_hours_start', '08:00'))} – {escape_html(m.get('operating_hours_end', '22:00'))}"

            # Pricing formatting
            hr_eur = f"€{m.get('hourly_rate_cents', 0) / 100:.2f}/h"
            if m.get("peak_hourly_rate_cents", 0) > 0:
                hr_eur += f' <span style="font-size: 0.8rem; color: #d97706;" title="Peak rate from {escape_html(m.get("peak_hours_start", ""))} to {escape_html(m.get("peak_hours_end", ""))}"> (Peak: €{m["peak_hourly_rate_cents"]/100:.2f}/h)</span>'

            # State badge
            st = m.get("state", "available")
            if st == "available":
                state_badge = '<span class="badge badge-success">Available</span>'
            elif st == "temporarily_unavailable":
                state_badge = '<span class="badge badge-warning">Unavailable</span>'
            elif st == "under_maintenance":
                state_badge = '<span class="badge badge-danger" style="background-color: #ea580c; color: white;">Maintenance</span>'
            elif st == "retired":
                state_badge = '<span class="badge badge-secondary" style="background-color: #64748b; color: white;">Retired</span>'
            else:
                state_badge = f'<span class="badge badge-secondary">{escape_html(st)}</span>'

            # Required qualification badge
            req_qual = m.get("required_qualification_category_name")
            if req_qual:
                qual_badge = f'<span class="badge badge-info" title="Requires certification in category">{escape_html(req_qual)}</span>'
            else:
                qual_badge = '<span style="color: #94a3b8; font-size: 0.85rem;">None (Open)</span>'

            # Maintenance badge
            maint_count = m.get("active_maintenance_windows_count", 0)
            if maint_count > 0:
                maint_badge = f'<span class="badge badge-warning" style="font-size: 0.75rem;">{maint_count} Scheduled</span>'
            else:
                maint_badge = '<span style="color: #94a3b8; font-size: 0.85rem;">—</span>'

            # Action buttons
            actions = [f'<a href="/machines/{m_id}" class="btn btn-sm btn-outline-primary">View</a>']
            if can_manage:
                actions.append(f'<a href="/machines/{m_id}/edit" class="btn btn-sm btn-outline-secondary">Edit</a>')
            if can_maint_manage and st != "retired":
                actions.append(f'<a href="/machines/{m_id}/maintenance/new" class="btn btn-sm btn-outline-warning" title="Schedule Maintenance">+ Maint</a>')

            rows.append(f"""
            <tr>
                <td><strong><a href="/machines/{m_id}" style="color: inherit; text-decoration: none;">{code}</a></strong></td>
                <td><a href="/machines/{m_id}" style="font-weight: 600; color: #1e40af; text-decoration: none;">{name}</a></td>
                <td><span class="badge badge-secondary" style="background-color: #f1f5f9; color: #334155; border: 1px solid #cbd5e1;">{cat_name}</span></td>
                <td>{state_badge}</td>
                <td><span class="badge badge-secondary">{capacity}</span></td>
                <td style="font-size: 0.85rem; font-family: var(--font-mono);">{op_hours}</td>
                <td style="font-size: 0.85rem;">{hr_eur}</td>
                <td>{qual_badge}</td>
                <td>{maint_badge}</td>
                <td style="white-space: nowrap; text-align: right;">
                    {' '.join(actions)}
                </td>
            </tr>
            """)

        table_content = "".join(rows) if rows else """
            <tr>
                <td colspan="10" style="text-align: center; padding: 2.5rem; color: #64748b;">
                    No machines found matching the selected filters.
                </td>
            </tr>
        """

        # Build Category Options for filter dropdown
        cat_options = ['<option value="">All Categories</option>']
        for c in categories:
            sel = " selected" if str(c["id"]) == cat_filter else ""
            cat_options.append(f'<option value="{c["id"]}"{sel}>{escape_html(c["name"])}</option>')

        # Build Status Options
        status_options = ['<option value="">All Operational States</option>']
        for st in VALID_MACHINE_STATES:
            sel = " selected" if st == state_filter else ""
            st_label = st.replace("_", " ").title()
            status_options.append(f'<option value="{st}"{sel}>{escape_html(st_label)}</option>')

        # Top Action Buttons
        top_actions_html = ""
        if can_manage:
            top_actions_html += '<a href="/machines/new" class="btn btn-primary">+ Add New Machine</a> '
        top_actions_html += '<a href="/machines/categories" class="btn btn-secondary">Machine Categories</a> '
        top_actions_html += '<a href="/machines/maintenance" class="btn btn-secondary">Maintenance Schedule</a>'

        flash_banner = ""
        if flash_msg:
            flash_banner = f"""
            <div class="alert alert-{flash_class} alert-dismissible" style="margin-bottom: 1.5rem;">
                <span>{escape_html(flash_msg)}</span>
                <button type="button" class="btn-close" onclick="this.parentElement.remove();">&times;</button>
            </div>
            """

        content = f"""
        {flash_banner}
        <div class="page-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; flex-wrap: wrap; gap: 1rem;">
            <div>
                <h1 style="font-size: 1.75rem; font-weight: 700; color: #0f172a; margin-bottom: 0.25rem;">Machines & Equipment Fleet</h1>
                <p style="color: #64748b; font-size: 0.95rem;">Manage makerspace machinery, operational states, capacities, operating hours, and scheduled maintenance windows.</p>
            </div>
            <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                {top_actions_html}
            </div>
        </div>

        <!-- Metric Cards -->
        <div class="grid grid-4" style="margin-bottom: 1.5rem;">
            <div class="card" style="padding: 1.25rem; border-left: 4px solid #2563eb;">
                <div style="color: #64748b; font-size: 0.85rem; font-weight: 600; text-transform: uppercase;">Total Machines</div>
                <div style="font-size: 1.85rem; font-weight: 700; color: #0f172a; margin-top: 0.25rem;">{stats['total']}</div>
            </div>
            <div class="card" style="padding: 1.25rem; border-left: 4px solid #16a34a;">
                <div style="color: #64748b; font-size: 0.85rem; font-weight: 600; text-transform: uppercase;">Available Fleet</div>
                <div style="font-size: 1.85rem; font-weight: 700; color: #16a34a; margin-top: 0.25rem;">{stats['available']}</div>
            </div>
            <div class="card" style="padding: 1.25rem; border-left: 4px solid #ea580c;">
                <div style="color: #64748b; font-size: 0.85rem; font-weight: 600; text-transform: uppercase;">Maintenance / Offline</div>
                <div style="font-size: 1.85rem; font-weight: 700; color: #ea580c; margin-top: 0.25rem;">{stats['under_maintenance'] + stats['temporarily_unavailable']}</div>
            </div>
            <div class="card" style="padding: 1.25rem; border-left: 4px solid #64748b;">
                <div style="color: #64748b; font-size: 0.85rem; font-weight: 600; text-transform: uppercase;">Retired Archive</div>
                <div style="font-size: 1.85rem; font-weight: 700; color: #64748b; margin-top: 0.25rem;">{stats['retired']}</div>
            </div>
        </div>

        <!-- Filters & Search Bar -->
        <div class="card" style="padding: 1.25rem; margin-bottom: 1.5rem;">
            <form method="GET" action="/machines" style="display: flex; gap: 1rem; align-items: center; flex-wrap: wrap;">
                <div style="flex: 2; min-width: 220px;">
                    <input type="text" name="q" class="form-control" placeholder="Search by machine code, name, location, category..." value="{escape_html(search_query)}">
                </div>
                <div style="flex: 1; min-width: 180px;">
                    <select name="category" class="form-control">
                        {''.join(cat_options)}
                    </select>
                </div>
                <div style="flex: 1; min-width: 180px;">
                    <select name="state" class="form-control">
                        {''.join(status_options)}
                    </select>
                </div>
                <div style="display: flex; gap: 0.5rem;">
                    <button type="submit" class="btn btn-primary">Filter</button>
                    <a href="/machines" class="btn btn-secondary">Reset</a>
                </div>
            </form>
        </div>

        <!-- Machines Table Card -->
        <div class="card" style="overflow: hidden;">
            <div class="table-responsive">
                <table class="table" style="margin-bottom: 0;">
                    <thead>
                        <tr>
                            <th>Code</th>
                            <th>Machine Name</th>
                            <th>Category</th>
                            <th>State</th>
                            <th>Capacity</th>
                            <th>Hours (Rome)</th>
                            <th>Rate</th>
                            <th>Req. Qualification</th>
                            <th>Maintenance</th>
                            <th style="text-align: right;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_content}
                    </tbody>
                </table>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title="Machines & Equipment",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    # -------------------------------------------------------------------------
    # HTML Views: Machine Creation
    # -------------------------------------------------------------------------

    @router.get("/machines/new")
    @require_auth
    @require_permission(PERM_MACHINES_MANAGE)
    def machine_create_view(req: Request, form_data: Optional[Dict[str, Any]] = None, error_msg: str = "") -> Response:
        """Render new machine registration form with form retention on error."""
        user = req.user
        categories = list_machine_categories()
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        fd = form_data or {}
        val_code = fd.get("code", "")
        val_name = fd.get("name", "")
        val_cat_id = str(fd.get("category_id", ""))
        val_req_qual_id = str(fd.get("required_qualification_category_id", ""))
        val_capacity = str(fd.get("capacity", "1"))
        val_state = fd.get("state", "available")
        val_location = fd.get("location", "")
        val_op_start = fd.get("operating_hours_start", "08:00")
        val_op_end = fd.get("operating_hours_end", "22:00")
        val_hr = fd.get("hourly_rate", "0.00")
        val_min = fd.get("minimum_charge", "0.00")
        val_peak = fd.get("peak_hourly_rate", "0.00")
        val_peak_start = fd.get("peak_hours_start", "17:00")
        val_peak_end = fd.get("peak_hours_end", "21:00")
        val_desc = fd.get("description", "")

        cat_options = ['<option value="">-- Select Category --</option>']
        req_qual_options = ['<option value="">-- No Qualification Required (Open Access) --</option>']
        for c in categories:
            sel_cat = " selected" if str(c["id"]) == val_cat_id else ""
            cat_options.append(f'<option value="{c["id"]}"{sel_cat}>{escape_html(c["name"])} ({escape_html(c["code"])})</option>')
            sel_qual = " selected" if str(c["id"]) == val_req_qual_id else ""
            req_qual_options.append(f'<option value="{c["id"]}"{sel_qual}>{escape_html(c["name"])} ({escape_html(c["code"])})</option>')

        state_options = []
        for st in VALID_MACHINE_STATES:
            st_label = st.replace("_", " ").title()
            sel_st = " selected" if st == val_state else ""
            state_options.append(f'<option value="{st}"{sel_st}>{escape_html(st_label)}</option>')

        err_text = error_msg or req.query("error", "")
        error_banner = f'<div class="alert alert-danger" style="margin-bottom: 1.5rem;"><strong>Creation Error:</strong> {escape_html(err_text)}</div>' if err_text else ""

        content = f"""
        <div style="max-width: 800px; margin: 0 auto;">
            <div style="margin-bottom: 1.5rem;">
                <a href="/machines" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to Machines Fleet</a>
                <h1 style="font-size: 1.75rem; font-weight: 700; color: #0f172a; margin-top: 0.5rem;">Add New Machine</h1>
                <p style="color: #64748b;">Configure hardware specifications, operating hours, qualification prerequisites, and billing rates.</p>
            </div>

            {error_banner}

            <div class="card" style="padding: 2rem;">
                <form method="POST" action="/machines/new">
                    {csrf_field}
                    
                    <div style="display: grid; grid-template-columns: 1fr 2fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="code">Machine Code *</label>
                            <input type="text" id="code" name="code" value="{escape_html(val_code)}" class="form-control" placeholder="e.g. PRUSA-MK4-02" required style="text-transform: uppercase;">
                            <small class="form-text" style="color: #64748b;">Unique uppercase identifier.</small>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="name">Machine Name *</label>
                            <input type="text" id="name" name="name" value="{escape_html(val_name)}" class="form-control" placeholder="e.g. Prusa MK4 3D Printer #2" required>
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="category_id">Equipment Category *</label>
                            <select id="category_id" name="category_id" class="form-control" required>
                                {''.join(cat_options)}
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="required_qualification_category_id">Required Qualification</label>
                            <select id="required_qualification_category_id" name="required_qualification_category_id" class="form-control">
                                {''.join(req_qual_options)}
                            </select>
                            <small class="form-text" style="color: #64748b;">Member must hold valid certification in this category.</small>
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="capacity">Concurrent Capacity *</label>
                            <input type="number" id="capacity" name="capacity" class="form-control" value="{escape_html(val_capacity)}" min="1" max="100" required>
                            <small class="form-text" style="color: #64748b;">Simultaneous user slots (usually 1).</small>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="state">Initial Operational State *</label>
                            <select id="state" name="state" class="form-control" required>
                                {''.join(state_options)}
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="location">Location / Bench</label>
                            <input type="text" id="location" name="location" value="{escape_html(val_location)}" class="form-control" placeholder="e.g. Lab 1, Bench B">
                        </div>
                    </div>

                    <h3 style="font-size: 1.1rem; font-weight: 600; color: #0f172a; margin: 1.5rem 0 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">
                        Operating Hours (Europe/Rome)
                    </h3>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="operating_hours_start">Daily Opening Time *</label>
                            <input type="text" id="operating_hours_start" name="operating_hours_start" class="form-control" value="{escape_html(val_op_start)}" placeholder="08:00" required>
                            <small class="form-text" style="color: #64748b;">24-hour format HH:MM.</small>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="operating_hours_end">Daily Closing Time *</label>
                            <input type="text" id="operating_hours_end" name="operating_hours_end" class="form-control" value="{escape_html(val_op_end)}" placeholder="22:00" required>
                            <small class="form-text" style="color: #64748b;">24-hour format HH:MM.</small>
                        </div>
                    </div>

                    <h3 style="font-size: 1.1rem; font-weight: 600; color: #0f172a; margin: 1.5rem 0 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">
                        Pricing & Usage Rates (EUR)
                    </h3>

                    <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="hourly_rate">Standard Rate (€ / hr)</label>
                            <input type="number" step="0.01" min="0" id="hourly_rate" name="hourly_rate" class="form-control" value="{escape_html(val_hr)}" placeholder="0.00">
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="minimum_charge">Minimum Charge (€)</label>
                            <input type="number" step="0.01" min="0" id="minimum_charge" name="minimum_charge" class="form-control" value="{escape_html(val_min)}" placeholder="0.00">
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="peak_hourly_rate">Peak Hourly Rate (€ / hr)</label>
                            <input type="number" step="0.01" min="0" id="peak_hourly_rate" name="peak_hourly_rate" class="form-control" value="{escape_html(val_peak)}" placeholder="0.00">
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="peak_hours_start">Peak Period Start</label>
                            <input type="text" id="peak_hours_start" name="peak_hours_start" class="form-control" value="{escape_html(val_peak_start)}" placeholder="17:00">
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="peak_hours_end">Peak Period End</label>
                            <input type="text" id="peak_hours_end" name="peak_hours_end" class="form-control" value="{escape_html(val_peak_end)}" placeholder="21:00">
                        </div>
                    </div>

                    <div class="form-group" style="margin-bottom: 1.5rem;">
                        <label class="form-label" for="description">Technical Notes & Specifications</label>
                        <textarea id="description" name="description" class="form-control" rows="3" placeholder="Add technical details, bed dimensions, tooling, or safety notices...">{escape_html(val_desc)}</textarea>
                    </div>

                    <div style="display: flex; justify-content: flex-end; gap: 0.75rem; border-top: 1px solid #e2e8f0; padding-top: 1.25rem;">
                        <a href="/machines" class="btn btn-secondary">Cancel</a>
                        <button type="submit" class="btn btn-primary">Create Machine</button>
                    </div>
                </form>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title="Add New Machine",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.post("/machines/new")
    @require_auth
    @require_permission(PERM_MACHINES_MANAGE)
    def machine_create_action(req: Request) -> Response:
        """Handle new machine form submission with form retention on validation failure."""
        user = req.user
        code = req.form("code", "").strip()
        name = req.form("name", "").strip()
        cat_id = req.form("category_id", "").strip()
        req_qual_id = req.form("required_qualification_category_id", "").strip()
        capacity = req.form("capacity", "1").strip()
        state = req.form("state", "available").strip()
        op_start = req.form("operating_hours_start", "08:00").strip()
        op_end = req.form("operating_hours_end", "22:00").strip()

        # Rates conversion (EUR float to integer cents)
        def _to_cents(val_str: str) -> int:
            if not val_str:
                return 0
            try:
                return int(round(float(val_str) * 100))
            except Exception:
                return 0

        hr_cents = _to_cents(req.form("hourly_rate", "0"))
        min_cents = _to_cents(req.form("minimum_charge", "0"))
        peak_cents = _to_cents(req.form("peak_hourly_rate", "0"))
        peak_start = req.form("peak_hours_start", "").strip() or None
        peak_end = req.form("peak_hours_end", "").strip() or None
        location = req.form("location", "").strip() or None
        description = req.form("description", "").strip() or None

        try:
            if not cat_id or not cat_id.isdigit():
                raise ValueError("Valid Equipment Category must be selected.")

            new_machine = create_machine(
                code=code,
                name=name,
                category_id=int(cat_id),
                required_qualification_category_id=int(req_qual_id) if req_qual_id and req_qual_id.isdigit() else None,
                capacity=int(capacity) if capacity.isdigit() else 1,
                state=state,
                operating_hours_start=op_start,
                operating_hours_end=op_end,
                hourly_rate_cents=hr_cents,
                minimum_charge_cents=min_cents,
                peak_hourly_rate_cents=peak_cents,
                peak_hours_start=peak_start,
                peak_hours_end=peak_end,
                location=location,
                description=description,
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/machines/{new_machine['id']}?success=Machine+{escape_html(new_machine['code'])}+created+successfully.")
        except Exception as e:
            logger.warning("Machine creation failed: %s", e)
            return machine_create_view(req, form_data=req.form(), error_msg=str(e))

    # -------------------------------------------------------------------------
    # HTML Views: Machine Details & Operational Profile
    # -------------------------------------------------------------------------

    @router.get("/machines/{id}")
    @require_auth
    def machine_detail_view(req: Request) -> Response:
        """Render detailed machine profile with specs, qualification requirements, and maintenance."""
        user = req.user
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.redirect("/machines?error=Invalid+machine+identifier.")

        machine = get_machine_by_id(int(m_id))
        if not machine:
            return Response.redirect("/machines?error=Machine+not+found.")

        can_manage = has_permission(user, PERM_MACHINES_MANAGE)
        can_maint_manage = has_permission(user, PERM_MAINTENANCE_MANAGE)

        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"

        # Maintenance windows for this machine
        maint_windows = list_maintenance_windows(machine_id=int(m_id))

        maint_rows = []
        for mw in maint_windows:
            w_id = mw["id"]
            w_title = escape_html(mw["title"])
            w_start = escape_html(format_display(mw["start_time"]))
            w_end = escape_html(format_display(mw["end_time"]))
            w_status = mw["status"]
            w_creator = escape_html(mw.get("creator_name") or mw.get("creator_username") or "System")

            if w_status == "in_progress":
                w_badge = '<span class="badge badge-danger">In Progress</span>'
            elif w_status == "scheduled":
                w_badge = '<span class="badge badge-warning">Scheduled</span>'
            elif w_status == "completed":
                w_badge = '<span class="badge badge-success">Completed</span>'
            else:
                w_badge = '<span class="badge badge-secondary">Cancelled</span>'

            # Status transition actions
            w_actions = []
            if can_maint_manage and w_status in ("scheduled", "in_progress"):
                if w_status == "scheduled":
                    w_actions.append(f"""
                    <form method="POST" action="/machines/maintenance/{w_id}/status" style="display: inline;">
                        {csrf_field}
                        <input type="hidden" name="status" value="in_progress">
                        <button type="submit" class="btn btn-sm btn-outline-warning">Start</button>
                    </form>
                    """)
                w_actions.append(f"""
                <form method="POST" action="/machines/maintenance/{w_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="completed">
                    <button type="submit" class="btn btn-sm btn-outline-success">Complete</button>
                </form>
                """)
                w_actions.append(f"""
                <form method="POST" action="/machines/maintenance/{w_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="cancelled">
                    <button type="submit" class="btn btn-sm btn-outline-danger">Cancel</button>
                </form>
                """)

            maint_rows.append(f"""
            <tr>
                <td><strong>#{w_id}</strong></td>
                <td><strong>{w_title}</strong></td>
                <td style="font-size: 0.85rem; font-family: var(--font-mono);">{w_start} &rarr; {w_end}</td>
                <td>{w_badge}</td>
                <td style="font-size: 0.85rem; color: #64748b;">{w_creator}</td>
                <td style="text-align: right; white-space: nowrap;">
                    {' '.join(w_actions)}
                </td>
            </tr>
            """)

        maint_table_html = "".join(maint_rows) if maint_rows else """
            <tr>
                <td colspan="6" style="text-align: center; padding: 2rem; color: #64748b;">
                    No maintenance windows logged for this machine.
                </td>
            </tr>
        """

        # Maintenance jobs for this machine
        maint_jobs = list_maintenance_jobs(machine_id=int(m_id))
        job_rows = []
        for job in maint_jobs:
            j_id = job["id"]
            j_title = escape_html(job["title"])
            j_prio = job.get("priority", "medium")
            if j_prio == "critical":
                prio_badge = '<span class="badge badge-danger">Critical</span>'
            elif j_prio == "high":
                prio_badge = '<span class="badge badge-warning" style="background-color: #ea580c; color: white;">High</span>'
            elif j_prio == "medium":
                prio_badge = '<span class="badge badge-info">Medium</span>'
            else:
                prio_badge = '<span class="badge badge-secondary">Low</span>'

            j_st = job.get("status", "open")
            if j_st == "in_progress":
                st_badge = '<span class="badge badge-warning">In Progress</span>'
            elif j_st == "completed":
                st_badge = '<span class="badge badge-success">Completed</span>'
            elif j_st == "cancelled":
                st_badge = '<span class="badge badge-secondary">Cancelled</span>'
            else:
                st_badge = '<span class="badge badge-primary">Open</span>'

            j_assignee = escape_html(job.get("assignee_name") or job.get("assignee_username") or "Unassigned")
            j_date = escape_html(job.get("scheduled_date") or "—")

            job_actions = [f'<a href="/maintenance-jobs/{j_id}" class="btn btn-sm btn-outline-primary">View</a>']
            if can_maint_manage and j_st in ("open", "in_progress"):
                if j_st == "open":
                    job_actions.append(f"""
                    <form method="POST" action="/maintenance-jobs/{j_id}/status" style="display: inline;">
                        {csrf_field}
                        <input type="hidden" name="status" value="in_progress">
                        <button type="submit" class="btn btn-sm btn-outline-warning">Start</button>
                    </form>
                    """)
                job_actions.append(f"""
                <form method="POST" action="/maintenance-jobs/{j_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="completed">
                    <button type="submit" class="btn btn-sm btn-outline-success">Done</button>
                </form>
                """)

            job_rows.append(f"""
            <tr>
                <td><strong>#{j_id}</strong></td>
                <td><a href="/maintenance-jobs/{j_id}" style="font-weight: 600; color: #1e40af; text-decoration: none;">{j_title}</a></td>
                <td>{prio_badge}</td>
                <td>{st_badge}</td>
                <td style="font-size: 0.85rem;">{j_assignee}</td>
                <td style="font-size: 0.85rem; font-family: var(--font-mono);">{j_date}</td>
                <td style="text-align: right; white-space: nowrap;">
                    {' '.join(job_actions)}
                </td>
            </tr>
            """)

        maint_jobs_table_html = "".join(job_rows) if job_rows else """
            <tr>
                <td colspan="7" style="text-align: center; padding: 2rem; color: #64748b;">
                    No maintenance interventions logged for this machine.
                </td>
            </tr>
        """

        # Incidents for this machine
        machine_incidents = list_incidents(machine_id=int(m_id))
        inc_rows = []
        for inc in machine_incidents:
            i_id = inc["id"]
            i_title = escape_html(inc["title"])
            i_sev = inc.get("severity", "minor")
            if i_sev == "critical":
                sev_badge = '<span class="badge badge-danger" style="background-color: #b91c1c; color: white;">Critical</span>'
            elif i_sev == "major":
                sev_badge = '<span class="badge badge-warning" style="background-color: #ea580c; color: white;">Major</span>'
            else:
                sev_badge = '<span class="badge badge-info">Minor</span>'

            i_st = inc.get("status", "reported")
            if i_st == "investigating":
                inc_st_badge = '<span class="badge badge-warning">Investigating</span>'
            elif i_st == "resolved":
                inc_st_badge = '<span class="badge badge-success">Resolved</span>'
            elif i_st == "closed":
                inc_st_badge = '<span class="badge badge-secondary">Closed</span>'
            else:
                inc_st_badge = '<span class="badge badge-danger">Reported</span>'

            oos_badge = '<span class="badge badge-danger" style="font-size: 0.75rem;">Out of Service</span>' if inc.get("takes_machine_out_of_service") else '<span style="color: #94a3b8; font-size: 0.85rem;">—</span>'
            att_count = inc.get("attachments_count", 0)
            att_badge = f'<span class="badge badge-secondary" style="font-size: 0.75rem;">📎 {att_count}</span>' if att_count > 0 else '<span style="color: #94a3b8; font-size: 0.85rem;">—</span>'
            i_reporter = escape_html(inc.get("reporter_name") or inc.get("reporter_username") or "Anonymous")
            i_created = escape_html(format_display(inc.get("created_at")))

            inc_actions = [f'<a href="/incidents/{i_id}" class="btn btn-sm btn-outline-primary">View</a>']
            if can_maint_manage and i_st in ("reported", "investigating"):
                inc_actions.append(f"""
                <form method="POST" action="/incidents/{i_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="resolved">
                    <button type="submit" class="btn btn-sm btn-outline-success">Resolve</button>
                </form>
                """)

            inc_rows.append(f"""
            <tr>
                <td><strong>#{i_id}</strong></td>
                <td><a href="/incidents/{i_id}" style="font-weight: 600; color: #1e40af; text-decoration: none;">{i_title}</a></td>
                <td>{sev_badge}</td>
                <td>{inc_st_badge}</td>
                <td>{oos_badge}</td>
                <td>{att_badge}</td>
                <td style="font-size: 0.85rem;">{i_reporter}</td>
                <td style="font-size: 0.85rem; color: #64748b;">{i_created}</td>
                <td style="text-align: right; white-space: nowrap;">
                    {' '.join(inc_actions)}
                </td>
            </tr>
            """)

        incidents_table_html = "".join(inc_rows) if inc_rows else """
            <tr>
                <td colspan="9" style="text-align: center; padding: 2rem; color: #64748b;">
                    No incident reports logged for this machine.
                </td>
            </tr>
        """

        # State badge
        st = machine.get("state", "available")
        if st == "available":
            state_badge = '<span class="badge badge-success" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Available</span>'
        elif st == "temporarily_unavailable":
            state_badge = '<span class="badge badge-warning" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Temporarily Unavailable</span>'
        elif st == "under_maintenance":
            state_badge = '<span class="badge badge-danger" style="background-color: #ea580c; color: white; font-size: 0.9rem; padding: 0.35rem 0.75rem;">Under Maintenance</span>'
        elif st == "retired":
            state_badge = '<span class="badge badge-secondary" style="background-color: #64748b; color: white; font-size: 0.9rem; padding: 0.35rem 0.75rem;">Retired from Service</span>'
        else:
            state_badge = f'<span class="badge badge-secondary">{escape_html(st)}</span>'

        # Quick State Change Form
        state_change_form_html = ""
        if can_manage:
            state_options_html = []
            for s_opt in VALID_MACHINE_STATES:
                sel = " selected" if s_opt == st else ""
                s_label = s_opt.replace("_", " ").title()
                state_options_html.append(f'<option value="{s_opt}"{sel}>{escape_html(s_label)}</option>')

            state_change_form_html = f"""
            <div class="card" style="padding: 1.5rem; margin-bottom: 1.5rem; border-top: 4px solid #f59e0b;">
                <h3 style="font-size: 1.1rem; font-weight: 600; color: #0f172a; margin-bottom: 1rem;">Change Operational State</h3>
                <form method="POST" action="/machines/{machine['id']}/state" style="display: flex; gap: 1rem; align-items: flex-end; flex-wrap: wrap;">
                    {csrf_field}
                    <div class="form-group" style="flex: 1; min-width: 180px; margin-bottom: 0;">
                        <label class="form-label" for="new_state">Operational State</label>
                        <select name="state" id="new_state" class="form-control">
                            {''.join(state_options_html)}
                        </select>
                    </div>
                    <div class="form-group" style="flex: 2; min-width: 240px; margin-bottom: 0;">
                        <label class="form-label" for="reason">Reason / Diagnostic Note</label>
                        <input type="text" name="reason" id="reason" class="form-control" placeholder="e.g. Scheduled nozzle replacement, laser tube calibration, etc.">
                    </div>
                    <button type="submit" class="btn btn-warning">Update State</button>
                </form>
            </div>
            """

        flash_banner = ""
        if flash_msg:
            flash_banner = f"""
            <div class="alert alert-{flash_class} alert-dismissible" style="margin-bottom: 1.5rem;">
                <span>{escape_html(flash_msg)}</span>
                <button type="button" class="btn-close" onclick="this.parentElement.remove();">&times;</button>
            </div>
            """

        # Required qualification box
        req_qual_name = machine.get("required_qualification_category_name")
        if req_qual_name:
            qual_notice_html = f"""
            <div style="background-color: #f0f9ff; border: 1px solid #bae6fd; border-radius: var(--radius-md); padding: 1rem; display: flex; align-items: center; gap: 0.75rem;">
                <span style="font-size: 1.5rem; color: #0284c7;">🎓</span>
                <div>
                    <div style="font-weight: 600; color: #0369a1;">Mandatory Qualification Required</div>
                    <div style="font-size: 0.9rem; color: #0c4a6e;">Members must hold an active, non-expired certification in <strong>{escape_html(req_qual_name)}</strong> to reserve or check into this machine.</div>
                </div>
            </div>
            """
        else:
            qual_notice_html = f"""
            <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: var(--radius-md); padding: 1rem; display: flex; align-items: center; gap: 0.75rem;">
                <span style="font-size: 1.5rem; color: #64748b;">🔓</span>
                <div>
                    <div style="font-weight: 600; color: #334155;">Open Access Equipment</div>
                    <div style="font-size: 0.9rem; color: #64748b;">No category-specific qualification is required to operate this machine. Standard makerspace membership applies.</div>
                </div>
            </div>
            """

        # Deletion / Retirement buttons
        admin_actions_html = ""
        action_buttons = []
        if st != "retired":
            action_buttons.append(f'<a href="/machines/{machine["id"]}/incidents/new" class="btn btn-outline-danger">+ Report Incident</a>')
            if can_maint_manage:
                action_buttons.append(f'<a href="/machines/{machine["id"]}/maintenance-jobs/new" class="btn btn-outline-warning">+ Log Maintenance</a>')
        if can_manage:
            action_buttons.append(f'<a href="/machines/{machine["id"]}/edit" class="btn btn-secondary">Edit Specs</a>')
            if st != "retired":
                action_buttons.append(f"""
                <form method="POST" action="/machines/{machine['id']}/state" style="display: inline;" onsubmit="return confirm('Are you sure you want to retire this machine? Historical reservations and records will remain preserved.');">
                    {csrf_field}
                    <input type="hidden" name="state" value="retired">
                    <input type="hidden" name="reason" value="Retire button clicked on machine profile">
                    <button type="submit" class="btn btn-outline-secondary">Retire Machine</button>
                </form>
                """)
            action_buttons.append(f"""
            <form method="POST" action="/machines/{machine['id']}/delete" style="display: inline;" onsubmit="return confirm('Are you sure you want to permanently delete this machine? Deletion is only allowed if no historical reservations or maintenance logs exist.');">
                {csrf_field}
                <button type="submit" class="btn btn-outline-danger">Delete Machine</button>
            </form>
            """)

        admin_actions_html = f'<div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">{" ".join(action_buttons)}</div>'

        content = f"""
        {flash_banner}
        <div style="margin-bottom: 1.5rem;">
            <a href="/machines" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to Machines Fleet</a>
            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 0.5rem; flex-wrap: wrap; gap: 1rem;">
                <div>
                    <div style="display: flex; align-items: center; gap: 0.75rem;">
                        <h1 style="font-size: 1.85rem; font-weight: 700; color: #0f172a; margin: 0;">{escape_html(machine['name'])}</h1>
                        {state_badge}
                    </div>
                    <p style="color: #64748b; font-family: var(--font-mono); font-size: 0.95rem; margin-top: 0.25rem;">
                        Code: <strong>{escape_html(machine['code'])}</strong> &bull; Category: {escape_html(machine.get('category_name') or 'General')} &bull; Location: {escape_html(machine.get('location') or 'Not specified')}
                    </p>
                </div>
                <div>
                    {admin_actions_html}
                </div>
            </div>
        </div>

        {state_change_form_html}

        <div class="grid grid-3" style="gap: 1.5rem; margin-bottom: 1.5rem;">
            <!-- Specs Card -->
            <div class="card" style="padding: 1.5rem;">
                <h3 style="font-size: 1.1rem; font-weight: 600; color: #0f172a; margin-bottom: 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">
                    Specifications & Capacity
                </h3>
                <div style="display: flex; flex-direction: column; gap: 0.75rem; font-size: 0.9rem;">
                    <div><span style="color: #64748b;">Machine Code:</span> <strong style="font-family: var(--font-mono);">{escape_html(machine['code'])}</strong></div>
                    <div><span style="color: #64748b;">Equipment Category:</span> <strong>{escape_html(machine.get('category_name') or '—')}</strong></div>
                    <div><span style="color: #64748b;">Concurrent Capacity:</span> <strong>{machine.get('capacity', 1)} slot(s)</strong></div>
                    <div><span style="color: #64748b;">Workshop Location:</span> <strong>{escape_html(machine.get('location') or '—')}</strong></div>
                    <div><span style="color: #64748b;">Historical Reservations:</span> <strong>{machine.get('total_reservations_count', 0)} logged</strong></div>
                </div>
            </div>

            <!-- Operating Hours Card -->
            <div class="card" style="padding: 1.5rem;">
                <h3 style="font-size: 1.1rem; font-weight: 600; color: #0f172a; margin-bottom: 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">
                    Operating Hours (Europe/Rome)
                </h3>
                <div style="display: flex; flex-direction: column; gap: 0.75rem; font-size: 0.9rem;">
                    <div><span style="color: #64748b;">Daily Operating Hours:</span> <strong style="font-family: var(--font-mono);">{escape_html(machine.get('operating_hours_start', '08:00'))} &rarr; {escape_html(machine.get('operating_hours_end', '22:00'))}</strong></div>
                    <div><span style="color: #64748b;">Peak Rate Period:</span> <strong style="font-family: var(--font-mono);">{escape_html(machine.get('peak_hours_start') or '17:00')} &rarr; {escape_html(machine.get('peak_hours_end') or '21:00')}</strong></div>
                    <div><span style="color: #64748b;">Availability Rule:</span> <span class="badge badge-success">Timezone Enforced</span></div>
                </div>
            </div>

            <!-- Pricing & Rates Card -->
            <div class="card" style="padding: 1.5rem;">
                <h3 style="font-size: 1.1rem; font-weight: 600; color: #0f172a; margin-bottom: 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">
                    Pricing & Rates
                </h3>
                <div style="display: flex; flex-direction: column; gap: 0.75rem; font-size: 0.9rem;">
                    <div><span style="color: #64748b;">Standard Hourly Rate:</span> <strong style="color: #16a34a; font-size: 1.1rem;">€{machine.get('hourly_rate_cents', 0) / 100:.2f} / hr</strong></div>
                    <div><span style="color: #64748b;">Minimum Usage Charge:</span> <strong>€{machine.get('minimum_charge_cents', 0) / 100:.2f}</strong></div>
                    <div><span style="color: #64748b;">Peak Period Rate:</span> <strong style="color: #d97706;">€{machine.get('peak_hourly_rate_cents', 0) / 100:.2f} / hr</strong></div>
                </div>
            </div>
        </div>

        <div style="margin-bottom: 1.5rem;">
            {qual_notice_html}
        </div>

        {f'<div class="card" style="padding: 1.5rem; margin-bottom: 1.5rem;"><h3 style="font-size: 1rem; font-weight: 600; color: #0f172a; margin-bottom: 0.5rem;">Description & Technical Notes</h3><p style="color: #334155; font-size: 0.95rem; white-space: pre-wrap;">{escape_html(machine.get("description") or "No description provided.")}</p></div>' if machine.get("description") else ''}

        <!-- Maintenance Jobs Card -->
        <div class="card" style="overflow: hidden; margin-bottom: 1.5rem;">
            <div style="padding: 1.25rem 1.5rem; border-bottom: 1px solid #e2e8f0; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h3 style="font-size: 1.15rem; font-weight: 700; color: #0f172a; margin: 0;">Maintenance Interventions & Jobs</h3>
                    <p style="color: #64748b; font-size: 0.85rem; margin: 0;">Assigned tasks, routine servicing, and repairs for this machine.</p>
                </div>
                {f'<a href="/machines/{machine["id"]}/maintenance-jobs/new" class="btn btn-sm btn-warning">+ Log Maintenance Job</a>' if can_maint_manage and st != "retired" else ''}
            </div>
            <div class="table-responsive">
                <table class="table" style="margin-bottom: 0;">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Job Title</th>
                            <th>Priority</th>
                            <th>Status</th>
                            <th>Assignee</th>
                            <th>Scheduled</th>
                            <th style="text-align: right;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {maint_jobs_table_html}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Incidents Card -->
        <div class="card" style="overflow: hidden; margin-bottom: 1.5rem;">
            <div style="padding: 1.25rem 1.5rem; border-bottom: 1px solid #e2e8f0; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h3 style="font-size: 1.15rem; font-weight: 700; color: #0f172a; margin: 0;">Incident Reports & Malfunctions</h3>
                    <p style="color: #64748b; font-size: 0.85rem; margin: 0;">Track malfunctions, damage reports, out-of-service alerts, and attachments.</p>
                </div>
                {f'<a href="/machines/{machine["id"]}/incidents/new" class="btn btn-sm btn-danger">+ Report Incident</a>' if st != "retired" else ''}
            </div>
            <div class="table-responsive">
                <table class="table" style="margin-bottom: 0;">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Incident Title</th>
                            <th>Severity</th>
                            <th>Status</th>
                            <th>Out of Service</th>
                            <th>Attachments</th>
                            <th>Reported By</th>
                            <th>Date</th>
                            <th style="text-align: right;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {incidents_table_html}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Maintenance Windows Card -->
        <div class="card" style="overflow: hidden; margin-bottom: 1.5rem;">
            <div style="padding: 1.25rem 1.5rem; border-bottom: 1px solid #e2e8f0; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h3 style="font-size: 1.15rem; font-weight: 700; color: #0f172a; margin: 0;">Maintenance Time Windows</h3>
                    <p style="color: #64748b; font-size: 0.85rem; margin: 0;">Scheduled maintenance blocks prevent conflicting reservations and transition machine state.</p>
                </div>
                {f'<a href="/machines/{machine["id"]}/maintenance/new" class="btn btn-sm btn-warning">+ Schedule Window</a>' if can_maint_manage and st != "retired" else ''}
            </div>
            <div class="table-responsive">
                <table class="table" style="margin-bottom: 0;">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Maintenance Title</th>
                            <th>Time Window (Europe/Rome)</th>
                            <th>Status</th>
                            <th>Created By</th>
                            <th style="text-align: right;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {maint_table_html}
                    </tbody>
                </table>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title=f"{machine['name']} ({machine['code']})",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    # -------------------------------------------------------------------------
    # HTML Views: Machine Edit
    # -------------------------------------------------------------------------

    @router.get("/machines/{id}/edit")
    @require_auth
    @require_permission(PERM_MACHINES_MANAGE)
    def machine_edit_view(req: Request, form_data: Optional[Dict[str, Any]] = None, error_msg: str = "") -> Response:
        """Render machine update form with value retention on error."""
        user = req.user
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.redirect("/machines?error=Invalid+machine+identifier.")

        machine = get_machine_by_id(int(m_id))
        if not machine:
            return Response.redirect("/machines?error=Machine+not+found.")

        categories = list_machine_categories()
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        fd = form_data or {}
        val_name = fd.get("name", machine["name"])
        val_cat_id = str(fd.get("category_id", machine["category_id"]))
        val_req_qual_id = str(fd.get("required_qualification_category_id", machine.get("required_qualification_category_id") or ""))
        val_capacity = str(fd.get("capacity", machine.get("capacity", 1)))
        val_state = fd.get("state", machine["state"])
        val_location = fd.get("location", machine.get("location") or "")
        val_op_start = fd.get("operating_hours_start", machine.get("operating_hours_start", "08:00"))
        val_op_end = fd.get("operating_hours_end", machine.get("operating_hours_end", "22:00"))
        
        default_hr_eur = f"{machine.get('hourly_rate_cents', 0) / 100:.2f}"
        default_min_eur = f"{machine.get('minimum_charge_cents', 0) / 100:.2f}"
        default_pk_eur = f"{machine.get('peak_hourly_rate_cents', 0) / 100:.2f}"
        val_hr = fd.get("hourly_rate", default_hr_eur)
        val_min = fd.get("minimum_charge", default_min_eur)
        val_peak = fd.get("peak_hourly_rate", default_pk_eur)
        val_peak_start = fd.get("peak_hours_start", machine.get("peak_hours_start") or "")
        val_peak_end = fd.get("peak_hours_end", machine.get("peak_hours_end") or "")
        val_desc = fd.get("description", machine.get("description") or "")

        cat_options = []
        req_qual_options = ['<option value="">-- No Qualification Required (Open Access) --</option>']
        for c in categories:
            sel_cat = " selected" if str(c["id"]) == val_cat_id else ""
            cat_options.append(f'<option value="{c["id"]}"{sel_cat}>{escape_html(c["name"])} ({escape_html(c["code"])})</option>')

            sel_req = " selected" if str(c["id"]) == val_req_qual_id else ""
            req_qual_options.append(f'<option value="{c["id"]}"{sel_req}>{escape_html(c["name"])} ({escape_html(c["code"])})</option>')

        state_options = []
        for st in VALID_MACHINE_STATES:
            sel_st = " selected" if st == val_state else ""
            st_label = st.replace("_", " ").title()
            state_options.append(f'<option value="{st}"{sel_st}>{escape_html(st_label)}</option>')

        err_text = error_msg or req.query("error", "")
        error_banner = f'<div class="alert alert-danger" style="margin-bottom: 1.5rem;"><strong>Update Error:</strong> {escape_html(err_text)}</div>' if err_text else ""

        content = f"""
        <div style="max-width: 800px; margin: 0 auto;">
            <div style="margin-bottom: 1.5rem;">
                <a href="/machines/{machine['id']}" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to Machine Profile</a>
                <h1 style="font-size: 1.75rem; font-weight: 700; color: #0f172a; margin-top: 0.5rem;">Edit Machine: {escape_html(machine['code'])}</h1>
                <p style="color: #64748b;">Update hardware specifications, operational parameters, and rate schedules.</p>
            </div>

            {error_banner}

            <div class="card" style="padding: 2rem;">
                <form method="POST" action="/machines/{machine['id']}/edit">
                    {csrf_field}

                    <div style="display: grid; grid-template-columns: 1fr 2fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="code">Machine Code (Immutable)</label>
                            <input type="text" id="code" class="form-control" value="{escape_html(machine['code'])}" disabled style="background-color: #f1f5f9; font-family: var(--font-mono);">
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="name">Machine Name *</label>
                            <input type="text" id="name" name="name" class="form-control" value="{escape_html(val_name)}" required>
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="category_id">Equipment Category *</label>
                            <select id="category_id" name="category_id" class="form-control" required>
                                {''.join(cat_options)}
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="required_qualification_category_id">Required Qualification</label>
                            <select id="required_qualification_category_id" name="required_qualification_category_id" class="form-control">
                                {''.join(req_qual_options)}
                            </select>
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="capacity">Concurrent Capacity *</label>
                            <input type="number" id="capacity" name="capacity" class="form-control" value="{escape_html(val_capacity)}" min="1" max="100" required>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="state">Operational State *</label>
                            <select id="state" name="state" class="form-control" required>
                                {''.join(state_options)}
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="location">Location / Bench</label>
                            <input type="text" id="location" name="location" class="form-control" value="{escape_html(val_location)}">
                        </div>
                    </div>

                    <h3 style="font-size: 1.1rem; font-weight: 600; color: #0f172a; margin: 1.5rem 0 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">
                        Operating Hours (Europe/Rome)
                    </h3>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="operating_hours_start">Daily Opening Time *</label>
                            <input type="text" id="operating_hours_start" name="operating_hours_start" class="form-control" value="{escape_html(val_op_start)}" required>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="operating_hours_end">Daily Closing Time *</label>
                            <input type="text" id="operating_hours_end" name="operating_hours_end" class="form-control" value="{escape_html(val_op_end)}" required>
                        </div>
                    </div>

                    <h3 style="font-size: 1.1rem; font-weight: 600; color: #0f172a; margin: 1.5rem 0 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">
                        Pricing & Usage Rates (EUR)
                    </h3>

                    <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="hourly_rate">Standard Rate (€ / hr)</label>
                            <input type="number" step="0.01" min="0" id="hourly_rate" name="hourly_rate" class="form-control" value="{escape_html(val_hr)}">
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="minimum_charge">Minimum Charge (€)</label>
                            <input type="number" step="0.01" min="0" id="minimum_charge" name="minimum_charge" class="form-control" value="{escape_html(val_min)}">
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="peak_hourly_rate">Peak Hourly Rate (€ / hr)</label>
                            <input type="number" step="0.01" min="0" id="peak_hourly_rate" name="peak_hourly_rate" class="form-control" value="{escape_html(val_peak)}">
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="peak_hours_start">Peak Period Start</label>
                            <input type="text" id="peak_hours_start" name="peak_hours_start" class="form-control" value="{escape_html(val_peak_start)}">
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="peak_hours_end">Peak Period End</label>
                            <input type="text" id="peak_hours_end" name="peak_hours_end" class="form-control" value="{escape_html(val_peak_end)}">
                        </div>
                    </div>

                    <div class="form-group" style="margin-bottom: 1.5rem;">
                        <label class="form-label" for="description">Technical Notes & Specifications</label>
                        <textarea id="description" name="description" class="form-control" rows="3">{escape_html(val_desc)}</textarea>
                    </div>

                    <div style="display: flex; justify-content: flex-end; gap: 0.75rem; border-top: 1px solid #e2e8f0; padding-top: 1.25rem;">
                        <a href="/machines/{machine['id']}" class="btn btn-secondary">Cancel</a>
                        <button type="submit" class="btn btn-primary">Save Changes</button>
                    </div>
                </form>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title=f"Edit {machine['name']}",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.post("/machines/{id}/edit")
    @require_auth
    @require_permission(PERM_MACHINES_MANAGE)
    def machine_edit_action(req: Request) -> Response:
        """Handle machine update form submission with value retention on error."""
        user = req.user
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.redirect("/machines?error=Invalid+machine+identifier.")

        name = req.form("name", "").strip()
        cat_id = req.form("category_id", "").strip()
        req_qual_id = req.form("required_qualification_category_id", "").strip()
        capacity = req.form("capacity", "").strip()
        state = req.form("state", "").strip()
        op_start = req.form("operating_hours_start", "").strip()
        op_end = req.form("operating_hours_end", "").strip()

        def _to_cents(val_str: str) -> Optional[int]:
            if not val_str:
                return None
            try:
                return int(round(float(val_str) * 100))
            except Exception:
                return None

        hr_cents = _to_cents(req.form("hourly_rate", ""))
        min_cents = _to_cents(req.form("minimum_charge", ""))
        peak_cents = _to_cents(req.form("peak_hourly_rate", ""))
        peak_start = req.form("peak_hours_start", "").strip()
        peak_end = req.form("peak_hours_end", "").strip()
        location = req.form("location", "").strip()
        description = req.form("description", "").strip()

        try:
            update_machine(
                machine_id=int(m_id),
                name=name if name else None,
                category_id=int(cat_id) if cat_id.isdigit() else None,
                required_qualification_category_id=int(req_qual_id) if req_qual_id.isdigit() else None,
                capacity=int(capacity) if capacity.isdigit() else None,
                state=state if state else None,
                operating_hours_start=op_start if op_start else None,
                operating_hours_end=op_end if op_end else None,
                hourly_rate_cents=hr_cents,
                minimum_charge_cents=min_cents,
                peak_hourly_rate_cents=peak_cents,
                peak_hours_start=peak_start,
                peak_hours_end=peak_end,
                location=location,
                description=description,
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/machines/{m_id}?success=Machine+specifications+updated+successfully.")
        except Exception as e:
            logger.warning("Machine update failed: %s", e)
            return machine_edit_view(req, form_data=req.form(), error_msg=str(e))

    # -------------------------------------------------------------------------
    # HTML Views: Machine State & Deletion Actions
    # -------------------------------------------------------------------------

    @router.post("/machines/{id}/state")
    @require_auth
    @require_permission(PERM_MACHINES_MANAGE)
    def machine_change_state_action(req: Request) -> Response:
        """Handle state change request."""
        user = req.user
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.redirect("/machines?error=Invalid+machine+identifier.")

        new_state = req.form("state", "").strip()
        reason = req.form("reason", "").strip() or None

        try:
            change_machine_state(
                machine_id=int(m_id),
                new_state=new_state,
                reason=reason,
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/machines/{m_id}?success=Operational+state+changed+to+{escape_html(new_state)}.")
        except Exception as e:
            logger.warning("Machine state change failed: %s", e)
            return Response.redirect(f"/machines/{m_id}?error={escape_html(str(e))}")

    @router.post("/machines/{id}/delete")
    @require_auth
    @require_permission(PERM_MACHINES_MANAGE)
    def machine_delete_action(req: Request) -> Response:
        """Handle machine deletion request."""
        user = req.user
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.redirect("/machines?error=Invalid+machine+identifier.")

        try:
            delete_machine(
                machine_id=int(m_id),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect("/machines?success=Machine+deleted+successfully.")
        except Exception as e:
            logger.warning("Machine deletion failed: %s", e)
            return Response.redirect(f"/machines/{m_id}?error={escape_html(str(e))}")

    # -------------------------------------------------------------------------
    # HTML Views: Machine Categories Management
    # -------------------------------------------------------------------------

    @router.get("/machines/categories")
    @require_auth
    def machine_categories_view(req: Request) -> Response:
        """Render machine categories list and creation form."""
        user = req.user
        can_manage = has_permission(user, PERM_MACHINES_MANAGE)
        categories = list_machine_categories()

        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"

        rows = []
        for c in categories:
            c_code = escape_html(c["code"])
            c_name = escape_html(c["name"])
            c_desc = escape_html(c.get("description") or "—")
            m_count = c.get("machine_count", 0)
            q_count = c.get("qualification_count", 0)
            created_at = escape_html(format_display(c.get("created_at")))

            rows.append(f"""
            <tr>
                <td><strong style="font-family: var(--font-mono); color: #1e40af;">{c_code}</strong></td>
                <td><strong>{c_name}</strong></td>
                <td style="color: #475569; font-size: 0.9rem;">{c_desc}</td>
                <td><span class="badge badge-secondary">{m_count} machine(s)</span></td>
                <td><span class="badge badge-info">{q_count} member(s)</span></td>
                <td style="font-size: 0.85rem; color: #64748b;">{created_at}</td>
            </tr>
            """)

        table_rows = "".join(rows) if rows else """
            <tr>
                <td colspan="6" style="text-align: center; padding: 2rem; color: #64748b;">
                    No equipment categories registered.
                </td>
            </tr>
        """

        create_card = ""
        if can_manage:
            create_card = f"""
            <div class="card" style="padding: 1.5rem; margin-bottom: 1.5rem; border-top: 4px solid #2563eb;">
                <h3 style="font-size: 1.15rem; font-weight: 600; color: #0f172a; margin-bottom: 1rem;">Create Equipment Category</h3>
                <form method="POST" action="/machines/categories/new">
                    {csrf_field}
                    <div style="display: grid; grid-template-columns: 1fr 2fr; gap: 1rem; margin-bottom: 1rem;">
                        <div class="form-group" style="margin-bottom: 0;">
                            <label class="form-label" for="code">Category Code *</label>
                            <input type="text" id="code" name="code" class="form-control" placeholder="e.g. waterjet_cutters" required>
                            <small class="form-text" style="color: #64748b;">Lowercase slug (letters, numbers, underscores).</small>
                        </div>
                        <div class="form-group" style="margin-bottom: 0;">
                            <label class="form-label" for="name">Category Name *</label>
                            <input type="text" id="name" name="name" class="form-control" placeholder="e.g. Abrasive Waterjet Cutters" required>
                        </div>
                    </div>
                    <div class="form-group" style="margin-bottom: 1rem;">
                        <label class="form-label" for="description">Description & Scope</label>
                        <input type="text" id="description" name="description" class="form-control" placeholder="Category scope, tooling requirements, safety certifications...">
                    </div>
                    <button type="submit" class="btn btn-primary">+ Create Category</button>
                </form>
            </div>
            """

        flash_banner = f'<div class="alert alert-{flash_class}" style="margin-bottom: 1.5rem;">{escape_html(flash_msg)}</div>' if flash_msg else ""

        content = f"""
        {flash_banner}
        <div style="margin-bottom: 1.5rem;">
            <a href="/machines" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to Machines Fleet</a>
            <h1 style="font-size: 1.75rem; font-weight: 700; color: #0f172a; margin-top: 0.5rem;">Equipment & Machine Categories</h1>
            <p style="color: #64748b;">Categories organize machines and link to specific user safety qualification certificates.</p>
        </div>

        {create_card}

        <div class="card" style="overflow: hidden;">
            <div class="table-responsive">
                <table class="table" style="margin-bottom: 0;">
                    <thead>
                        <tr>
                            <th>Code</th>
                            <th>Category Name</th>
                            <th>Description</th>
                            <th>Assigned Machines</th>
                            <th>Qualified Members</th>
                            <th>Created</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_rows}
                    </tbody>
                </table>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title="Machine Categories",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.post("/machines/categories/new")
    @require_auth
    @require_permission(PERM_MACHINES_MANAGE)
    def machine_category_create_action(req: Request) -> Response:
        """Handle new category creation."""
        user = req.user
        code = req.form("code", "").strip()
        name = req.form("name", "").strip()
        desc = req.form("description", "").strip() or None

        try:
            create_machine_category(
                code=code,
                name=name,
                description=desc,
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect("/machines/categories?success=Category+created+successfully.")
        except Exception as e:
            logger.warning("Category creation failed: %s", e)
            return Response.redirect(f"/machines/categories?error={escape_html(str(e))}")

    # -------------------------------------------------------------------------
    # HTML Views: Maintenance Schedules & Windows
    # -------------------------------------------------------------------------

    @router.get("/machines/maintenance")
    @require_auth
    def maintenance_schedule_view(req: Request) -> Response:
        """Render all maintenance windows across the fleet."""
        user = req.user
        can_maint_manage = has_permission(user, PERM_MAINTENANCE_MANAGE)

        status_filter = req.query("status", "").strip().lower()
        windows = list_maintenance_windows(
            status=status_filter if status_filter in VALID_MAINTENANCE_STATUSES else None,
        )

        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"

        rows = []
        for mw in windows:
            w_id = mw["id"]
            m_id = mw["machine_id"]
            m_code = escape_html(mw["machine_code"])
            m_name = escape_html(mw["machine_name"])
            w_title = escape_html(mw["title"])
            w_start = escape_html(format_display(mw["start_time"]))
            w_end = escape_html(format_display(mw["end_time"]))
            w_status = mw["status"]
            w_creator = escape_html(mw.get("creator_name") or mw.get("creator_username") or "System")

            if w_status == "in_progress":
                w_badge = '<span class="badge badge-danger">In Progress</span>'
            elif w_status == "scheduled":
                w_badge = '<span class="badge badge-warning">Scheduled</span>'
            elif w_status == "completed":
                w_badge = '<span class="badge badge-success">Completed</span>'
            else:
                w_badge = '<span class="badge badge-secondary">Cancelled</span>'

            w_actions = []
            if can_maint_manage and w_status in ("scheduled", "in_progress"):
                if w_status == "scheduled":
                    w_actions.append(f"""
                    <form method="POST" action="/machines/maintenance/{w_id}/status" style="display: inline;">
                        {csrf_field}
                        <input type="hidden" name="status" value="in_progress">
                        <button type="submit" class="btn btn-sm btn-outline-warning">Start</button>
                    </form>
                    """)
                w_actions.append(f"""
                <form method="POST" action="/machines/maintenance/{w_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="completed">
                    <button type="submit" class="btn btn-sm btn-outline-success">Complete</button>
                </form>
                """)
                w_actions.append(f"""
                <form method="POST" action="/machines/maintenance/{w_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="cancelled">
                    <button type="submit" class="btn btn-sm btn-outline-danger">Cancel</button>
                </form>
                """)

            rows.append(f"""
            <tr>
                <td><strong>#{w_id}</strong></td>
                <td><a href="/machines/{m_id}" style="font-weight: 600; color: #1e40af; text-decoration: none;">{m_name}</a> <span style="font-family: var(--font-mono); color: #64748b; font-size: 0.85rem;">({m_code})</span></td>
                <td><strong>{w_title}</strong></td>
                <td style="font-size: 0.85rem; font-family: var(--font-mono);">{w_start} &rarr; {w_end}</td>
                <td>{w_badge}</td>
                <td style="font-size: 0.85rem; color: #64748b;">{w_creator}</td>
                <td style="text-align: right; white-space: nowrap;">
                    {' '.join(w_actions)}
                </td>
            </tr>
            """)

        table_rows = "".join(rows) if rows else """
            <tr>
                <td colspan="7" style="text-align: center; padding: 2.5rem; color: #64748b;">
                    No maintenance windows found for the selected filter.
                </td>
            </tr>
        """

        flash_banner = f'<div class="alert alert-{flash_class}" style="margin-bottom: 1.5rem;">{escape_html(flash_msg)}</div>' if flash_msg else ""

        content = f"""
        {flash_banner}
        <div style="margin-bottom: 1.5rem;">
            <a href="/machines" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to Machines Fleet</a>
            <h1 style="font-size: 1.75rem; font-weight: 700; color: #0f172a; margin-top: 0.5rem;">Fleet Maintenance Schedule</h1>
            <p style="color: #64748b;">Track scheduled maintenance intervals, routine calibrations, and active repairs across all equipment.</p>
        </div>

        <div class="card" style="overflow: hidden;">
            <div class="table-responsive">
                <table class="table" style="margin-bottom: 0;">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Machine</th>
                            <th>Maintenance Window</th>
                            <th>Time Period (Europe/Rome)</th>
                            <th>Status</th>
                            <th>Created By</th>
                            <th style="text-align: right;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_rows}
                    </tbody>
                </table>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title="Maintenance Schedule",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.get("/machines/{id}/maintenance/new")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def maintenance_window_create_view(req: Request) -> Response:
        """Render form to schedule a maintenance window for a machine."""
        user = req.user
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.redirect("/machines?error=Invalid+machine+identifier.")

        machine = get_machine_by_id(int(m_id))
        if not machine:
            return Response.redirect("/machines?error=Machine+not+found.")

        if machine["state"] == "retired":
            return Response.redirect(f"/machines/{m_id}?error=Cannot+schedule+maintenance+on+retired+machine.")

        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        now = now_rome()
        default_start = (now + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
        default_end = (now + datetime.timedelta(hours=4)).strftime("%Y-%m-%dT%H:00")

        error_msg = req.query("error")
        error_banner = f'<div class="alert alert-danger" style="margin-bottom: 1.5rem;">{escape_html(error_msg)}</div>' if error_msg else ""

        content = f"""
        <div style="max-width: 700px; margin: 0 auto;">
            <div style="margin-bottom: 1.5rem;">
                <a href="/machines/{machine['id']}" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to {escape_html(machine['code'])}</a>
                <h1 style="font-size: 1.75rem; font-weight: 700; color: #0f172a; margin-top: 0.5rem;">Schedule Maintenance Window</h1>
                <p style="color: #64748b;">Machine: <strong>{escape_html(machine['name'])} ({escape_html(machine['code'])})</strong></p>
            </div>

            {error_banner}

            <div class="card" style="padding: 2rem;">
                <form method="POST" action="/machines/{machine['id']}/maintenance/new">
                    {csrf_field}

                    <div class="form-group" style="margin-bottom: 1.25rem;">
                        <label class="form-label" for="title">Maintenance Title / Task *</label>
                        <input type="text" id="title" name="title" class="form-control" placeholder="e.g. Spindle Lubrication & Bed Leveling" required>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
                        <div class="form-group">
                            <label class="form-label" for="start_time">Start Time (Europe/Rome) *</label>
                            <input type="datetime-local" id="start_time" name="start_time" class="form-control" value="{default_start}" required>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="end_time">End Time (Europe/Rome) *</label>
                            <input type="datetime-local" id="end_time" name="end_time" class="form-control" value="{default_end}" required>
                        </div>
                    </div>

                    <div class="form-group" style="margin-bottom: 1.25rem;">
                        <label class="form-label" for="status">Initial Window Status</label>
                        <select id="status" name="status" class="form-control">
                            <option value="scheduled" selected>Scheduled</option>
                            <option value="in_progress">In Progress (Sets machine state to Under Maintenance)</option>
                        </select>
                    </div>

                    <div class="form-group" style="margin-bottom: 1.5rem;">
                        <label class="form-label" for="notes">Maintenance Procedures & Notes</label>
                        <textarea id="notes" name="notes" class="form-control" rows="3" placeholder="Parts required, technicians assigned, or specific safety measures..."></textarea>
                    </div>

                    <div style="display: flex; justify-content: flex-end; gap: 0.75rem; border-top: 1px solid #e2e8f0; padding-top: 1.25rem;">
                        <a href="/machines/{machine['id']}" class="btn btn-secondary">Cancel</a>
                        <button type="submit" class="btn btn-warning">Schedule Maintenance</button>
                    </div>
                </form>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title=f"Schedule Maintenance - {machine['code']}",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.post("/machines/{id}/maintenance/new")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def maintenance_window_create_action(req: Request) -> Response:
        """Handle maintenance window creation form submission."""
        user = req.user
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.redirect("/machines?error=Invalid+machine+identifier.")

        title = req.form("title", "").strip()
        start_time = req.form("start_time", "").strip()
        end_time = req.form("end_time", "").strip()
        status = req.form("status", "scheduled").strip()
        notes = req.form("notes", "").strip() or None

        try:
            create_maintenance_window(
                machine_id=int(m_id),
                title=title,
                start_time=start_time,
                end_time=end_time,
                status=status,
                created_by_user_id=user.get("id"),
                notes=notes,
                set_machine_under_maintenance=(status == "in_progress"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/machines/{m_id}?success=Maintenance+window+scheduled+successfully.")
        except Exception as e:
            logger.warning("Maintenance window creation failed: %s", e)
            return Response.redirect(f"/machines/{m_id}/maintenance/new?error={escape_html(str(e))}")

    @router.post("/machines/maintenance/{window_id}/status")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def maintenance_window_status_action(req: Request) -> Response:
        """Handle maintenance window status update."""
        user = req.user
        w_id = req.route_params.get("window_id", "")
        if not w_id or not w_id.isdigit():
            return Response.redirect("/machines?error=Invalid+maintenance+window+identifier.")

        new_status = req.form("status", "").strip()
        notes = req.form("notes", "").strip() or None

        try:
            updated = change_maintenance_window_status(
                window_id=int(w_id),
                new_status=new_status,
                notes=notes,
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/machines/{updated['machine_id']}?success=Maintenance+window+status+updated+to+{escape_html(new_status)}.")
        except Exception as e:
            logger.warning("Maintenance status change failed: %s", e)
            return Response.redirect(f"/machines?error={escape_html(str(e))}")

    # -------------------------------------------------------------------------
    # REST JSON API Endpoints
    # -------------------------------------------------------------------------

    @router.get("/api/machines")
    @require_auth
    def api_list_machines(req: Request) -> Response:
        """API: List all machines with filtering."""
        cat_id = req.query("category_id")
        state = req.query("state")
        search = req.query("q")
        include_retired = req.query("include_retired", "true").lower() != "false"

        machines = list_machines(
            category_id=int(cat_id) if cat_id and cat_id.isdigit() else None,
            state=state if state in VALID_MACHINE_STATES else None,
            search=search,
            include_retired=include_retired,
        )
        return Response.json({"success": True, "machines": machines, "count": len(machines)})

    @router.post("/api/machines")
    @require_auth
    @require_permission(PERM_MACHINES_MANAGE)
    def api_create_machine(req: Request) -> Response:
        """API: Create a new machine."""
        user = req.user
        data = req.json()
        if not data:
            return Response.json({"error": "Missing JSON request body."}, status_code=400)

        try:
            machine = create_machine(
                code=data.get("code", ""),
                name=data.get("name", ""),
                category_id=data.get("category_id"),
                required_qualification_category_id=data.get("required_qualification_category_id"),
                capacity=data.get("capacity", 1),
                state=data.get("state", "available"),
                operating_hours_start=data.get("operating_hours_start", "08:00"),
                operating_hours_end=data.get("operating_hours_end", "22:00"),
                hourly_rate_cents=data.get("hourly_rate_cents", 0),
                minimum_charge_cents=data.get("minimum_charge_cents", 0),
                peak_hourly_rate_cents=data.get("peak_hourly_rate_cents", 0),
                peak_hours_start=data.get("peak_hours_start"),
                peak_hours_end=data.get("peak_hours_end"),
                location=data.get("location"),
                description=data.get("description"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "machine": machine}, status_code=201)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("API create machine error: %s", e)
            return Response.json({"error": "Internal server error creating machine."}, status_code=500)

    @router.get("/api/machines/{id}")
    @require_auth
    def api_get_machine(req: Request) -> Response:
        """API: Get machine details by ID."""
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.json({"error": "Invalid machine ID."}, status_code=400)

        machine = get_machine_by_id(int(m_id))
        if not machine:
            return Response.json({"error": "Machine not found."}, status_code=404)

        maint_windows = list_maintenance_windows(machine_id=int(m_id))
        return Response.json({"success": True, "machine": machine, "maintenance_windows": maint_windows})

    @router.put("/api/machines/{id}")
    @require_auth
    @require_permission(PERM_MACHINES_MANAGE)
    def api_update_machine(req: Request) -> Response:
        """API: Update machine specifications."""
        user = req.user
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.json({"error": "Invalid machine ID."}, status_code=400)

        data = req.json()
        if not data:
            return Response.json({"error": "Missing JSON request body."}, status_code=400)

        try:
            updated = update_machine(
                machine_id=int(m_id),
                name=data.get("name"),
                category_id=data.get("category_id"),
                required_qualification_category_id=data.get("required_qualification_category_id", "UNSET"),
                capacity=data.get("capacity"),
                state=data.get("state"),
                operating_hours_start=data.get("operating_hours_start"),
                operating_hours_end=data.get("operating_hours_end"),
                hourly_rate_cents=data.get("hourly_rate_cents"),
                minimum_charge_cents=data.get("minimum_charge_cents"),
                peak_hourly_rate_cents=data.get("peak_hourly_rate_cents"),
                peak_hours_start=data.get("peak_hours_start", "UNSET"),
                peak_hours_end=data.get("peak_hours_end", "UNSET"),
                location=data.get("location", "UNSET"),
                description=data.get("description", "UNSET"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "machine": updated})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("API update machine error: %s", e)
            return Response.json({"error": "Internal server error updating machine."}, status_code=500)

    @router.patch("/api/machines/{id}/state")
    @require_auth
    @require_permission(PERM_MACHINES_MANAGE)
    def api_patch_machine_state(req: Request) -> Response:
        """API: Change operational state of machine."""
        user = req.user
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.json({"error": "Invalid machine ID."}, status_code=400)

        data = req.json()
        if not data or "state" not in data:
            return Response.json({"error": "Missing 'state' in JSON body."}, status_code=400)

        try:
            updated = change_machine_state(
                machine_id=int(m_id),
                new_state=data["state"],
                reason=data.get("reason"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "machine": updated})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)

    @router.delete("/api/machines/{id}")
    @require_auth
    @require_permission(PERM_MACHINES_MANAGE)
    def api_delete_machine(req: Request) -> Response:
        """API: Delete a machine (only if no reservations or maintenance history exist)."""
        user = req.user
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.json({"error": "Invalid machine ID."}, status_code=400)

        try:
            delete_machine(
                machine_id=int(m_id),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "message": "Machine deleted successfully."})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)

    @router.get("/api/machines/{id}/availability")
    @require_auth
    def api_check_availability(req: Request) -> Response:
        """API: Query machine availability for a target Europe/Rome time interval."""
        m_id = req.route_params.get("id", "")
        if not m_id or not m_id.isdigit():
            return Response.json({"error": "Invalid machine ID."}, status_code=400)

        start_time = req.query("start_time")
        end_time = req.query("end_time")
        if not start_time or not end_time:
            return Response.json({"error": "Query parameters 'start_time' and 'end_time' are required."}, status_code=400)

        exclude_res_id = req.query("exclude_reservation_id")
        avail, msg, conflicts = check_machine_availability(
            machine_id=int(m_id),
            start_time=start_time,
            end_time=end_time,
            exclude_reservation_id=int(exclude_res_id) if exclude_res_id and exclude_res_id.isdigit() else None,
        )

        return Response.json({
            "success": True,
            "machine_id": int(m_id),
            "is_available": avail,
            "message": msg,
            "conflicts": conflicts,
        })

    @router.get("/api/maintenance-windows")
    @require_auth
    def api_list_maintenance_windows(req: Request) -> Response:
        """API: List all maintenance windows."""
        m_id = req.query("machine_id")
        status = req.query("status")
        upcoming = req.query("upcoming", "false").lower() == "true"

        windows = list_maintenance_windows(
            machine_id=int(m_id) if m_id and m_id.isdigit() else None,
            status=status if status in VALID_MAINTENANCE_STATUSES else None,
            upcoming_only=upcoming,
        )
        return Response.json({"success": True, "maintenance_windows": windows, "count": len(windows)})

    @router.post("/api/maintenance-windows")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def api_create_maintenance_window(req: Request) -> Response:
        """API: Create a new maintenance window."""
        user = req.user
        data = req.json()
        if not data:
            return Response.json({"error": "Missing JSON request body."}, status_code=400)

        try:
            win = create_maintenance_window(
                machine_id=int(data.get("machine_id")),
                title=data.get("title", ""),
                start_time=data.get("start_time", ""),
                end_time=data.get("end_time", ""),
                status=data.get("status", "scheduled"),
                created_by_user_id=user.get("id"),
                notes=data.get("notes"),
                set_machine_under_maintenance=bool(data.get("set_machine_under_maintenance", False)),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "maintenance_window": win}, status_code=201)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("API create maintenance window error: %s", e)
            return Response.json({"error": "Internal server error creating maintenance window."}, status_code=500)

    @router.patch("/api/maintenance-windows/{id}/status")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def api_patch_maintenance_window_status(req: Request) -> Response:
        """API: Update maintenance window status."""
        user = req.user
        w_id = req.route_params.get("id", "")
        if not w_id or not w_id.isdigit():
            return Response.json({"error": "Invalid maintenance window ID."}, status_code=400)

        data = req.json()
        if not data or "status" not in data:
            return Response.json({"error": "Missing 'status' in JSON request body."}, status_code=400)

        try:
            updated = change_maintenance_window_status(
                window_id=int(w_id),
                new_status=data["status"],
                notes=data.get("notes"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "maintenance_window": updated})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)

    @router.delete("/api/maintenance-windows/{id}")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def api_delete_maintenance_window(req: Request) -> Response:
        """API: Delete a maintenance window."""
        user = req.user
        w_id = req.route_params.get("id", "")
        if not w_id or not w_id.isdigit():
            return Response.json({"error": "Invalid maintenance window ID."}, status_code=400)

        try:
            delete_maintenance_window(
                window_id=int(w_id),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "message": "Maintenance window deleted successfully."})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)

    # =========================================================================
    # HTML Views: Maintenance Jobs
    # =========================================================================

    def _render_maintenance_jobs_list(req: Request) -> Response:
        """Render list of all maintenance jobs across the workshop fleet."""
        user = req.user
        can_manage = has_permission(user, PERM_MAINTENANCE_MANAGE)
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        status_filter = req.query("status")
        priority_filter = req.query("priority")
        machine_filter = req.query("machine_id")
        search_query = req.query("q")

        m_id_val = int(machine_filter) if (machine_filter and machine_filter.isdigit()) else None

        jobs = list_maintenance_jobs(
            machine_id=m_id_val,
            status=status_filter if status_filter in VALID_MAINTENANCE_JOB_STATUSES else None,
            priority=priority_filter if priority_filter in VALID_MAINTENANCE_PRIORITIES else None,
            search=search_query,
        )
        all_machines = list_machines()

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"
        flash_banner = ""
        if flash_msg:
            flash_banner = f"""
            <div class="alert alert-{flash_class} alert-dismissible" style="margin-bottom: 1.5rem;">
                <span>{escape_html(flash_msg)}</span>
                <button type="button" class="btn-close" onclick="this.parentElement.remove();">&times;</button>
            </div>
            """

        job_rows = []
        for job in jobs:
            j_id = job["id"]
            j_title = escape_html(job["title"])
            m_code = escape_html(job["machine_code"])
            m_name = escape_html(job["machine_name"])

            prio = job.get("priority", "medium")
            if prio == "critical":
                prio_badge = '<span class="badge badge-danger">Critical</span>'
            elif prio == "high":
                prio_badge = '<span class="badge badge-warning" style="background-color: #ea580c; color: white;">High</span>'
            elif prio == "medium":
                prio_badge = '<span class="badge badge-info">Medium</span>'
            else:
                prio_badge = '<span class="badge badge-secondary">Low</span>'

            st = job.get("status", "open")
            if st == "in_progress":
                st_badge = '<span class="badge badge-warning">In Progress</span>'
            elif st == "completed":
                st_badge = '<span class="badge badge-success">Completed</span>'
            elif st == "cancelled":
                st_badge = '<span class="badge badge-secondary">Cancelled</span>'
            else:
                st_badge = '<span class="badge badge-primary">Open</span>'

            assignee = escape_html(job.get("assignee_name") or job.get("assignee_username") or "Unassigned")
            scheduled = escape_html(job.get("scheduled_date") or "—")
            created_at = escape_html(format_display(job.get("created_at")))

            actions = [f'<a href="/maintenance-jobs/{j_id}" class="btn btn-sm btn-outline-primary">View</a>']
            if can_manage and st in ("open", "in_progress"):
                if st == "open":
                    actions.append(f"""
                    <form method="POST" action="/maintenance-jobs/{j_id}/status" style="display: inline;">
                        {csrf_field}
                        <input type="hidden" name="status" value="in_progress">
                        <button type="submit" class="btn btn-sm btn-outline-warning">Start</button>
                    </form>
                    """)
                actions.append(f"""
                <form method="POST" action="/maintenance-jobs/{j_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="completed">
                    <button type="submit" class="btn btn-sm btn-outline-success">Done</button>
                </form>
                """)

            job_rows.append(f"""
            <tr>
                <td><strong>#{j_id}</strong></td>
                <td>
                    <a href="/machines/{job['machine_id']}" style="font-weight: 600; color: #1e40af; text-decoration: none;">
                        {m_name} <span style="font-family: var(--font-mono); font-size: 0.8rem; color: #64748b;">({m_code})</span>
                    </a>
                </td>
                <td><a href="/maintenance-jobs/{j_id}" style="font-weight: 600; color: #0f172a; text-decoration: none;">{j_title}</a></td>
                <td>{prio_badge}</td>
                <td>{st_badge}</td>
                <td>{assignee}</td>
                <td style="font-size: 0.85rem; font-family: var(--font-mono);">{scheduled}</td>
                <td style="font-size: 0.85rem; color: #64748b;">{created_at}</td>
                <td style="text-align: right; white-space: nowrap;">
                    {' '.join(actions)}
                </td>
            </tr>
            """)

        table_body = "".join(job_rows) if job_rows else """
            <tr>
                <td colspan="9" style="text-align: center; padding: 3rem; color: #64748b;">
                    No maintenance jobs found matching the specified filters.
                </td>
            </tr>
        """

        # Machine filter options
        machine_options = ['<option value="">All Machines</option>']
        for m in all_machines:
            sel = " selected" if str(m["id"]) == machine_filter else ""
            machine_options.append(f'<option value="{m["id"]}"{sel}>{escape_html(m["name"])} ({escape_html(m["code"])})</option>')

        # Priority filter options
        prio_options = ['<option value="">All Priorities</option>']
        for p in VALID_MAINTENANCE_PRIORITIES:
            sel = " selected" if p == priority_filter else ""
            prio_options.append(f'<option value="{p}"{sel}>{p.capitalize()}</option>')

        # Status filter options
        st_options = ['<option value="">All Statuses</option>']
        for s in VALID_MAINTENANCE_JOB_STATUSES:
            sel = " selected" if s == status_filter else ""
            st_options.append(f'<option value="{s}"{sel}>{s.replace("_", " ").title()}</option>')

        content = f"""
        {flash_banner}
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; flex-wrap: wrap; gap: 1rem;">
            <div>
                <h1 style="font-size: 1.85rem; font-weight: 700; color: #0f172a; margin: 0;">Maintenance Interventions</h1>
                <p style="color: #64748b; margin-top: 0.25rem;">Fleet servicing tasks, repairs, scheduled inspections, and technician assignments.</p>
            </div>
            <div>
                {f'<a href="/maintenance/jobs/new" class="btn btn-warning">+ Log Maintenance Job</a>' if can_manage else ''}
            </div>
        </div>

        <!-- Filter Bar -->
        <div class="card" style="padding: 1.25rem; margin-bottom: 1.5rem;">
            <form method="GET" action="/maintenance/jobs" style="display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-end;">
                <div style="flex: 2; min-width: 200px;">
                    <label class="form-label" for="search_q">Search Jobs / Notes</label>
                    <input type="text" name="q" id="search_q" class="form-control" placeholder="Search by title, description, machine code..." value="{escape_html(search_query or '')}">
                </div>
                <div style="flex: 1; min-width: 160px;">
                    <label class="form-label" for="filter_machine">Machine</label>
                    <select name="machine_id" id="filter_machine" class="form-control">
                        {''.join(machine_options)}
                    </select>
                </div>
                <div style="flex: 1; min-width: 140px;">
                    <label class="form-label" for="filter_priority">Priority</label>
                    <select name="priority" id="filter_priority" class="form-control">
                        {''.join(prio_options)}
                    </select>
                </div>
                <div style="flex: 1; min-width: 140px;">
                    <label class="form-label" for="filter_status">Status</label>
                    <select name="status" id="filter_status" class="form-control">
                        {''.join(st_options)}
                    </select>
                </div>
                <div>
                    <button type="submit" class="btn btn-secondary">Filter</button>
                    <a href="/maintenance/jobs" class="btn btn-outline-secondary">Reset</a>
                </div>
            </form>
        </div>

        <!-- Jobs Table -->
        <div class="card" style="overflow: hidden;">
            <div class="table-responsive">
                <table class="table" style="margin-bottom: 0;">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Machine</th>
                            <th>Job Title</th>
                            <th>Priority</th>
                            <th>Status</th>
                            <th>Technician</th>
                            <th>Scheduled Date</th>
                            <th>Logged At</th>
                            <th style="text-align: right;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_body}
                    </tbody>
                </table>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title="Maintenance Jobs Fleet",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.get("/maintenance/jobs")
    @require_auth
    def maintenance_jobs_list_view(req: Request) -> Response:
        return _render_maintenance_jobs_list(req)

    @router.get("/maintenance")
    @require_auth
    def maintenance_alias_view(req: Request) -> Response:
        return _render_maintenance_jobs_list(req)

    @router.get("/machines/{id}/maintenance-jobs/new")
    @router.get("/maintenance/jobs/new")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def maintenance_job_create_view(req: Request, form_data: Optional[Dict[str, Any]] = None, error_msg: str = "") -> Response:
        """Render form to create a new maintenance job with field retention on error."""
        user = req.user
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        fd = form_data or {}
        val_machine_id = str(fd.get("machine_id", req.route_params.get("id") or req.query("machine_id") or ""))
        val_title = fd.get("title", "")
        val_priority = fd.get("priority", "medium")
        val_status = fd.get("status", "open")
        val_assignee = str(fd.get("assigned_to_user_id", ""))
        val_date = fd.get("scheduled_date", "")
        val_desc = fd.get("description", "")
        val_notes = fd.get("notes", "")

        all_machines = list_machines()
        machine_options = []
        for m in all_machines:
            if m.get("state") == "retired":
                continue
            sel = " selected" if str(m["id"]) == val_machine_id else ""
            machine_options.append(f'<option value="{m["id"]}"{sel}>{escape_html(m["name"])} ({escape_html(m["code"])})</option>')

        # Assignee options (technicians/users)
        from forgedesk.db.connection import query_all
        users = query_all("SELECT id, username, full_name, role FROM users WHERE is_active = 1 ORDER BY full_name ASC;")
        user_options = ['<option value="">-- Unassigned --</option>']
        for u in users:
            display_name = u["full_name"] or u["username"]
            sel_u = " selected" if str(u["id"]) == val_assignee else ""
            user_options.append(f'<option value="{u["id"]}"{sel_u}>{escape_html(display_name)} ({u["role"]})</option>')

        err_text = error_msg or req.query("error", "")
        error_banner = f'<div class="alert alert-danger" style="margin-bottom: 1.5rem;"><strong>Maintenance Creation Error:</strong> {escape_html(err_text)}</div>' if err_text else ""

        content = f"""
        {error_banner}
        <div style="max-width: 760px; margin: 0 auto;">
            <div style="margin-bottom: 1.5rem;">
                <a href="/maintenance/jobs" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to Maintenance Jobs</a>
                <h1 style="font-size: 1.85rem; font-weight: 700; color: #0f172a; margin-top: 0.5rem;">Log Maintenance Intervention</h1>
                <p style="color: #64748b;">Create a servicing task, inspection, calibration, or repair job for workshop equipment.</p>
            </div>

            <div class="card" style="padding: 2rem;">
                <form method="POST" action="/maintenance/jobs/new">
                    {csrf_field}

                    <div class="form-group">
                        <label class="form-label" for="machine_id">Target Equipment <span style="color: #ef4444;">*</span></label>
                        <select name="machine_id" id="machine_id" class="form-control" required>
                            {''.join(machine_options)}
                        </select>
                    </div>

                    <div class="form-group">
                        <label class="form-label" for="title">Job Title <span style="color: #ef4444;">*</span></label>
                        <input type="text" name="title" id="title" class="form-control" value="{escape_html(val_title)}" required placeholder="e.g. 500-Hour Spindle Inspection, Replace 0.4mm Nozzle">
                    </div>

                    <div class="grid grid-2" style="gap: 1rem;">
                        <div class="form-group">
                            <label class="form-label" for="priority">Priority Level</label>
                            <select name="priority" id="priority" class="form-control">
                                <option value="low" {"selected" if val_priority == 'low' else ""}>Low (Routine servicing)</option>
                                <option value="medium" {"selected" if val_priority == 'medium' else ""}>Medium (Standard task)</option>
                                <option value="high" {"selected" if val_priority == 'high' else ""}>High (Impacting production)</option>
                                <option value="critical" {"selected" if val_priority == 'critical' else ""}>Critical (Safety / Outage)</option>
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="status">Initial Status</label>
                            <select name="status" id="status" class="form-control">
                                <option value="open" {"selected" if val_status == 'open' else ""}>Open (Pending)</option>
                                <option value="in_progress" {"selected" if val_status == 'in_progress' else ""}>In Progress</option>
                                <option value="completed" {"selected" if val_status == 'completed' else ""}>Completed</option>
                            </select>
                        </div>
                    </div>

                    <div class="grid grid-2" style="gap: 1rem;">
                        <div class="form-group">
                            <label class="form-label" for="assigned_to_user_id">Assign Technician</label>
                            <select name="assigned_to_user_id" id="assigned_to_user_id" class="form-control">
                                {''.join(user_options)}
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="scheduled_date">Target / Scheduled Date</label>
                            <input type="date" name="scheduled_date" id="scheduled_date" value="{escape_html(val_date)}" class="form-control">
                        </div>
                    </div>

                    <div class="form-group">
                        <label class="form-label" for="description">Task Description & Instructions</label>
                        <textarea name="description" id="description" class="form-control" rows="4" placeholder="Detail the steps, part numbers, or symptoms requiring attention...">{escape_html(val_desc)}</textarea>
                    </div>

                    <div class="form-group">
                        <label class="form-label" for="notes">Internal Technical Notes</label>
                        <textarea name="notes" id="notes" class="form-control" rows="2" placeholder="Optional technician scratch notes or observations...">{escape_html(val_notes)}</textarea>
                    </div>

                    <div style="display: flex; justify-content: flex-end; gap: 1rem; margin-top: 1.5rem;">
                        <a href="/maintenance/jobs" class="btn btn-secondary">Cancel</a>
                        <button type="submit" class="btn btn-warning">Create Maintenance Job</button>
                    </div>
                </form>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title="New Maintenance Job",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.post("/machines/{id}/maintenance-jobs/new")
    @router.post("/maintenance/jobs/new")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def maintenance_job_create_action(req: Request) -> Response:
        """Process maintenance job creation form with field retention on error."""
        user = req.user
        machine_id_val = req.form("machine_id") or req.route_params.get("id")
        title_val = req.form("title", "").strip()
        priority_val = req.form("priority", "medium").strip()
        status_val = req.form("status", "open").strip()
        assignee_val = req.form("assigned_to_user_id")
        scheduled_date_val = req.form("scheduled_date")
        description_val = req.form("description")
        notes_val = req.form("notes")

        if not machine_id_val or not machine_id_val.isdigit():
            return maintenance_job_create_view(req, form_data=req.form(), error_msg="Please select a valid target machine.")

        if not title_val:
            return maintenance_job_create_view(req, form_data=req.form(), error_msg="Job title is required.")

        try:
            job = create_maintenance_job(
                machine_id=int(machine_id_val),
                title=title_val,
                description=description_val,
                priority=priority_val,
                status=status_val,
                assigned_to_user_id=int(assignee_val) if assignee_val and assignee_val.isdigit() else None,
                opened_by_user_id=user.get("id"),
                scheduled_date=scheduled_date_val,
                notes=notes_val,
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/maintenance-jobs/{job['id']}?success=Maintenance+job+created+successfully.")
        except Exception as e:
            logger.error("Error creating maintenance job: %s", e)
            return maintenance_job_create_view(req, form_data=req.form(), error_msg=str(e))

    @router.get("/maintenance-jobs/{id}")
    @require_auth
    def maintenance_job_detail_view(req: Request) -> Response:
        """Render detailed maintenance job view."""
        user = req.user
        j_id = req.route_params.get("id", "")
        if not j_id or not j_id.isdigit():
            return Response.redirect("/maintenance/jobs?error=Invalid+job+ID.")

        job = get_maintenance_job_by_id(int(j_id))
        if not job:
            return Response.redirect("/maintenance/jobs?error=Maintenance+job+not+found.")

        can_manage = has_permission(user, PERM_MAINTENANCE_MANAGE)
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"
        flash_banner = ""
        if flash_msg:
            flash_banner = f"""
            <div class="alert alert-{flash_class} alert-dismissible" style="margin-bottom: 1.5rem;">
                <span>{escape_html(flash_msg)}</span>
                <button type="button" class="btn-close" onclick="this.parentElement.remove();">&times;</button>
            </div>
            """

        prio = job.get("priority", "medium")
        if prio == "critical":
            prio_badge = '<span class="badge badge-danger" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Critical Priority</span>'
        elif prio == "high":
            prio_badge = '<span class="badge badge-warning" style="background-color: #ea580c; color: white; font-size: 0.9rem; padding: 0.35rem 0.75rem;">High Priority</span>'
        elif prio == "medium":
            prio_badge = '<span class="badge badge-info" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Medium Priority</span>'
        else:
            prio_badge = '<span class="badge badge-secondary" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Low Priority</span>'

        st = job.get("status", "open")
        if st == "in_progress":
            st_badge = '<span class="badge badge-warning" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">In Progress</span>'
        elif st == "completed":
            st_badge = '<span class="badge badge-success" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Completed</span>'
        elif st == "cancelled":
            st_badge = '<span class="badge badge-secondary" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Cancelled</span>'
        else:
            st_badge = '<span class="badge badge-primary" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Open</span>'

        # Action buttons
        status_buttons = []
        if can_manage:
            if st != "in_progress" and st != "completed":
                status_buttons.append(f"""
                <form method="POST" action="/maintenance-jobs/{j_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="in_progress">
                    <button type="submit" class="btn btn-warning">Start Work</button>
                </form>
                """)
            if st != "completed":
                status_buttons.append(f"""
                <form method="POST" action="/maintenance-jobs/{j_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="completed">
                    <button type="submit" class="btn btn-success">Mark Completed</button>
                </form>
                """)
            if st != "cancelled":
                status_buttons.append(f"""
                <form method="POST" action="/maintenance-jobs/{j_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="cancelled">
                    <button type="submit" class="btn btn-outline-danger">Cancel Job</button>
                </form>
                """)

        assignee_name = escape_html(job.get("assignee_name") or job.get("assignee_username") or "Unassigned")
        opener_name = escape_html(job.get("opener_name") or job.get("opener_username") or "System")
        completed_display = escape_html(format_display(job.get("completed_at"))) if job.get("completed_at") else "Not completed"

        content = f"""
        {flash_banner}
        <div style="margin-bottom: 1.5rem;">
            <a href="/maintenance/jobs" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to Maintenance Jobs</a>
            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 0.5rem; flex-wrap: wrap; gap: 1rem;">
                <div>
                    <div style="display: flex; align-items: center; gap: 0.75rem;">
                        <h1 style="font-size: 1.85rem; font-weight: 700; color: #0f172a; margin: 0;">Maintenance #{job['id']}: {escape_html(job['title'])}</h1>
                        {prio_badge}
                        {st_badge}
                    </div>
                    <p style="color: #64748b; margin-top: 0.25rem;">
                        Target Equipment: <a href="/machines/{job['machine_id']}" style="font-weight: 600; color: #1e40af; text-decoration: none;">{escape_html(job['machine_name'])} ({escape_html(job['machine_code'])})</a>
                    </p>
                </div>
                <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
                    {' '.join(status_buttons)}
                    {f'<a href="/maintenance-jobs/{j_id}/edit" class="btn btn-secondary">Edit Job</a>' if can_manage else ''}
                </div>
            </div>
        </div>

        <div class="grid grid-3" style="gap: 1.5rem; margin-bottom: 1.5rem;">
            <div class="card" style="padding: 1.5rem;">
                <h3 style="font-size: 1rem; font-weight: 600; color: #0f172a; margin-bottom: 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">Assignment & Opener</h3>
                <div style="display: flex; flex-direction: column; gap: 0.75rem; font-size: 0.9rem;">
                    <div><span style="color: #64748b;">Assigned Technician:</span> <strong>{assignee_name}</strong></div>
                    <div><span style="color: #64748b;">Opened By:</span> <strong>{opener_name}</strong></div>
                </div>
            </div>

            <div class="card" style="padding: 1.5rem;">
                <h3 style="font-size: 1rem; font-weight: 600; color: #0f172a; margin-bottom: 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">Schedule & Timestamps</h3>
                <div style="display: flex; flex-direction: column; gap: 0.75rem; font-size: 0.9rem;">
                    <div><span style="color: #64748b;">Scheduled Date:</span> <strong style="font-family: var(--font-mono);">{escape_html(job.get('scheduled_date') or 'None')}</strong></div>
                    <div><span style="color: #64748b;">Completed At:</span> <strong>{completed_display}</strong></div>
                </div>
            </div>

            <div class="card" style="padding: 1.5rem;">
                <h3 style="font-size: 1rem; font-weight: 600; color: #0f172a; margin-bottom: 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">Machine Profile</h3>
                <div style="display: flex; flex-direction: column; gap: 0.75rem; font-size: 0.9rem;">
                    <div><span style="color: #64748b;">Machine Code:</span> <strong style="font-family: var(--font-mono);">{escape_html(job['machine_code'])}</strong></div>
                    <div><span style="color: #64748b;">Current State:</span> <strong>{escape_html(job.get('machine_state', 'available'))}</strong></div>
                </div>
            </div>
        </div>

        <div class="card" style="padding: 1.5rem; margin-bottom: 1.5rem;">
            <h3 style="font-size: 1.1rem; font-weight: 600; color: #0f172a; margin-bottom: 0.75rem;">Description & Scope of Work</h3>
            <p style="color: #334155; font-size: 0.95rem; line-height: 1.6; white-space: pre-wrap;">{escape_html(job.get('description') or 'No detailed description provided.')}</p>
        </div>

        <div class="card" style="padding: 1.5rem; margin-bottom: 1.5rem;">
            <h3 style="font-size: 1.1rem; font-weight: 600; color: #0f172a; margin-bottom: 0.75rem;">Technician Activity Notes</h3>
            <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: var(--radius-md); padding: 1rem; font-family: var(--font-mono); font-size: 0.88rem; color: #1e293b; white-space: pre-wrap;">{escape_html(job.get('notes') or 'No notes recorded.')}</div>

            {f"""
            <form method="POST" action="/maintenance-jobs/{j_id}/edit" style="margin-top: 1rem; display: flex; gap: 0.75rem;">
                {csrf_field}
                <input type="text" name="notes" class="form-control" placeholder="Append quick technician note..." required>
                <button type="submit" class="btn btn-secondary">Append Note</button>
            </form>
            """ if can_manage else ''}
        </div>
        """

        return Response.html(
            render_page(
                title=f"Maintenance Job #{job['id']}",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.get("/maintenance-jobs/{id}/edit")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def maintenance_job_edit_view(req: Request) -> Response:
        """Render form to edit a maintenance job."""
        user = req.user
        j_id = req.route_params.get("id", "")
        if not j_id or not j_id.isdigit():
            return Response.redirect("/maintenance/jobs?error=Invalid+job+ID.")

        job = get_maintenance_job_by_id(int(j_id))
        if not job:
            return Response.redirect("/maintenance/jobs?error=Maintenance+job+not+found.")

        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        # Assignee options
        from forgedesk.db.connection import query_all
        users = query_all("SELECT id, username, full_name, role FROM users WHERE is_active = 1 ORDER BY full_name ASC;")
        user_options = ['<option value="">-- Unassigned --</option>']
        for u in users:
            display_name = u["full_name"] or u["username"]
            sel = " selected" if job.get("assigned_to_user_id") == u["id"] else ""
            user_options.append(f'<option value="{u["id"]}"{sel}>{escape_html(display_name)} ({u["role"]})</option>')

        # Priority options
        prio_options = []
        for p in VALID_MAINTENANCE_PRIORITIES:
            sel = " selected" if p == job.get("priority") else ""
            prio_options.append(f'<option value="{p}"{sel}>{p.capitalize()}</option>')

        # Status options
        status_options = []
        for s in VALID_MAINTENANCE_JOB_STATUSES:
            sel = " selected" if s == job.get("status") else ""
            status_options.append(f'<option value="{s}"{sel}>{s.replace("_", " ").title()}</option>')

        content = f"""
        <div style="max-width: 760px; margin: 0 auto;">
            <div style="margin-bottom: 1.5rem;">
                <a href="/maintenance-jobs/{job['id']}" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to Job #{job['id']}</a>
                <h1 style="font-size: 1.85rem; font-weight: 700; color: #0f172a; margin-top: 0.5rem;">Edit Maintenance Job #{job['id']}</h1>
                <p style="color: #64748b;">Target Equipment: <strong>{escape_html(job['machine_name'])} ({escape_html(job['machine_code'])})</strong></p>
            </div>

            <div class="card" style="padding: 2rem;">
                <form method="POST" action="/maintenance-jobs/{job['id']}/edit">
                    {csrf_field}

                    <div class="form-group">
                        <label class="form-label" for="title">Job Title <span style="color: #ef4444;">*</span></label>
                        <input type="text" name="title" id="title" class="form-control" required value="{escape_html(job['title'])}">
                    </div>

                    <div class="grid grid-2" style="gap: 1rem;">
                        <div class="form-group">
                            <label class="form-label" for="priority">Priority Level</label>
                            <select name="priority" id="priority" class="form-control">
                                {''.join(prio_options)}
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="status">Job Status</label>
                            <select name="status" id="status" class="form-control">
                                {''.join(status_options)}
                            </select>
                        </div>
                    </div>

                    <div class="grid grid-2" style="gap: 1rem;">
                        <div class="form-group">
                            <label class="form-label" for="assigned_to_user_id">Assign Technician</label>
                            <select name="assigned_to_user_id" id="assigned_to_user_id" class="form-control">
                                {''.join(user_options)}
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="scheduled_date">Scheduled Date</label>
                            <input type="date" name="scheduled_date" id="scheduled_date" class="form-control" value="{escape_html(job.get('scheduled_date') or '')}">
                        </div>
                    </div>

                    <div class="form-group">
                        <label class="form-label" for="description">Task Description</label>
                        <textarea name="description" id="description" class="form-control" rows="4">{escape_html(job.get('description') or '')}</textarea>
                    </div>

                    <div class="form-group">
                        <label class="form-label" for="notes">Technician Notes</label>
                        <textarea name="notes" id="notes" class="form-control" rows="3">{escape_html(job.get('notes') or '')}</textarea>
                    </div>

                    <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 1.5rem;">
                        <form method="POST" action="/maintenance-jobs/{job['id']}/delete" style="display: inline;" onsubmit="return confirm('Delete this maintenance job?');">
                            {csrf_field}
                            <button type="submit" class="btn btn-outline-danger">Delete Job</button>
                        </form>
                        <div style="display: flex; gap: 1rem;">
                            <a href="/maintenance-jobs/{job['id']}" class="btn btn-secondary">Cancel</a>
                            <button type="submit" class="btn btn-warning">Save Changes</button>
                        </div>
                    </div>
                </form>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title=f"Edit Maintenance Job #{job['id']}",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.post("/maintenance-jobs/{id}/edit")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def maintenance_job_edit_action(req: Request) -> Response:
        """Process maintenance job updates."""
        user = req.user
        j_id = req.route_params.get("id", "")
        if not j_id or not j_id.isdigit():
            return Response.redirect("/maintenance/jobs?error=Invalid+job+ID.")

        title_val = req.form("title")
        prio_val = req.form("priority")
        st_val = req.form("status")
        assignee_val = req.form("assigned_to_user_id", "UNSET")
        sched_val = req.form("scheduled_date", "UNSET")
        desc_val = req.form("description", "UNSET")
        notes_val = req.form("notes", "UNSET")

        try:
            update_maintenance_job(
                job_id=int(j_id),
                title=title_val if title_val else None,
                description=desc_val,
                priority=prio_val,
                status=st_val,
                assigned_to_user_id=assignee_val,
                scheduled_date=sched_val,
                notes=notes_val,
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/maintenance-jobs/{j_id}?success=Maintenance+job+updated+successfully.")
        except Exception as e:
            logger.error("Error updating maintenance job: %s", e)
            return Response.redirect(f"/maintenance-jobs/{j_id}/edit?error={escape_html(str(e))}")

    @router.post("/maintenance-jobs/{id}/status")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def maintenance_job_status_action(req: Request) -> Response:
        """Quick status change action for maintenance jobs."""
        user = req.user
        j_id = req.route_params.get("id", "")
        if not j_id or not j_id.isdigit():
            return Response.redirect("/maintenance/jobs?error=Invalid+job+ID.")

        new_status = req.form("status", "")
        notes = req.form("notes")

        try:
            change_maintenance_job_status(
                job_id=int(j_id),
                new_status=new_status,
                notes=notes,
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/maintenance-jobs/{j_id}?success=Status+updated+to+{new_status}.")
        except Exception as e:
            logger.error("Error updating job status: %s", e)
            return Response.redirect(f"/maintenance-jobs/{j_id}?error={escape_html(str(e))}")

    @router.post("/maintenance-jobs/{id}/delete")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def maintenance_job_delete_action(req: Request) -> Response:
        """Delete maintenance job action."""
        user = req.user
        j_id = req.route_params.get("id", "")
        if not j_id or not j_id.isdigit():
            return Response.redirect("/maintenance/jobs?error=Invalid+job+ID.")

        try:
            delete_maintenance_job(
                job_id=int(j_id),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect("/maintenance/jobs?success=Maintenance+job+deleted+successfully.")
        except Exception as e:
            return Response.redirect(f"/maintenance-jobs/{j_id}?error={escape_html(str(e))}")

    # =========================================================================
    # HTML Views: Incidents & Attachments
    # =========================================================================

    @router.get("/incidents")
    @require_auth
    def incidents_list_view(req: Request) -> Response:
        """Render list of incident and malfunction reports across the fleet."""
        user = req.user
        can_manage = has_permission(user, PERM_MAINTENANCE_MANAGE)
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        severity_filter = req.query("severity")
        status_filter = req.query("status")
        machine_filter = req.query("machine_id")
        search_query = req.query("q")

        m_id_val = int(machine_filter) if (machine_filter and machine_filter.isdigit()) else None

        incidents = list_incidents(
            machine_id=m_id_val,
            status=status_filter if status_filter in VALID_INCIDENT_STATUSES else None,
            severity=severity_filter if severity_filter in VALID_INCIDENT_SEVERITIES else None,
            search=search_query,
        )
        all_machines = list_machines()

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"
        flash_banner = ""
        if flash_msg:
            flash_banner = f"""
            <div class="alert alert-{flash_class} alert-dismissible" style="margin-bottom: 1.5rem;">
                <span>{escape_html(flash_msg)}</span>
                <button type="button" class="btn-close" onclick="this.parentElement.remove();">&times;</button>
            </div>
            """

        inc_rows = []
        for inc in incidents:
            i_id = inc["id"]
            i_title = escape_html(inc["title"])
            m_code = escape_html(inc["machine_code"])
            m_name = escape_html(inc["machine_name"])

            sev = inc.get("severity", "minor")
            if sev == "critical":
                sev_badge = '<span class="badge badge-danger" style="background-color: #b91c1c; color: white;">Critical</span>'
            elif sev == "major":
                sev_badge = '<span class="badge badge-warning" style="background-color: #ea580c; color: white;">Major</span>'
            else:
                sev_badge = '<span class="badge badge-info">Minor</span>'

            st = inc.get("status", "reported")
            if st == "investigating":
                st_badge = '<span class="badge badge-warning">Investigating</span>'
            elif st == "resolved":
                st_badge = '<span class="badge badge-success">Resolved</span>'
            elif st == "closed":
                st_badge = '<span class="badge badge-secondary">Closed</span>'
            else:
                st_badge = '<span class="badge badge-danger">Reported</span>'

            oos_badge = '<span class="badge badge-danger" style="font-size: 0.75rem;">Out of Service</span>' if inc.get("takes_machine_out_of_service") else '<span style="color: #94a3b8; font-size: 0.85rem;">—</span>'
            att_count = inc.get("attachments_count", 0)
            att_badge = f'<span class="badge badge-secondary" style="font-size: 0.75rem;">📎 {att_count}</span>' if att_count > 0 else '<span style="color: #94a3b8; font-size: 0.85rem;">—</span>'
            reporter = escape_html(inc.get("reporter_name") or inc.get("reporter_username") or "Anonymous")
            created_at = escape_html(format_display(inc.get("created_at")))

            actions = [f'<a href="/incidents/{i_id}" class="btn btn-sm btn-outline-primary">View</a>']
            if can_manage and st in ("reported", "investigating"):
                actions.append(f"""
                <form method="POST" action="/incidents/{i_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="resolved">
                    <button type="submit" class="btn btn-sm btn-outline-success">Resolve</button>
                </form>
                """)

            inc_rows.append(f"""
            <tr>
                <td><strong>#{i_id}</strong></td>
                <td>
                    <a href="/machines/{inc['machine_id']}" style="font-weight: 600; color: #1e40af; text-decoration: none;">
                        {m_name} <span style="font-family: var(--font-mono); font-size: 0.8rem; color: #64748b;">({m_code})</span>
                    </a>
                </td>
                <td><a href="/incidents/{i_id}" style="font-weight: 600; color: #0f172a; text-decoration: none;">{i_title}</a></td>
                <td>{sev_badge}</td>
                <td>{st_badge}</td>
                <td>{oos_badge}</td>
                <td>{att_badge}</td>
                <td>{reporter}</td>
                <td style="font-size: 0.85rem; color: #64748b;">{created_at}</td>
                <td style="text-align: right; white-space: nowrap;">
                    {' '.join(actions)}
                </td>
            </tr>
            """)

        table_body = "".join(inc_rows) if inc_rows else """
            <tr>
                <td colspan="10" style="text-align: center; padding: 3rem; color: #64748b;">
                    No incident reports found matching the specified filters.
                </td>
            </tr>
        """

        machine_options = ['<option value="">All Machines</option>']
        for m in all_machines:
            sel = " selected" if str(m["id"]) == machine_filter else ""
            machine_options.append(f'<option value="{m["id"]}"{sel}>{escape_html(m["name"])} ({escape_html(m["code"])})</option>')

        sev_options = ['<option value="">All Severities</option>']
        for s in VALID_INCIDENT_SEVERITIES:
            sel = " selected" if s == severity_filter else ""
            sev_options.append(f'<option value="{s}"{sel}>{s.capitalize()}</option>')

        st_options = ['<option value="">All Statuses</option>']
        for st_item in VALID_INCIDENT_STATUSES:
            sel = " selected" if st_item == status_filter else ""
            st_options.append(f'<option value="{st_item}"{sel}>{st_item.capitalize()}</option>')

        content = f"""
        {flash_banner}
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; flex-wrap: wrap; gap: 1rem;">
            <div>
                <h1 style="font-size: 1.85rem; font-weight: 700; color: #0f172a; margin: 0;">Incident Reports & Malfunctions</h1>
                <p style="color: #64748b; margin-top: 0.25rem;">Track equipment breakdowns, safety hazards, damage reports, and out-of-service alerts.</p>
            </div>
            <div>
                <a href="/incidents/new" class="btn btn-danger">+ Report Incident</a>
            </div>
        </div>

        <div class="card" style="padding: 1.25rem; margin-bottom: 1.5rem;">
            <form method="GET" action="/incidents" style="display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-end;">
                <div style="flex: 2; min-width: 200px;">
                    <label class="form-label" for="search_q">Search Incidents</label>
                    <input type="text" name="q" id="search_q" class="form-control" placeholder="Search by title, description, machine code..." value="{escape_html(search_query or '')}">
                </div>
                <div style="flex: 1; min-width: 160px;">
                    <label class="form-label" for="filter_machine">Machine</label>
                    <select name="machine_id" id="filter_machine" class="form-control">
                        {''.join(machine_options)}
                    </select>
                </div>
                <div style="flex: 1; min-width: 140px;">
                    <label class="form-label" for="filter_severity">Severity</label>
                    <select name="severity" id="filter_severity" class="form-control">
                        {''.join(sev_options)}
                    </select>
                </div>
                <div style="flex: 1; min-width: 140px;">
                    <label class="form-label" for="filter_status">Status</label>
                    <select name="status" id="filter_status" class="form-control">
                        {''.join(st_options)}
                    </select>
                </div>
                <div>
                    <button type="submit" class="btn btn-secondary">Filter</button>
                    <a href="/incidents" class="btn btn-outline-secondary">Reset</a>
                </div>
            </form>
        </div>

        <div class="card" style="overflow: hidden;">
            <div class="table-responsive">
                <table class="table" style="margin-bottom: 0;">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Machine</th>
                            <th>Incident Title</th>
                            <th>Severity</th>
                            <th>Status</th>
                            <th>Out of Service</th>
                            <th>Attachments</th>
                            <th>Reported By</th>
                            <th>Date</th>
                            <th style="text-align: right;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_body}
                    </tbody>
                </table>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title="Incidents & Malfunctions Fleet",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.get("/machines/{id}/incidents/new")
    @router.get("/incidents/new")
    @require_auth
    def incident_create_view(req: Request, form_data: Optional[Dict[str, Any]] = None, error_msg: str = "") -> Response:
        """Render form to report an incident or malfunction on a machine with field retention."""
        user = req.user
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        fd = form_data or {}
        val_machine_id = str(fd.get("machine_id", req.route_params.get("id") or req.query("machine_id") or ""))
        val_title = fd.get("title", "")
        val_severity = fd.get("severity", "minor")
        val_oos = bool(fd.get("takes_machine_out_of_service") in ("1", "true", "on", "yes", True))
        val_desc = fd.get("description", "")

        all_machines = list_machines()
        machine_options = []
        for m in all_machines:
            if m.get("state") == "retired":
                continue
            sel = " selected" if (val_machine_id and str(m["id"]) == str(val_machine_id)) else ""
            machine_options.append(f'<option value="{m["id"]}"{sel}>{escape_html(m["name"])} ({escape_html(m["code"])})</option>')

        err_text = error_msg or req.query("error", "")
        error_banner = f'<div class="alert alert-danger" style="margin-bottom: 1.5rem;"><strong>Incident Error:</strong> {escape_html(err_text)}</div>' if err_text else ""

        content = f"""
        {error_banner}
        <div style="max-width: 760px; margin: 0 auto;">
            <div style="margin-bottom: 1.5rem;">
                <a href="/incidents" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to Incidents</a>
                <h1 style="font-size: 1.85rem; font-weight: 700; color: #0f172a; margin-top: 0.5rem;">Report Machine Incident / Malfunction</h1>
                <p style="color: #64748b;">Report equipment breakage, safety anomalies, software crashes, or physical defects.</p>
            </div>

            <div class="card" style="padding: 2rem;">
                <form method="POST" action="/incidents/new" enctype="multipart/form-data">
                    {csrf_field}

                    <div class="form-group">
                        <label class="form-label" for="machine_id">Equipment Involved <span style="color: #ef4444;">*</span></label>
                        <select name="machine_id" id="machine_id" class="form-control" required>
                            {''.join(machine_options)}
                        </select>
                    </div>

                    <div class="form-group">
                        <label class="form-label" for="title">Incident Summary / Title <span style="color: #ef4444;">*</span></label>
                        <input type="text" name="title" id="title" class="form-control" value="{escape_html(val_title)}" required placeholder="e.g. Laser tube failing to ignite, Extruder grinding filament">
                    </div>

                    <div class="grid grid-2" style="gap: 1rem;">
                        <div class="form-group">
                            <label class="form-label" for="severity">Severity Level</label>
                            <select name="severity" id="severity" class="form-control">
                                <option value="minor" {"selected" if val_severity == 'minor' else ""}>Minor (Machine usable with caution/degraded)</option>
                                <option value="major" {"selected" if val_severity == 'major' else ""}>Major (Core function impaired)</option>
                                <option value="critical" {"selected" if val_severity == 'critical' else ""}>Critical (Complete breakdown / Safety hazard)</option>
                            </select>
                        </div>
                        <div class="form-group" style="display: flex; align-items: center; padding-top: 1.75rem;">
                            <label style="display: flex; align-items: center; gap: 0.5rem; cursor: pointer;">
                                <input type="checkbox" name="takes_machine_out_of_service" value="1" {"checked" if val_oos else ""}>
                                <span style="font-weight: 600; color: #b91c1c;">Place machine out of service immediately</span>
                            </label>
                        </div>
                    </div>

                    <div class="form-group">
                        <label class="form-label" for="description">Detailed Description of the Problem</label>
                        <textarea name="description" id="description" class="form-control" rows="4" placeholder="Explain what happened, error codes displayed, materials being cut/printed, or unusual noises observed...">{escape_html(val_desc)}</textarea>
                    </div>

                    <div class="form-group">
                        <label class="form-label" for="attachment">Attach Photo, Diagnostic Log or Report (Optional, max 10MB)</label>
                        <input type="file" name="attachment" id="attachment" class="form-control">
                        <small style="color: #64748b;">Supported formats: PNG, JPG, PDF, TXT, LOG, CSV, JSON, ZIP.</small>
                    </div>

                    <div style="display: flex; justify-content: flex-end; gap: 1rem; margin-top: 1.5rem;">
                        <a href="/incidents" class="btn btn-secondary">Cancel</a>
                        <button type="submit" class="btn btn-danger">Submit Incident Report</button>
                    </div>
                </form>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title="Report Machine Incident",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.post("/machines/{id}/incidents/new")
    @router.post("/incidents/new")
    @require_auth
    def incident_create_action(req: Request) -> Response:
        """Process incident report submission with field retention on error."""
        user = req.user
        if user.get("role") == ROLE_VIEWER:
            return Response.redirect("/incidents?error=Forbidden:+Viewers+have+read-only+access.")

        machine_id_val = req.form("machine_id") or req.route_params.get("id")
        title_val = req.form("title", "").strip()
        severity_val = req.form("severity", "minor").strip()
        description_val = req.form("description")
        takes_oos = bool(req.form("takes_machine_out_of_service") in ("1", "true", "on", "yes"))

        if not machine_id_val or not machine_id_val.isdigit():
            return incident_create_view(req, form_data=req.form(), error_msg="Please select a valid machine.")

        if not title_val:
            return incident_create_view(req, form_data=req.form(), error_msg="Incident title is required.")

        try:
            inc = create_incident(
                machine_id=int(machine_id_val),
                title=title_val,
                description=description_val,
                severity=severity_val,
                status="reported",
                reported_by_user_id=user.get("id"),
                takes_machine_out_of_service=takes_oos,
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )

            # Handle optional file upload
            uploaded_file = req.file("attachment")
            if uploaded_file and getattr(uploaded_file, "filename", "") and getattr(uploaded_file, "size", 0) > 0:
                try:
                    add_incident_attachment(
                        incident_id=inc["id"],
                        uploaded_file=uploaded_file,
                        uploaded_by_user_id=user.get("id"),
                        actor=user,
                        actor_id=user.get("id"),
                        actor_name=user.get("full_name") or user.get("username"),
                        ip_address=getattr(req, "client_ip", None),
                    )
                except Exception as upload_err:
                    logger.warning("Could not save initial attachment for incident #%d: %s", inc["id"], upload_err)
                    return Response.redirect(f"/incidents/{inc['id']}?warning=Incident+reported+but+attachment+failed:+{escape_html(str(upload_err))}")

            return Response.redirect(f"/incidents/{inc['id']}?success=Incident+reported+successfully.")
        except Exception as e:
            logger.error("Error creating incident: %s", e)
            return incident_create_view(req, form_data=req.form(), error_msg=str(e))

    @router.get("/incidents/{id}")
    @require_auth
    def incident_detail_view(req: Request) -> Response:
        """Render detailed incident view with attachments, timeline, and actions."""
        user = req.user
        i_id = req.route_params.get("id", "")
        if not i_id or not i_id.isdigit():
            return Response.redirect("/incidents?error=Invalid+incident+ID.")

        inc = get_incident_by_id(int(i_id))
        if not inc:
            return Response.redirect("/incidents?error=Incident+report+not+found.")

        can_manage = has_permission(user, PERM_MAINTENANCE_MANAGE)
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        attachments = list_incident_attachments(int(i_id))

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"
        flash_banner = ""
        if flash_msg:
            flash_banner = f"""
            <div class="alert alert-{flash_class} alert-dismissible" style="margin-bottom: 1.5rem;">
                <span>{escape_html(flash_msg)}</span>
                <button type="button" class="btn-close" onclick="this.parentElement.remove();">&times;</button>
            </div>
            """

        sev = inc.get("severity", "minor")
        if sev == "critical":
            sev_badge = '<span class="badge badge-danger" style="background-color: #b91c1c; color: white; font-size: 0.9rem; padding: 0.35rem 0.75rem;">Critical Severity</span>'
        elif sev == "major":
            sev_badge = '<span class="badge badge-warning" style="background-color: #ea580c; color: white; font-size: 0.9rem; padding: 0.35rem 0.75rem;">Major Severity</span>'
        else:
            sev_badge = '<span class="badge badge-info" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Minor Severity</span>'

        st = inc.get("status", "reported")
        if st == "investigating":
            st_badge = '<span class="badge badge-warning" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Under Investigation</span>'
        elif st == "resolved":
            st_badge = '<span class="badge badge-success" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Resolved</span>'
        elif st == "closed":
            st_badge = '<span class="badge badge-secondary" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Closed</span>'
        else:
            st_badge = '<span class="badge badge-danger" style="font-size: 0.9rem; padding: 0.35rem 0.75rem;">Reported</span>'

        oos_banner = ""
        if inc.get("takes_machine_out_of_service"):
            oos_banner = """
            <div style="background-color: #fee2e2; border: 1px solid #fca5a5; border-radius: var(--radius-md); padding: 1rem; margin-bottom: 1.5rem; display: flex; align-items: center; gap: 0.75rem;">
                <span style="font-size: 1.5rem; color: #b91c1c;">⚠️</span>
                <div>
                    <div style="font-weight: 700; color: #991b1b;">Machine Placed Out of Service</div>
                    <div style="font-size: 0.9rem; color: #7f1d1d;">This incident has put the machine out of commission. Resolving or closing this report will allow equipment restoration.</div>
                </div>
            </div>
            """

        # Status transition buttons
        status_buttons = []
        if can_manage:
            if st == "reported":
                status_buttons.append(f"""
                <form method="POST" action="/incidents/{i_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="investigating">
                    <button type="submit" class="btn btn-warning">Investigate</button>
                </form>
                """)
            if st in ("reported", "investigating"):
                status_buttons.append(f"""
                <form method="POST" action="/incidents/{i_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="resolved">
                    <button type="submit" class="btn btn-success">Mark Resolved</button>
                </form>
                """)
            if st == "resolved":
                status_buttons.append(f"""
                <form method="POST" action="/incidents/{i_id}/status" style="display: inline;">
                    {csrf_field}
                    <input type="hidden" name="status" value="closed">
                    <button type="submit" class="btn btn-outline-secondary">Close Incident</button>
                </form>
                """)

        # Attachments table
        att_rows = []
        for att in attachments:
            a_id = att["id"]
            orig_name = escape_html(att["original_filename"])
            size_kb = f"{att['file_size'] / 1024:.1f} KB" if att['file_size'] < 1024 * 1024 else f"{att['file_size'] / (1024*1024):.2f} MB"
            mime = escape_html(att.get("mime_type") or "application/octet-stream")
            uploader = escape_html(att.get("uploader_name") or att.get("uploader_username") or "System")
            uploaded_at = escape_html(format_display(att.get("created_at")))

            att_actions = [f'<a href="/incidents/{i_id}/attachments/{a_id}/download" class="btn btn-sm btn-outline-primary" download>Download</a>']
            if can_manage:
                att_actions.append(f"""
                <form method="POST" action="/incidents/{i_id}/attachments/{a_id}/delete" style="display: inline;" onsubmit="return confirm('Delete this attachment?');">
                    {csrf_field}
                    <button type="submit" class="btn btn-sm btn-outline-danger">Delete</button>
                </form>
                """)

            att_rows.append(f"""
            <tr>
                <td><strong>📎 {orig_name}</strong></td>
                <td style="font-family: var(--font-mono); font-size: 0.85rem;">{size_kb}</td>
                <td style="font-size: 0.85rem; color: #64748b;">{mime}</td>
                <td style="font-size: 0.85rem;">{uploader}</td>
                <td style="font-size: 0.85rem; color: #64748b;">{uploaded_at}</td>
                <td style="text-align: right; white-space: nowrap;">
                    {' '.join(att_actions)}
                </td>
            </tr>
            """)

        att_table_body = "".join(att_rows) if att_rows else """
            <tr>
                <td colspan="6" style="text-align: center; padding: 2rem; color: #64748b;">
                    No files or photos attached to this incident report.
                </td>
            </tr>
        """

        reporter_display = escape_html(inc.get("reporter_name") or inc.get("reporter_username") or "Anonymous")
        resolved_display = escape_html(format_display(inc.get("resolved_at"))) if inc.get("resolved_at") else "Unresolved"

        content = f"""
        {flash_banner}
        {oos_banner}
        <div style="margin-bottom: 1.5rem;">
            <a href="/incidents" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to Incidents</a>
            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 0.5rem; flex-wrap: wrap; gap: 1rem;">
                <div>
                    <div style="display: flex; align-items: center; gap: 0.75rem;">
                        <h1 style="font-size: 1.85rem; font-weight: 700; color: #0f172a; margin: 0;">Incident #{inc['id']}: {escape_html(inc['title'])}</h1>
                        {sev_badge}
                        {st_badge}
                    </div>
                    <p style="color: #64748b; margin-top: 0.25rem;">
                        Equipment: <a href="/machines/{inc['machine_id']}" style="font-weight: 600; color: #1e40af; text-decoration: none;">{escape_html(inc['machine_name'])} ({escape_html(inc['machine_code'])})</a>
                    </p>
                </div>
                <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
                    {' '.join(status_buttons)}
                    {f'<a href="/incidents/{i_id}/edit" class="btn btn-secondary">Edit Incident</a>' if can_manage else ''}
                </div>
            </div>
        </div>

        <div class="grid grid-3" style="gap: 1.5rem; margin-bottom: 1.5rem;">
            <div class="card" style="padding: 1.5rem;">
                <h3 style="font-size: 1rem; font-weight: 600; color: #0f172a; margin-bottom: 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">Reporter Information</h3>
                <div style="display: flex; flex-direction: column; gap: 0.75rem; font-size: 0.9rem;">
                    <div><span style="color: #64748b;">Reported By:</span> <strong>{reporter_display}</strong></div>
                    <div><span style="color: #64748b;">Created At:</span> <strong style="font-family: var(--font-mono);">{escape_html(format_display(inc.get('created_at')))}</strong></div>
                </div>
            </div>

            <div class="card" style="padding: 1.5rem;">
                <h3 style="font-size: 1rem; font-weight: 600; color: #0f172a; margin-bottom: 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">Outage & Availability</h3>
                <div style="display: flex; flex-direction: column; gap: 0.75rem; font-size: 0.9rem;">
                    <div><span style="color: #64748b;">Out of Service:</span> <strong>{'YES (Blocked)' if inc.get('takes_machine_out_of_service') else 'NO'}</strong></div>
                    <div><span style="color: #64748b;">Resolved At:</span> <strong>{resolved_display}</strong></div>
                </div>
            </div>

            <div class="card" style="padding: 1.5rem;">
                <h3 style="font-size: 1rem; font-weight: 600; color: #0f172a; margin-bottom: 1rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem;">Equipment State</h3>
                <div style="display: flex; flex-direction: column; gap: 0.75rem; font-size: 0.9rem;">
                    <div><span style="color: #64748b;">Machine Code:</span> <strong style="font-family: var(--font-mono);">{escape_html(inc['machine_code'])}</strong></div>
                    <div><span style="color: #64748b;">Current Fleet State:</span> <strong>{escape_html(inc.get('machine_state', 'available'))}</strong></div>
                </div>
            </div>
        </div>

        <div class="card" style="padding: 1.5rem; margin-bottom: 1.5rem;">
            <h3 style="font-size: 1.1rem; font-weight: 600; color: #0f172a; margin-bottom: 0.75rem;">Problem Description & Observational Evidence</h3>
            <p style="color: #334155; font-size: 0.95rem; line-height: 1.6; white-space: pre-wrap;">{escape_html(inc.get('description') or 'No description provided.')}</p>
        </div>

        <!-- Attachments Card -->
        <div class="card" style="overflow: hidden; margin-bottom: 1.5rem;">
            <div style="padding: 1.25rem 1.5rem; border-bottom: 1px solid #e2e8f0; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h3 style="font-size: 1.15rem; font-weight: 700; color: #0f172a; margin: 0;">Attached Photos, Logs & Reports</h3>
                    <p style="color: #64748b; font-size: 0.85rem; margin: 0;">Forensic evidence and diagnostic logs securely stored for this incident.</p>
                </div>
            </div>
            <div class="table-responsive">
                <table class="table" style="margin-bottom: 0;">
                    <thead>
                        <tr>
                            <th>File Name</th>
                            <th>Size</th>
                            <th>Type</th>
                            <th>Uploaded By</th>
                            <th>Date</th>
                            <th style="text-align: right;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {att_table_body}
                    </tbody>
                </table>
            </div>

            <!-- Upload Additional Attachment -->
            <div style="padding: 1.25rem 1.5rem; background-color: #f8fafc; border-top: 1px solid #e2e8f0;">
                <form method="POST" action="/incidents/{i_id}/attachments/new" enctype="multipart/form-data" style="display: flex; gap: 1rem; align-items: flex-end; flex-wrap: wrap;">
                    {csrf_field}
                    <div style="flex: 2; min-width: 240px;">
                        <label class="form-label" for="new_att">Upload Additional File / Log (Max 10MB)</label>
                        <input type="file" name="attachment" id="new_att" class="form-control" required>
                    </div>
                    <button type="submit" class="btn btn-secondary">Upload Attachment</button>
                </form>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title=f"Incident #{inc['id']}",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.get("/incidents/{id}/edit")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def incident_edit_view(req: Request) -> Response:
        """Render form to edit incident report details."""
        user = req.user
        i_id = req.route_params.get("id", "")
        if not i_id or not i_id.isdigit():
            return Response.redirect("/incidents?error=Invalid+incident+ID.")

        inc = get_incident_by_id(int(i_id))
        if not inc:
            return Response.redirect("/incidents?error=Incident+report+not+found.")

        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        sev_options = []
        for s in VALID_INCIDENT_SEVERITIES:
            sel = " selected" if s == inc.get("severity") else ""
            sev_options.append(f'<option value="{s}"{sel}>{s.capitalize()}</option>')

        st_options = []
        for st_item in VALID_INCIDENT_STATUSES:
            sel = " selected" if st_item == inc.get("status") else ""
            st_options.append(f'<option value="{st_item}"{sel}>{st_item.capitalize()}</option>')

        content = f"""
        <div style="max-width: 760px; margin: 0 auto;">
            <div style="margin-bottom: 1.5rem;">
                <a href="/incidents/{inc['id']}" style="text-decoration: none; color: #64748b; font-size: 0.9rem;">&larr; Back to Incident #{inc['id']}</a>
                <h1 style="font-size: 1.85rem; font-weight: 700; color: #0f172a; margin-top: 0.5rem;">Edit Incident #{inc['id']}</h1>
                <p style="color: #64748b;">Target Equipment: <strong>{escape_html(inc['machine_name'])} ({escape_html(inc['machine_code'])})</strong></p>
            </div>

            <div class="card" style="padding: 2rem;">
                <form method="POST" action="/incidents/{inc['id']}/edit">
                    {csrf_field}

                    <div class="form-group">
                        <label class="form-label" for="title">Incident Title <span style="color: #ef4444;">*</span></label>
                        <input type="text" name="title" id="title" class="form-control" required value="{escape_html(inc['title'])}">
                    </div>

                    <div class="grid grid-2" style="gap: 1rem;">
                        <div class="form-group">
                            <label class="form-label" for="severity">Severity Level</label>
                            <select name="severity" id="severity" class="form-control">
                                {''.join(sev_options)}
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="status">Incident Status</label>
                            <select name="status" id="status" class="form-control">
                                {''.join(st_options)}
                            </select>
                        </div>
                    </div>

                    <div class="form-group">
                        <label style="display: flex; align-items: center; gap: 0.5rem; cursor: pointer;">
                            <input type="checkbox" name="takes_machine_out_of_service" value="1" {'checked' if inc.get('takes_machine_out_of_service') else ''}>
                            <span style="font-weight: 600; color: #b91c1c;">Takes machine out of service</span>
                        </label>
                    </div>

                    <div class="form-group">
                        <label class="form-label" for="description">Detailed Problem Description</label>
                        <textarea name="description" id="description" class="form-control" rows="4">{escape_html(inc.get('description') or '')}</textarea>
                    </div>

                    <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 1.5rem;">
                        <form method="POST" action="/incidents/{inc['id']}/delete" style="display: inline;" onsubmit="return confirm('Delete this incident report?');">
                            {csrf_field}
                            <button type="submit" class="btn btn-outline-danger">Delete Incident</button>
                        </form>
                        <div style="display: flex; gap: 1rem;">
                            <a href="/incidents/{inc['id']}" class="btn btn-secondary">Cancel</a>
                            <button type="submit" class="btn btn-warning">Save Changes</button>
                        </div>
                    </div>
                </form>
            </div>
        </div>
        """

        return Response.html(
            render_page(
                title=f"Edit Incident #{inc['id']}",
                content_html=content,
                user=user,
                active_nav="machines",
                csrf_token=csrf_token,
            )
        )

    @router.post("/incidents/{id}/edit")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def incident_edit_action(req: Request) -> Response:
        """Process incident update action."""
        user = req.user
        i_id = req.route_params.get("id", "")
        if not i_id or not i_id.isdigit():
            return Response.redirect("/incidents?error=Invalid+incident+ID.")

        title_val = req.form("title")
        sev_val = req.form("severity")
        st_val = req.form("status")
        takes_oos = bool(req.form("takes_machine_out_of_service") in ("1", "true", "on", "yes"))
        desc_val = req.form("description", "UNSET")

        try:
            update_incident(
                incident_id=int(i_id),
                title=title_val if title_val else None,
                description=desc_val,
                severity=sev_val,
                status=st_val,
                takes_machine_out_of_service=takes_oos,
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/incidents/{i_id}?success=Incident+updated+successfully.")
        except Exception as e:
            logger.error("Error updating incident: %s", e)
            return Response.redirect(f"/incidents/{i_id}/edit?error={escape_html(str(e))}")

    @router.post("/incidents/{id}/status")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def incident_status_action(req: Request) -> Response:
        """Quick status change action for incidents."""
        user = req.user
        i_id = req.route_params.get("id", "")
        if not i_id or not i_id.isdigit():
            return Response.redirect("/incidents?error=Invalid+incident+ID.")

        new_status = req.form("status", "")
        notes = req.form("notes")

        try:
            change_incident_status(
                incident_id=int(i_id),
                new_status=new_status,
                notes=notes,
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/incidents/{i_id}?success=Status+updated+to+{new_status}.")
        except Exception as e:
            logger.error("Error updating incident status: %s", e)
            return Response.redirect(f"/incidents/{i_id}?error={escape_html(str(e))}")

    @router.post("/incidents/{id}/attachments/new")
    @require_auth
    def incident_attachment_create_action(req: Request) -> Response:
        """Upload an additional attachment to an incident."""
        user = req.user
        i_id = req.route_params.get("id", "")
        if not i_id or not i_id.isdigit():
            return Response.redirect("/incidents?error=Invalid+incident+ID.")

        uploaded_file = req.file("attachment")
        if not uploaded_file or getattr(uploaded_file, "size", 0) <= 0:
            return Response.redirect(f"/incidents/{i_id}?error=Please+select+a+valid+file+to+upload.")

        try:
            add_incident_attachment(
                incident_id=int(i_id),
                uploaded_file=uploaded_file,
                uploaded_by_user_id=user.get("id"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/incidents/{i_id}?success=Attachment+uploaded+successfully.")
        except Exception as e:
            logger.warning("Upload error: %s", e)
            return Response.redirect(f"/incidents/{i_id}?error={escape_html(str(e))}")

    @router.get("/incidents/{id}/attachments/{att_id}/download")
    @require_auth
    def incident_attachment_download_view(req: Request) -> Response:
        """Secure download endpoint for incident attachments."""
        from pathlib import Path
        from forgedesk.config import UPLOAD_DIR

        i_id = req.route_params.get("id", "")
        a_id = req.route_params.get("att_id", "")
        if not i_id.isdigit() or not a_id.isdigit():
            return Response.json({"error": "Invalid request identifiers."}, status_code=400)

        att = get_incident_attachment_by_id(int(a_id))
        if not att or str(att["incident_id"]) != str(i_id):
            return Response.json({"error": "Attachment not found for this incident."}, status_code=404)

        file_path_str = att["file_path"]
        target_path = Path(file_path_str).resolve()

        # Strict security validation: file must exist and be inside UPLOAD_DIR
        if not target_path.is_file() or not str(target_path).startswith(str(UPLOAD_DIR)):
            return Response.json({"error": "Attachment file not found on disk or invalid path."}, status_code=404)

        mime = att.get("mime_type") or "application/octet-stream"
        orig_name = att.get("original_filename") or target_path.name
        return Response.file(
            filepath=target_path,
            content_type=mime,
            download_filename=orig_name,
        )

    @router.post("/incidents/{id}/attachments/{att_id}/delete")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def incident_attachment_delete_action(req: Request) -> Response:
        """Delete incident attachment."""
        user = req.user
        i_id = req.route_params.get("id", "")
        a_id = req.route_params.get("att_id", "")
        if not i_id.isdigit() or not a_id.isdigit():
            return Response.redirect("/incidents?error=Invalid+identifiers.")

        try:
            delete_incident_attachment(
                attachment_id=int(a_id),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect(f"/incidents/{i_id}?success=Attachment+deleted+successfully.")
        except Exception as e:
            return Response.redirect(f"/incidents/{i_id}?error={escape_html(str(e))}")

    @router.post("/incidents/{id}/delete")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def incident_delete_action(req: Request) -> Response:
        """Delete incident action."""
        user = req.user
        i_id = req.route_params.get("id", "")
        if not i_id or not i_id.isdigit():
            return Response.redirect("/incidents?error=Invalid+incident+ID.")

        try:
            delete_incident(
                incident_id=int(i_id),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.redirect("/incidents?success=Incident+report+deleted+successfully.")
        except Exception as e:
            return Response.redirect(f"/incidents/{i_id}?error={escape_html(str(e))}")

    # =========================================================================
    # REST API: Maintenance Jobs
    # =========================================================================

    @router.get("/api/maintenance-jobs")
    @require_auth
    def api_list_maintenance_jobs(req: Request) -> Response:
        """API: List all maintenance jobs."""
        m_id = req.query("machine_id")
        status = req.query("status")
        prio = req.query("priority")
        assignee = req.query("assigned_to_user_id")
        search = req.query("q")

        jobs = list_maintenance_jobs(
            machine_id=int(m_id) if m_id and m_id.isdigit() else None,
            status=status if status in VALID_MAINTENANCE_JOB_STATUSES else None,
            priority=prio if prio in VALID_MAINTENANCE_PRIORITIES else None,
            assigned_to_user_id=int(assignee) if assignee and assignee.isdigit() else None,
            search=search,
        )
        return Response.json({"success": True, "maintenance_jobs": jobs, "count": len(jobs)})

    @router.post("/api/maintenance-jobs")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def api_create_maintenance_job(req: Request) -> Response:
        """API: Create a new maintenance job."""
        user = req.user
        data = req.json()
        if not data:
            return Response.json({"error": "Missing JSON request body."}, status_code=400)

        try:
            job = create_maintenance_job(
                machine_id=int(data.get("machine_id")),
                title=data.get("title", ""),
                description=data.get("description"),
                priority=data.get("priority", "medium"),
                status=data.get("status", "open"),
                assigned_to_user_id=int(data["assigned_to_user_id"]) if data.get("assigned_to_user_id") else None,
                opened_by_user_id=user.get("id"),
                scheduled_date=data.get("scheduled_date"),
                notes=data.get("notes"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "maintenance_job": job}, status_code=201)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("API create maintenance job error: %s", e)
            return Response.json({"error": "Internal server error creating maintenance job."}, status_code=500)

    @router.get("/api/maintenance-jobs/{id}")
    @require_auth
    def api_get_maintenance_job(req: Request) -> Response:
        """API: Get maintenance job by ID."""
        j_id = req.route_params.get("id", "")
        if not j_id or not j_id.isdigit():
            return Response.json({"error": "Invalid job ID."}, status_code=400)

        job = get_maintenance_job_by_id(int(j_id))
        if not job:
            return Response.json({"error": "Maintenance job not found."}, status_code=404)
        return Response.json({"success": True, "maintenance_job": job})

    @router.put("/api/maintenance-jobs/{id}")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def api_update_maintenance_job(req: Request) -> Response:
        """API: Update maintenance job."""
        user = req.user
        j_id = req.route_params.get("id", "")
        if not j_id or not j_id.isdigit():
            return Response.json({"error": "Invalid job ID."}, status_code=400)

        data = req.json()
        if not data:
            return Response.json({"error": "Missing JSON request body."}, status_code=400)

        try:
            updated = update_maintenance_job(
                job_id=int(j_id),
                title=data.get("title"),
                description=data.get("description", "UNSET"),
                priority=data.get("priority"),
                status=data.get("status"),
                assigned_to_user_id=data.get("assigned_to_user_id", "UNSET"),
                scheduled_date=data.get("scheduled_date", "UNSET"),
                completed_at=data.get("completed_at", "UNSET"),
                notes=data.get("notes", "UNSET"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "maintenance_job": updated})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)

    @router.patch("/api/maintenance-jobs/{id}/status")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def api_patch_maintenance_job_status(req: Request) -> Response:
        """API: Change maintenance job status."""
        user = req.user
        j_id = req.route_params.get("id", "")
        if not j_id or not j_id.isdigit():
            return Response.json({"error": "Invalid job ID."}, status_code=400)

        data = req.json()
        if not data or "status" not in data:
            return Response.json({"error": "Missing 'status' in request body."}, status_code=400)

        try:
            updated = change_maintenance_job_status(
                job_id=int(j_id),
                new_status=data["status"],
                notes=data.get("notes"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "maintenance_job": updated})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)

    @router.delete("/api/maintenance-jobs/{id}")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def api_delete_maintenance_job(req: Request) -> Response:
        """API: Delete maintenance job."""
        user = req.user
        j_id = req.route_params.get("id", "")
        if not j_id or not j_id.isdigit():
            return Response.json({"error": "Invalid job ID."}, status_code=400)

        try:
            delete_maintenance_job(
                job_id=int(j_id),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "message": "Maintenance job deleted successfully."})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)

    # =========================================================================
    # REST API: Incidents & Attachments
    # =========================================================================

    @router.get("/api/incidents")
    @require_auth
    def api_list_incidents(req: Request) -> Response:
        """API: List all incidents."""
        m_id = req.query("machine_id")
        status = req.query("status")
        sev = req.query("severity")
        search = req.query("q")

        incidents = list_incidents(
            machine_id=int(m_id) if m_id and m_id.isdigit() else None,
            status=status if status in VALID_INCIDENT_STATUSES else None,
            severity=sev if sev in VALID_INCIDENT_SEVERITIES else None,
            search=search,
        )
        return Response.json({"success": True, "incidents": incidents, "count": len(incidents)})

    @router.post("/api/incidents")
    @require_auth
    def api_create_incident(req: Request) -> Response:
        """API: Report a new incident (available to Admin, Operator, and Member)."""
        user = req.user
        if user.get("role") == ROLE_VIEWER:
            return Response.json({"error": "Forbidden: Viewers have read-only access and cannot report incidents."}, status_code=403)

        data = req.json()
        if not data:
            return Response.json({"error": "Missing JSON request body."}, status_code=400)

        try:
            inc = create_incident(
                machine_id=int(data.get("machine_id")),
                title=data.get("title", ""),
                description=data.get("description"),
                severity=data.get("severity", "minor"),
                status=data.get("status", "reported"),
                reported_by_user_id=user.get("id"),
                takes_machine_out_of_service=bool(data.get("takes_machine_out_of_service", False)),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "incident": inc}, status_code=201)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("API create incident error: %s", e)
            return Response.json({"error": "Internal server error creating incident."}, status_code=500)

    @router.get("/api/incidents/{id}")
    @require_auth
    def api_get_incident(req: Request) -> Response:
        """API: Get incident details along with attachments."""
        i_id = req.route_params.get("id", "")
        if not i_id or not i_id.isdigit():
            return Response.json({"error": "Invalid incident ID."}, status_code=400)

        inc = get_incident_by_id(int(i_id))
        if not inc:
            return Response.json({"error": "Incident not found."}, status_code=404)

        attachments = list_incident_attachments(int(i_id))
        res_data = dict(inc)
        res_data["attachments"] = attachments
        return Response.json({"success": True, "incident": res_data})

    @router.put("/api/incidents/{id}")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def api_update_incident(req: Request) -> Response:
        """API: Update incident details."""
        user = req.user
        i_id = req.route_params.get("id", "")
        if not i_id or not i_id.isdigit():
            return Response.json({"error": "Invalid incident ID."}, status_code=400)

        data = req.json()
        if not data:
            return Response.json({"error": "Missing JSON request body."}, status_code=400)

        try:
            updated = update_incident(
                incident_id=int(i_id),
                title=data.get("title"),
                description=data.get("description", "UNSET"),
                severity=data.get("severity"),
                status=data.get("status"),
                takes_machine_out_of_service=data.get("takes_machine_out_of_service"),
                resolved_at=data.get("resolved_at", "UNSET"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "incident": updated})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)

    @router.patch("/api/incidents/{id}/status")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def api_patch_incident_status(req: Request) -> Response:
        """API: Update incident status."""
        user = req.user
        i_id = req.route_params.get("id", "")
        if not i_id or not i_id.isdigit():
            return Response.json({"error": "Invalid incident ID."}, status_code=400)

        data = req.json()
        if not data or "status" not in data:
            return Response.json({"error": "Missing 'status' in request body."}, status_code=400)

        try:
            updated = change_incident_status(
                incident_id=int(i_id),
                new_status=data["status"],
                notes=data.get("notes"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "incident": updated})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)

    @router.delete("/api/incidents/{id}")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def api_delete_incident(req: Request) -> Response:
        """API: Delete incident and attachments."""
        user = req.user
        i_id = req.route_params.get("id", "")
        if not i_id or not i_id.isdigit():
            return Response.json({"error": "Invalid incident ID."}, status_code=400)

        try:
            delete_incident(
                incident_id=int(i_id),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "message": "Incident deleted successfully."})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)

    @router.post("/api/incidents/{id}/attachments")
    @require_auth
    def api_create_incident_attachment(req: Request) -> Response:
        """API: Upload attachment to an incident."""
        user = req.user
        i_id = req.route_params.get("id", "")
        if not i_id or not i_id.isdigit():
            return Response.json({"error": "Invalid incident ID."}, status_code=400)

        uploaded_file = req.file("attachment") or req.file("file")
        if not uploaded_file or getattr(uploaded_file, "size", 0) <= 0:
            return Response.json({"error": "No valid file uploaded."}, status_code=400)

        try:
            att = add_incident_attachment(
                incident_id=int(i_id),
                uploaded_file=uploaded_file,
                uploaded_by_user_id=user.get("id"),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "attachment": att}, status_code=201)
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("API upload attachment error: %s", e)
            return Response.json({"error": "Internal server error saving attachment."}, status_code=500)

    @router.get("/api/incidents/{id}/attachments/{att_id}/download")
    @require_auth
    def api_download_incident_attachment(req: Request) -> Response:
        """API: Secure download endpoint for incident attachment."""
        from pathlib import Path
        from forgedesk.config import UPLOAD_DIR

        i_id = req.route_params.get("id", "")
        a_id = req.route_params.get("att_id", "")
        if not i_id.isdigit() or not a_id.isdigit():
            return Response.json({"error": "Invalid identifiers."}, status_code=400)

        att = get_incident_attachment_by_id(int(a_id))
        if not att or str(att["incident_id"]) != str(i_id):
            return Response.json({"error": "Attachment not found."}, status_code=404)

        file_path_str = att["file_path"]
        target_path = Path(file_path_str).resolve()

        if not target_path.is_file() or not str(target_path).startswith(str(UPLOAD_DIR)):
            return Response.json({"error": "Attachment file not found on disk or invalid path."}, status_code=404)

        mime = att.get("mime_type") or "application/octet-stream"
        orig_name = att.get("original_filename") or target_path.name
        return Response.file(
            filepath=target_path,
            content_type=mime,
            download_filename=orig_name,
        )

    @router.delete("/api/incidents/{id}/attachments/{att_id}")
    @require_auth
    @require_permission(PERM_MAINTENANCE_MANAGE)
    def api_delete_incident_attachment(req: Request) -> Response:
        """API: Delete incident attachment."""
        user = req.user
        a_id = req.route_params.get("att_id", "")
        if not a_id or not a_id.isdigit():
            return Response.json({"error": "Invalid attachment ID."}, status_code=400)

        try:
            delete_incident_attachment(
                attachment_id=int(a_id),
                actor=user,
                actor_id=user.get("id"),
                actor_name=user.get("full_name") or user.get("username"),
                ip_address=getattr(req, "client_ip", None),
            )
            return Response.json({"success": True, "message": "Attachment deleted successfully."})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
