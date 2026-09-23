"""HTTP request handlers for Audit Log inspection, searchable history, and data export."""

import html
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from forgedesk.audit.service import (
    diff_values,
    export_audit_logs_csv,
    export_audit_logs_json,
    get_audit_event_by_id,
    get_audit_filter_options,
    query_audit_logs,
)
from forgedesk.auth.middleware import (
    is_api_request,
    require_auth,
    require_permission,
    require_role,
)
from forgedesk.auth.permissions import (
    PERM_AUDIT_VIEW,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    get_user_role,
    has_permission,
)
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.core.templates import escape_html, render_page
from forgedesk.utils.datetime_tz import format_display, now_rome_iso

logger = logging.getLogger("forgedesk.audit.handlers")


def _get_action_badge(action: str) -> str:
    """Return styled badge HTML based on audit action category."""
    clean_act = escape_html(action)
    if action.startswith("auth."):
        badge_style = "background-color: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd;"
    elif action.startswith("user."):
        badge_style = "background-color: #f3e8ff; color: #7e22ce; border: 1px solid #e9d5ff;"
    elif action.startswith("reservation."):
        badge_style = "background-color: #dbeafe; color: #1d4ed8; border: 1px solid #bfdbfe;"
    elif action.startswith("machine.") or action.startswith("maintenance."):
        badge_style = "background-color: #fef3c7; color: #b45309; border: 1px solid #fde68a;"
    elif action.startswith("inventory."):
        badge_style = "background-color: #dcfce7; color: #15803d; border: 1px solid #bbf7d0;"
    elif action.startswith("charge."):
        badge_style = "background-color: #fee2e2; color: #b91c1c; border: 1px solid #fecaca;"
    else:
        badge_style = "background-color: #f1f5f9; color: #475569; border: 1px solid #e2e8f0;"

    return f'<span class="badge" style="{badge_style}; font-family: monospace; font-size: 0.8rem; padding: 0.2rem 0.5rem;">{clean_act}</span>'


def register_audit_routes(router: Router) -> None:
    """Register all audit logging web and API endpoints onto the router."""

    # -------------------------------------------------------------------------
    # HTML View Handlers
    # -------------------------------------------------------------------------

    @router.get("/audit")
    @require_permission(PERM_AUDIT_VIEW)
    def audit_list_view(req: Request) -> Response:
        """Render searchable and filterable append-only audit history table."""
        # Query parameters
        search_query = req.query("q", "").strip()
        selected_action = req.query("action", "").strip()
        selected_object_type = req.query("object_type", "").strip()
        start_date = req.query("start_date", "").strip()
        end_date = req.query("end_date", "").strip()

        try:
            page = max(1, int(req.query("page", "1")))
        except ValueError:
            page = 1

        per_page = 25
        offset = (page - 1) * per_page

        # Fetch records and total
        records, total_matched = query_audit_logs(
            search_query=search_query or None,
            action=selected_action or None,
            object_type=selected_object_type or None,
            start_date=start_date or None,
            end_date=end_date or None,
            limit=per_page,
            offset=offset,
        )

        total_pages = max(1, (total_matched + per_page - 1) // per_page)
        filter_opts = get_audit_filter_options()

        # Build Action options
        action_options_html = ['<option value="">All Actions</option>']
        for act in filter_opts["actions"]:
            selected = "selected" if act == selected_action else ""
            action_options_html.append(f'<option value="{escape_html(act)}" {selected}>{escape_html(act)}</option>')

        # Build Object Type options
        object_options_html = ['<option value="">All Object Types</option>']
        for obj in filter_opts["object_types"]:
            selected = "selected" if obj == selected_object_type else ""
            object_options_html.append(f'<option value="{escape_html(obj)}" {selected}>{escape_html(obj)}</option>')

        # Build table rows
        rows_html = []
        for r in records:
            dt_display = escape_html(r["created_at"].replace("T", " ")[:19])
            actor_role = r.get("actor_role")
            actor_badge = f'<span class="role-badge role-{escape_html(actor_role)}">{escape_html(actor_role.upper())}</span>' if actor_role else '<span class="badge" style="background:#e2e8f0; color:#475569;">SYSTEM</span>'
            actor_info = f"""
            <div>
                <strong>{escape_html(r['actor_name'])}</strong>
                <div style="font-size: 0.75rem; color: #64748b; margin-top: 2px;">{actor_badge}</div>
            </div>
            """

            action_badge = _get_action_badge(r["action"])
            object_tag = f"<code>{escape_html(r['object_type'])}:{escape_html(r['object_id'])}</code>"

            # Quick summary of diff or details
            diff_summary = []
            if r.get("diffs"):
                for k, d in r["diffs"].items():
                    c_type = d.get("change_type")
                    if c_type == "modified":
                        diff_summary.append(f'<span style="color:#2563eb;">&Delta; {escape_html(k)}</span>')
                    elif c_type == "added":
                        diff_summary.append(f'<span style="color:#16a34a;">+ {escape_html(k)}</span>')
                    elif c_type == "removed":
                        diff_summary.append(f'<span style="color:#dc2626;">- {escape_html(k)}</span>')
            elif r.get("details"):
                # show brief details key summary
                keys = list(r["details"].keys())[:3]
                diff_summary.append(f'<span style="color:#64748b; font-size: 0.8rem;">{", ".join(escape_html(k) for k in keys)}</span>')
            else:
                diff_summary.append('<span style="color:#94a3b8; font-size: 0.8rem;">—</span>')

            ip_tag = f"<code>{escape_html(r['ip_address'])}</code>" if r.get("ip_address") else '<span style="color:#94a3b8;">—</span>'

            # Inspect link
            inspect_btn = f'<a href="/audit/{r["id"]}" class="btn btn-sm btn-outline-primary" style="padding: 0.2rem 0.5rem; font-size: 0.8rem;">Inspect &rarr;</a>'

            rows_html.append(
                f"""
                <tr>
                    <td style="font-family: monospace; font-weight: 600; color: #64748b;">#{r['id']}</td>
                    <td style="white-space: nowrap; font-size: 0.85rem;">{dt_display}</td>
                    <td>{actor_info}</td>
                    <td>{action_badge}</td>
                    <td>{object_tag}</td>
                    <td>{" ".join(diff_summary)}</td>
                    <td style="font-size: 0.8rem;">{ip_tag}</td>
                    <td style="text-align: right;">{inspect_btn}</td>
                </tr>
                """
            )

        if not rows_html:
            rows_html.append(
                """
                <tr>
                    <td colspan="8" style="text-align: center; padding: 2rem; color: #64748b; font-style: italic;">
                        No audit records match the selected filter criteria.
                    </td>
                </tr>
                """
            )

        # Build Pagination links
        base_params = {}
        if search_query:
            base_params["q"] = search_query
        if selected_action:
            base_params["action"] = selected_action
        if selected_object_type:
            base_params["object_type"] = selected_object_type
        if start_date:
            base_params["start_date"] = start_date
        if end_date:
            base_params["end_date"] = end_date

        def make_page_url(p: int) -> str:
            p_dict = dict(base_params)
            p_dict["page"] = str(p)
            return "/audit?" + urlencode(p_dict)

        prev_disabled = "disabled" if page <= 1 else ""
        next_disabled = "disabled" if page >= total_pages else ""
        prev_url = make_page_url(page - 1) if page > 1 else "#"
        next_url = make_page_url(page + 1) if page < total_pages else "#"

        pagination_html = f"""
        <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 1rem; padding-top: 1rem; border-top: 1px solid var(--color-border);">
            <div style="font-size: 0.85rem; color: var(--color-text-muted);">
                Showing <strong>{len(records)}</strong> of <strong>{total_matched}</strong> events (Page <strong>{page}</strong> of <strong>{total_pages}</strong>)
            </div>
            <div style="display: flex; gap: 0.5rem;">
                <a href="{prev_url}" class="btn btn-sm btn-secondary" style="pointer-events: {'none' if page <= 1 else 'auto'}; opacity: {'0.5' if page <= 1 else '1'};">&larr; Previous</a>
                <a href="{next_url}" class="btn btn-sm btn-secondary" style="pointer-events: {'none' if page >= total_pages else 'auto'}; opacity: {'0.5' if page >= total_pages else '1'};">Next &rarr;</a>
            </div>
        </div>
        """

        # Export URL query strings
        export_csv_url = "/audit/export?format=csv&" + urlencode(base_params)
        export_json_url = "/audit/export?format=json&" + urlencode(base_params)

        content = f"""
        <div class="metrics-row" style="margin-bottom: 1.5rem;">
            <div class="metric-card">
                <span class="metric-label">Total Audit Events</span>
                <span class="metric-value">{total_matched}</span>
                <span class="metric-subtext">Append-only historical records</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Action Categories</span>
                <span class="metric-value">{len(filter_opts['actions'])}</span>
                <span class="metric-subtext">Security & operational event types</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Monitored Objects</span>
                <span class="metric-value">{len(filter_opts['object_types'])}</span>
                <span class="metric-subtext">Users, reservations, ledger, assets</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Integrity Status</span>
                <span class="metric-value" style="color: #16a34a; font-size: 1.3rem;">IMMUTABLE</span>
                <span class="metric-subtext">Strict append-only logging</span>
            </div>
        </div>

        <div class="card">
            <div class="card-header" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem;">
                <h2 class="card-title">Searchable Audit Trail</h2>
                <div style="display: flex; gap: 0.5rem;">
                    <a href="{export_csv_url}" class="btn btn-sm btn-outline-primary" download>📥 Export CSV</a>
                    <a href="{export_json_url}" class="btn btn-sm btn-outline-primary" download>📥 Export JSON</a>
                </div>
            </div>
            <div class="card-body">
                <!-- Search & Filter Controls -->
                <form action="/audit" method="GET" style="margin-bottom: 1.5rem; background: #f8fafc; padding: 1rem; border-radius: var(--radius-md); border: 1px solid var(--color-border);">
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 0.75rem; align-items: flex-end;">
                        <div>
                            <label class="form-label" for="q" style="font-size: 0.8rem;">Text Search</label>
                            <input type="text" id="q" name="q" class="form-control form-control-sm" placeholder="Action, ID, Actor, JSON text..." value="{escape_html(search_query)}">
                        </div>
                        <div>
                            <label class="form-label" for="action" style="font-size: 0.8rem;">Action Category</label>
                            <select id="action" name="action" class="form-control form-control-sm">
                                {"".join(action_options_html)}
                            </select>
                        </div>
                        <div>
                            <label class="form-label" for="object_type" style="font-size: 0.8rem;">Object Type</label>
                            <select id="object_type" name="object_type" class="form-control form-control-sm">
                                {"".join(object_options_html)}
                            </select>
                        </div>
                        <div>
                            <label class="form-label" for="start_date" style="font-size: 0.8rem;">Start Date</label>
                            <input type="date" id="start_date" name="start_date" class="form-control form-control-sm" value="{escape_html(start_date)}">
                        </div>
                        <div>
                            <label class="form-label" for="end_date" style="font-size: 0.8rem;">End Date</label>
                            <input type="date" id="end_date" name="end_date" class="form-control form-control-sm" value="{escape_html(end_date)}">
                        </div>
                        <div style="display: flex; gap: 0.5rem;">
                            <button type="submit" class="btn btn-sm btn-primary" style="flex: 1;">Filter</button>
                            <a href="/audit" class="btn btn-sm btn-secondary" style="flex: 1;">Reset</a>
                        </div>
                    </div>
                </form>

                <!-- Audit Log Table -->
                <div class="table-responsive">
                    <table class="table">
                        <thead>
                            <tr>
                                <th style="width: 50px;">ID</th>
                                <th>Timestamp</th>
                                <th>Actor</th>
                                <th>Action</th>
                                <th>Affected Object</th>
                                <th>State Changes / Details</th>
                                <th>IP Address</th>
                                <th style="text-align: right;">Action</th>
                            </tr>
                        </thead>
                        <tbody>
                            {"".join(rows_html)}
                        </tbody>
                    </table>
                </div>

                {pagination_html}
            </div>
        </div>
        """
        html_doc = render_page("Audit Log", content, user=req.user, active_nav="audit")
        return Response.html(html_doc)

    @router.get("/audit/export")
    @require_permission(PERM_AUDIT_VIEW)
    def audit_export_download(req: Request) -> Response:
        """Download filtered audit log as CSV or JSON."""
        fmt = req.query("format", "csv").lower()
        search_query = req.query("q", "").strip() or None
        action = req.query("action", "").strip() or None
        object_type = req.query("object_type", "").strip() or None
        start_date = req.query("start_date", "").strip() or None
        end_date = req.query("end_date", "").strip() or None

        date_stamp = now_rome_iso()[:10].replace("-", "")
        if fmt == "json":
            content_str = export_audit_logs_json(
                search_query=search_query,
                action=action,
                object_type=object_type,
                start_date=start_date,
                end_date=end_date,
            )
            filename = f"forgedesk-audit-{date_stamp}.json"
            return Response(
                status_code=200,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
                body=content_str.encode("utf-8"),
            )
        else:
            content_str = export_audit_logs_csv(
                search_query=search_query,
                action=action,
                object_type=object_type,
                start_date=start_date,
                end_date=end_date,
            )
            filename = f"forgedesk-audit-{date_stamp}.csv"
            return Response(
                status_code=200,
                headers={
                    "Content-Type": "text/csv; charset=utf-8",
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
                body=content_str.encode("utf-8"),
            )

    @router.get("/audit/{id}")
    @require_permission(PERM_AUDIT_VIEW)
    def audit_detail_view(req: Request) -> Response:
        """Detailed inspector page for a single audit event."""
        audit_id_str = req.route_params.get("id", "")
        try:
            audit_id = int(audit_id_str)
        except ValueError:
            return Response.redirect("/audit", status_code=303)

        rec = get_audit_event_by_id(audit_id)
        if not rec:
            return Response.html(
                render_page(
                    "Audit Event Not Found",
                    '<div class="alert alert-danger">The requested audit event was not found.</div><a href="/audit" class="btn btn-primary">&larr; Back to Audit Log</a>',
                    user=req.user,
                    active_nav="audit",
                ),
                status_code=404,
            )

        dt_display = escape_html(rec["created_at"].replace("T", " ")[:19])
        action_badge = _get_action_badge(rec["action"])
        actor_role = rec.get("actor_role")
        actor_badge = f'<span class="role-badge role-{escape_html(actor_role)}">{escape_html(actor_role.upper())}</span>' if actor_role else '<span class="badge" style="background:#e2e8f0; color:#475569;">SYSTEM</span>'

        # Build Field Diff Table
        diff_rows = []
        if rec.get("diffs"):
            for field, d in rec["diffs"].items():
                c_type = d.get("change_type")
                if c_type == "modified":
                    type_badge = '<span class="badge badge-primary">MODIFIED</span>'
                elif c_type == "added":
                    type_badge = '<span class="badge badge-success">ADDED</span>'
                else:
                    type_badge = '<span class="badge badge-danger">REMOVED</span>'

                before_val = escape_html(json.dumps(d.get("before"), ensure_ascii=False)) if d.get("before") is not None else '<span style="color:#94a3b8;">None</span>'
                after_val = escape_html(json.dumps(d.get("after"), ensure_ascii=False)) if d.get("after") is not None else '<span style="color:#94a3b8;">None</span>'

                diff_rows.append(
                    f"""
                    <tr>
                        <td><code>{escape_html(field)}</code></td>
                        <td>{type_badge}</td>
                        <td style="background-color: #fff1f2; font-family: monospace; font-size: 0.85rem;">{before_val}</td>
                        <td style="background-color: #f0fdf4; font-family: monospace; font-size: 0.85rem;">{after_val}</td>
                    </tr>
                    """
                )

        diff_table_html = ""
        if diff_rows:
            diff_table_html = f"""
            <h4 style="margin-top: 1.5rem; margin-bottom: 0.75rem; color: #334155;">Field State Changes (Before &rarr; After)</h4>
            <table class="table" style="margin-bottom: 1.5rem;">
                <thead>
                    <tr>
                        <th>Field Name</th>
                        <th>Change Type</th>
                        <th>Before State</th>
                        <th>After State</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(diff_rows)}
                </tbody>
            </table>
            """

        details_json_formatted = escape_html(json.dumps(rec.get("details"), indent=2, ensure_ascii=False)) if rec.get("details") else "None"
        before_json_formatted = escape_html(json.dumps(rec.get("before"), indent=2, ensure_ascii=False)) if rec.get("before") else "None"
        after_json_formatted = escape_html(json.dumps(rec.get("after"), indent=2, ensure_ascii=False)) if rec.get("after") else "None"

        content = f"""
        <div style="max-width: 1000px; margin: 0 auto;">
            <div style="margin-bottom: 1rem;">
                <a href="/audit" class="btn btn-sm btn-secondary">&larr; Back to Audit Log</a>
            </div>

            <div class="card">
                <div class="card-header">
                    <h2 class="card-title">Audit Event #{rec['id']}: {action_badge}</h2>
                    <span class="tz-badge">{dt_display} (Europe/Rome)</span>
                </div>
                <div class="card-body">
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem; background: #f8fafc; padding: 1.25rem; border-radius: var(--radius-md); border: 1px solid var(--color-border);">
                        <div>
                            <h4 style="margin-bottom: 0.5rem; color: #334155;">Actor Details</h4>
                            <p><strong>Actor Name:</strong> {escape_html(rec['actor_name'])}</p>
                            <p><strong>Actor ID:</strong> <code>{rec['actor_id'] or 'None (System)'}</code></p>
                            <p><strong>Role:</strong> {actor_badge}</p>
                            <p><strong>IP Address:</strong> <code>{escape_html(rec['ip_address'] or 'Local / Unknown')}</code></p>
                        </div>
                        <div>
                            <h4 style="margin-bottom: 0.5rem; color: #334155;">Target Object & Action</h4>
                            <p><strong>Action:</strong> <code>{escape_html(rec['action'])}</code></p>
                            <p><strong>Object Type:</strong> <code>{escape_html(rec['object_type'])}</code></p>
                            <p><strong>Object ID:</strong> <code>{escape_html(rec['object_id'])}</code></p>
                            <p><strong>Timestamp:</strong> <code>{escape_html(rec['created_at'])}</code></p>
                        </div>
                    </div>

                    {diff_table_html}

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-top: 1rem;">
                        <div>
                            <h4 style="margin-bottom: 0.5rem; color: #334155;">Before State JSON</h4>
                            <pre style="max-height: 250px;"><code>{before_json_formatted}</code></pre>
                        </div>
                        <div>
                            <h4 style="margin-bottom: 0.5rem; color: #334155;">After State JSON</h4>
                            <pre style="max-height: 250px;"><code>{after_json_formatted}</code></pre>
                        </div>
                    </div>

                    <div style="margin-top: 1.5rem;">
                        <h4 style="margin-bottom: 0.5rem; color: #334155;">Additional Context & Details JSON</h4>
                        <pre style="max-height: 200px;"><code>{details_json_formatted}</code></pre>
                    </div>
                </div>
            </div>
        </div>
        """
        return Response.html(render_page(f"Audit Event #{rec['id']}", content, user=req.user, active_nav="audit"))

    # -------------------------------------------------------------------------
    # JSON API Handlers
    # -------------------------------------------------------------------------

    @router.get("/api/audit/logs")
    @require_permission(PERM_AUDIT_VIEW)
    def api_audit_logs(req: Request) -> Response:
        """API endpoint returning paginated and filtered audit events."""
        search_query = req.query("q", "").strip() or None
        action = req.query("action", "").strip() or None
        object_type = req.query("object_type", "").strip() or None
        object_id = req.query("object_id", "").strip() or None
        start_date = req.query("start_date", "").strip() or None
        end_date = req.query("end_date", "").strip() or None

        try:
            limit = int(req.query("limit", "50"))
        except ValueError:
            limit = 50

        try:
            offset = int(req.query("offset", "0"))
        except ValueError:
            offset = 0

        records, total_matched = query_audit_logs(
            search_query=search_query,
            action=action,
            object_type=object_type,
            object_id=object_id,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )

        return Response.json({
            "total": total_matched,
            "limit": limit,
            "offset": offset,
            "records": records,
        })

    @router.get("/api/audit/logs/{id}")
    @require_permission(PERM_AUDIT_VIEW)
    def api_audit_log_detail(req: Request) -> Response:
        """API endpoint returning single audit event with diffs."""
        audit_id_str = req.route_params.get("id", "")
        try:
            audit_id = int(audit_id_str)
        except ValueError:
            return Response.json({"error": "Bad Request", "message": "Invalid audit log ID."}, status_code=400)

        record = get_audit_event_by_id(audit_id)
        if not record:
            return Response.json({"error": "Not Found", "message": "Audit event not found."}, status_code=404)

        return Response.json({"record": record})

    @router.get("/api/audit/filter-options")
    @require_permission(PERM_AUDIT_VIEW)
    def api_audit_filter_options(req: Request) -> Response:
        """API endpoint returning distinct actions and object types."""
        return Response.json(get_audit_filter_options())
