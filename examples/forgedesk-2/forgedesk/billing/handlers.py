"""HTTP Request Handlers for Usage Charges, Pricing Calculations, and Financial Adjustments."""

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode

from forgedesk.auth.permissions import (
    can_adjust_charges,
    can_view_charges,
    has_permission,
    is_operator_or_admin,
    PERM_CHARGES_ADJUST,
    PERM_CHARGES_VIEW_ALL,
    PERM_CHARGES_VIEW_OWN,
)
from forgedesk.billing.service import (
    calculate_usage_charge,
    create_charge_adjustment,
    format_cents_currency,
    get_billing_summary,
    get_usage_charge_by_id,
    list_charge_adjustments,
    list_usage_charges,
    parse_currency_to_cents,
)
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.core.templates import csrf_input, escape_html, render_page
from forgedesk.machines.service import list_machines
from forgedesk.members.service import list_members

logger = logging.getLogger("forgedesk.billing.handlers")


def register_billing_routes(router: Router) -> None:
    """Register all web and REST API routes for charges and billing."""

    # -------------------------------------------------------------------------
    # 1. REST API Endpoints
    # -------------------------------------------------------------------------

    @router.get("/api/charges")
    def api_list_charges(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        member_id = req.query("member_id")
        machine_id = req.query("machine_id")
        status = req.query("status")
        date_from = req.query("date_from") or req.query("from")
        date_to = req.query("date_to") or req.query("to")
        search = req.query("search") or req.query("q")
        limit = req.query("limit") or "50"
        offset = req.query("offset") or "0"

        # Enforce RBAC
        is_member = req.user.get("role") == "member"
        filter_member_id = None
        if is_member:
            filter_member_id = req.user.get("member_id")
        elif member_id:
            try:
                filter_member_id = int(member_id)
            except ValueError:
                return Response.json({"error": "Invalid member_id parameter."}, status_code=400)

        filter_machine_id = None
        if machine_id:
            try:
                filter_machine_id = int(machine_id)
            except ValueError:
                return Response.json({"error": "Invalid machine_id parameter."}, status_code=400)

        try:
            charges, total_count, summary = list_usage_charges(
                member_id=filter_member_id,
                machine_id=filter_machine_id,
                status=status,
                date_from=date_from,
                date_to=date_to,
                search=search,
                limit=int(limit),
                offset=int(offset),
                actor_user=req.user,
            )
            return Response.json({
                "charges": charges,
                "total_count": total_count,
                "summary": summary,
                "limit": int(limit),
                "offset": int(offset),
            })
        except Exception as e:
            logger.error("Error listing charges via API: %s", e)
            return Response.json({"error": "Internal error retrieving charges."}, status_code=500)

    @router.get("/api/charges/summary")
    @router.get("/api/billing/summary")
    def api_billing_summary(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        is_member = req.user.get("role") == "member"
        member_id = req.user.get("member_id") if is_member else None
        if not is_member and req.query("member_id"):
            try:
                member_id = int(req.query("member_id"))
            except ValueError:
                pass

        date_from = req.query("date_from")
        date_to = req.query("date_to")

        summary = get_billing_summary(member_id=member_id, date_from=date_from, date_to=date_to)
        return Response.json(summary)

    @router.get("/api/charges/{id}")
    def api_get_charge(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        charge_id_raw = req.route_params.get("id")
        try:
            charge_id = int(charge_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid charge ID."}, status_code=400)

        charge = get_usage_charge_by_id(charge_id, include_adjustments=True)
        if not charge:
            return Response.json({"error": f"Usage charge #{charge_id} not found."}, status_code=404)

        if not can_view_charges(req.user, target_member_id=charge["member_id"], target_user_id=charge.get("member_user_id")):
            return Response.json({"error": "Access forbidden: you cannot view charges for other members."}, status_code=403)

        return Response.json({"charge": charge})

    @router.post("/api/charges/{id}/adjustments")
    def api_create_charge_adjustment(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        if not can_adjust_charges(req.user):
            return Response.json({"error": "Only administrators are authorized to adjust charges."}, status_code=403)

        charge_id_raw = req.route_params.get("id")
        try:
            charge_id = int(charge_id_raw)
        except (ValueError, TypeError):
            return Response.json({"error": "Invalid charge ID."}, status_code=400)

        body = req.json() or {}
        reason = body.get("reason", "").strip()
        if not reason:
            return Response.json({"error": "Reason is required for charge adjustment."}, status_code=400)

        # Support either adjustment_cents or adjustment_amount (in EUR)
        if "adjustment_cents" in body:
            try:
                adj_cents = int(body["adjustment_cents"])
            except ValueError:
                return Response.json({"error": "adjustment_cents must be an integer."}, status_code=400)
        elif "adjustment_amount" in body or "amount" in body:
            raw_amt = body.get("adjustment_amount") or body.get("amount")
            try:
                adj_cents = parse_currency_to_cents(raw_amt, "adjustment_amount")
            except ValueError as e:
                return Response.json({"error": str(e)}, status_code=400)
        else:
            return Response.json({"error": "adjustment_cents or adjustment_amount is required."}, status_code=400)

        if adj_cents == 0:
            return Response.json({"error": "Adjustment amount cannot be zero."}, status_code=400)

        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            updated_charge = create_charge_adjustment(
                charge_id=charge_id,
                adjustment_cents=adj_cents,
                reason=reason,
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.json({
                "success": True,
                "message": f"Adjustment of {format_cents_currency(adj_cents)} applied successfully.",
                "charge": updated_charge,
            })
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except PermissionError as e:
            return Response.json({"error": str(e)}, status_code=403)
        except Exception as e:
            logger.error("Error creating charge adjustment: %s", e)
            return Response.json({"error": "Internal server error applying adjustment."}, status_code=500)

    @router.post("/api/charges/estimate")
    @router.post("/api/billing/estimate")
    def api_estimate_charge(req: Request) -> Response:
        if not req.user:
            return Response.json({"error": "Authentication required."}, status_code=401)

        body = req.json() or {}
        machine_id = body.get("machine_id")
        start_time = body.get("start_time")
        end_time = body.get("end_time")

        if not machine_id or not start_time or not end_time:
            return Response.json({"error": "machine_id, start_time, and end_time are required."}, status_code=400)

        rates_override = body.get("rates_override")
        if rates_override is not None:
            if not is_operator_or_admin(req.user):
                return Response.json({
                    "error": "Only administrators and operators are authorized to provide rates_override for simulations.",
                    "code": "rates_override_unauthorized",
                }, status_code=403)

        try:
            breakdown = calculate_usage_charge(
                machine_or_id=int(machine_id),
                start_time=start_time,
                end_time=end_time,
                rates_override=rates_override,
            )
            return Response.json({"estimate": breakdown})
        except ValueError as e:
            return Response.json({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Error estimating charge: %s", e)
            return Response.json({"error": "Internal error calculating rate estimate."}, status_code=500)

    # -------------------------------------------------------------------------
    # 2. HTML Web Views
    # -------------------------------------------------------------------------

    @router.get("/charges")
    def html_charges_list_view(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=" + req.path)

        member_id_param = req.query("member_id")
        machine_id_param = req.query("machine_id")
        status_param = req.query("status")
        date_from_param = req.query("date_from")
        date_to_param = req.query("date_to")
        search_param = req.query("q") or req.query("search")

        is_member = req.user.get("role") == "member"
        is_admin = req.user.get("role") == "admin"
        is_staff = is_operator_or_admin(req.user)

        member_id_filter = None
        if is_member:
            member_id_filter = req.user.get("member_id")
        elif member_id_param:
            try:
                member_id_filter = int(member_id_param)
            except ValueError:
                pass

        machine_id_filter = None
        if machine_id_param:
            try:
                machine_id_filter = int(machine_id_param)
            except ValueError:
                pass

        charges, total_count, summary = list_usage_charges(
            member_id=member_id_filter,
            machine_id=machine_id_filter,
            status=status_param,
            date_from=date_from_param,
            date_to=date_to_param,
            search=search_param,
            limit=100,
            offset=0,
            actor_user=req.user,
        )

        all_machines = list_machines(include_retired=True)
        all_members = list_members() if is_staff else []

        # Build Machine filter options
        machine_opts = ['<option value="">All Machines</option>']
        for m in all_machines:
            sel = ' selected' if machine_id_filter == m['id'] else ''
            machine_opts.append(f'<option value="{m["id"]}"{sel}>{escape_html(m["code"])} - {escape_html(m["name"])}</option>')

        # Build Member filter options (for staff)
        member_opts = ['<option value="">All Members</option>']
        for mem in all_members:
            sel = ' selected' if member_id_filter == mem['id'] else ''
            member_opts.append(f'<option value="{mem["id"]}"{sel}>{escape_html(mem["member_number"])} - {escape_html(mem["full_name"])}</option>')

        # Build Status filter options
        status_opts = ['<option value="">All Statuses</option>']
        for st in [("finalized", "Finalized (Original)"), ("adjusted", "Adjusted (With Adjustments)"), ("voided", "Voided")]:
            sel = ' selected' if status_param == st[0] else ''
            status_opts.append(f'<option value="{st[0]}"{sel}>{st[1]}</option>')

        # Build Charges Table Rows
        table_rows = []
        if charges:
            for c in charges:
                status_cls = "badge-success" if c["status"] == "finalized" else ("badge-warning" if c["status"] == "adjusted" else "badge-danger")
                adj_badge = ""
                if c.get("adjustments_count", 0) > 0:
                    adj_val = c.get("total_adjustments_cents", 0)
                    adj_cls = "text-success" if adj_val < 0 else "text-danger"
                    adj_badge = f'<span class="{adj_cls}" style="font-size: 0.8rem; font-weight: bold; margin-left: 0.3rem;">({c["formatted_total_adjustments"]})</span>'

                mem_cell = f'<a href="/members/{c["member_id"]}"><strong>{escape_html(c["member_name"])}</strong></a><br><small class="text-muted">{escape_html(c["member_number"])}</small>'
                mach_cell = f'<a href="/machines/{c["machine_id"]}"><strong>{escape_html(c["machine_code"])}</strong></a><br><small class="text-muted">{escape_html(c["machine_name"])}</small>'
                res_link = f'<a href="/reservations/{c["reservation_id"]}">#{c["reservation_id"]}</a>' if c.get("reservation_id") else '<span class="text-muted">—</span>'

                table_rows.append(f"""
                <tr>
                    <td><strong>#{c['id']}</strong></td>
                    <td>{mem_cell}</td>
                    <td>{mach_cell}</td>
                    <td>{res_link}</td>
                    <td>{c['formatted_start_time']}<br><small class="text-muted">{c['duration_minutes']} min ({c.get('rate_breakdown', {}).get('duration_hours', 0)}h)</small></td>
                    <td>{c['formatted_base_charge']}</td>
                    <td>{c['formatted_final_charge']}{adj_badge}</td>
                    <td><span class="badge {status_cls}">{escape_html(c['status'].capitalize())}</span></td>
                    <td class="text-right">
                        <a href="/charges/{c['id']}" class="btn btn-sm btn-outline-primary">View Breakdown</a>
                    </td>
                </tr>
                """)
            table_body = "\n".join(table_rows)
        else:
            table_body = '<tr><td colspan="9" class="text-center text-muted" style="padding: 2rem;">No usage charges found matching current filters.</td></tr>'

        content = f"""
        <div class="card">
            <div class="card-header" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
                <div>
                    <h2 class="card-title">Charges & Machine Usage Billing</h2>
                    <p class="card-subtitle">Usage-based billing ledger with exact Decimal calculation, peak/off-peak rate snapshots, and immutable financial records.</p>
                </div>
            </div>
            <div class="card-body">
                <!-- Summary KPI Row -->
                <div class="metrics-row" style="margin-bottom: 1.5rem;">
                    <div class="metric-card">
                        <span class="metric-label">Total Charges Billed</span>
                        <span class="metric-value">{summary['total_count']}</span>
                        <span class="metric-subtext">Checkout sessions</span>
                    </div>
                    <div class="metric-card">
                        <span class="metric-label">Base Charges Subtotal</span>
                        <span class="metric-value" style="color: #2563eb;">{summary['formatted_sum_base']}</span>
                        <span class="metric-subtext">Calculated at checkout</span>
                    </div>
                    <div class="metric-card">
                        <span class="metric-label">Net Adjustments</span>
                        <span class="metric-value" style="color: {'#16a34a' if summary['sum_adjustments_cents'] <= 0 else '#dc2626'};">{summary['formatted_sum_adjustments']}</span>
                        <span class="metric-subtext">Admin credits/surcharges</span>
                    </div>
                    <div class="metric-card">
                        <span class="metric-label">Final Billed Total</span>
                        <span class="metric-value" style="color: #1e293b; font-weight: 700;">{summary['formatted_sum_final']}</span>
                        <span class="metric-subtext">Net balance</span>
                    </div>
                </div>

                <!-- Filter Form -->
                <form method="GET" action="/charges" class="filter-form" style="background: #f8fafc; padding: 1rem; border-radius: 6px; margin-bottom: 1.5rem; display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: flex-end;">
                    <div class="form-group" style="flex: 1; min-width: 160px; margin-bottom: 0;">
                        <label for="q" style="font-size: 0.85rem; font-weight: 600;">Search Member / Machine:</label>
                        <input type="text" name="q" id="q" value="{escape_html(search_param or '')}" class="form-control form-control-sm" placeholder="Name, code, number...">
                    </div>
                    {f'''
                    <div class="form-group" style="flex: 1; min-width: 160px; margin-bottom: 0;">
                        <label for="member_id" style="font-size: 0.85rem; font-weight: 600;">Member:</label>
                        <select name="member_id" id="member_id" class="form-control form-control-sm">
                            {''.join(member_opts)}
                        </select>
                    </div>
                    ''' if is_staff else ''}
                    <div class="form-group" style="flex: 1; min-width: 160px; margin-bottom: 0;">
                        <label for="machine_id" style="font-size: 0.85rem; font-weight: 600;">Machine:</label>
                        <select name="machine_id" id="machine_id" class="form-control form-control-sm">
                            {''.join(machine_opts)}
                        </select>
                    </div>
                    <div class="form-group" style="flex: 1; min-width: 140px; margin-bottom: 0;">
                        <label for="status" style="font-size: 0.85rem; font-weight: 600;">Status:</label>
                        <select name="status" id="status" class="form-control form-control-sm">
                            {''.join(status_opts)}
                        </select>
                    </div>
                    <div class="form-group" style="width: 130px; margin-bottom: 0;">
                        <label for="date_from" style="font-size: 0.85rem; font-weight: 600;">From:</label>
                        <input type="date" name="date_from" id="date_from" value="{escape_html(date_from_param or '')}" class="form-control form-control-sm">
                    </div>
                    <div class="form-group" style="width: 130px; margin-bottom: 0;">
                        <label for="date_to" style="font-size: 0.85rem; font-weight: 600;">To:</label>
                        <input type="date" name="date_to" id="date_to" value="{escape_html(date_to_param or '')}" class="form-control form-control-sm">
                    </div>
                    <div style="display: flex; gap: 0.5rem; align-items: flex-end;">
                        <button type="submit" class="btn btn-sm btn-primary">Filter</button>
                        <a href="/charges" class="btn btn-sm btn-outline-secondary">Reset</a>
                    </div>
                </form>

                <!-- Charges Table -->
                <div class="table-responsive">
                    <table class="table table-striped table-hover">
                        <thead>
                            <tr>
                                <th>#ID</th>
                                <th>Member</th>
                                <th>Machine</th>
                                <th>Booking</th>
                                <th>Usage Interval</th>
                                <th>Base Charge</th>
                                <th>Final Charge</th>
                                <th>Status</th>
                                <th class="text-right">Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {table_body}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        """
        csrf_val = getattr(req, "csrf_token", "")
        html = render_page("Charges & Billing", content, user=req.user, active_nav="charges", csrf_token=csrf_val)
        return Response.html(html)

    @router.get("/charges/{id}")
    def html_charge_detail_view(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=" + req.path)

        charge_id_raw = req.route_params.get("id")
        try:
            charge_id = int(charge_id_raw)
        except (ValueError, TypeError):
            return Response.redirect("/charges?error=Invalid+charge+ID")

        charge = get_usage_charge_by_id(charge_id, include_adjustments=True)
        if not charge:
            return Response.redirect("/charges?error=Charge+not+found")

        if not can_view_charges(req.user, target_member_id=charge["member_id"], target_user_id=charge.get("member_user_id")):
            return Response.html("<h1>403 Forbidden</h1><p>You are not authorized to view this usage charge.</p>", status_code=403)

        is_admin = can_adjust_charges(req.user)
        breakdown = charge.get("rate_breakdown", {})
        adjustments = charge.get("adjustments", [])

        # Build Adjustment Table
        adj_rows = []
        if adjustments:
            for a in adjustments:
                amt_cents = a.get("adjustment_cents", 0)
                amt_str = a.get("formatted_amount", "")
                amt_cls = "text-success" if amt_cents < 0 else "text-danger"
                actor_info = f"{escape_html(a.get('actor_name') or a.get('actor_username') or 'Admin')} ({escape_html(a.get('actor_role', 'admin'))})"
                adj_rows.append(f"""
                <tr>
                    <td><strong>#{a['id']}</strong></td>
                    <td>{a['formatted_created_at']}</td>
                    <td><strong class="{amt_cls}">{amt_str}</strong></td>
                    <td>{escape_html(a['reason'])}</td>
                    <td>{actor_info}</td>
                </tr>
                """)
            adj_table_html = f"""
            <table class="table table-bordered">
                <thead>
                    <tr>
                        <th>#ID</th>
                        <th>Timestamp</th>
                        <th>Adjustment</th>
                        <th>Reason / Rationale</th>
                        <th>Authorized Actor</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(adj_rows)}
                </tbody>
            </table>
            """
        else:
            adj_table_html = '<p class="text-muted">No adjustments recorded for this charge. Original checkout pricing applies.</p>'

        csrf_val = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_val)

        # Admin adjustment form
        adj_form_html = ""
        if is_admin:
            adj_form_html = f"""
            <div class="card" style="margin-top: 1.5rem; border: 1px solid #cbd5e1;">
                <div class="card-header" style="background: #f1f5f9;">
                    <h3 class="card-title" style="font-size: 1.1rem; color: #1e293b;">Issue Explicit Adjustment (Administrator)</h3>
                    <p class="card-subtitle" style="font-size: 0.85rem;">Adjust the final payable charge without altering the original snapshot. Use positive numbers for surcharges (e.g. 5.00) or negative numbers for discounts/credits (e.g. -3.50).</p>
                </div>
                <div class="card-body">
                    <form method="POST" action="/charges/{charge_id}/adjust">
                        {csrf_field}
                        <div class="row" style="display: flex; gap: 1rem; flex-wrap: wrap;">
                            <div class="form-group" style="flex: 1; min-width: 180px;">
                                <label for="adjustment_amount"><strong>Adjustment Amount (€ EUR):</strong></label>
                                <input type="number" step="0.01" name="adjustment_amount" id="adjustment_amount" class="form-control" placeholder="e.g. -2.50 or 5.00" required>
                                <small class="text-muted">Negative = discount / credit, Positive = surcharge</small>
                            </div>
                            <div class="form-group" style="flex: 2; min-width: 260px;">
                                <label for="reason"><strong>Reason / Justification:</strong></label>
                                <input type="text" name="reason" id="reason" class="form-control" placeholder="e.g. Loyalty discount, tooling surcharge, machine stoppage refund" required>
                                <small class="text-muted">Mandatory justification recorded in audit log</small>
                            </div>
                        </div>
                        <div style="margin-top: 1rem; text-align: right;">
                            <button type="submit" class="btn btn-warning">Apply Financial Adjustment</button>
                        </div>
                    </form>
                </div>
            </div>
            """

        status_cls = "badge-success" if charge["status"] == "finalized" else ("badge-warning" if charge["status"] == "adjusted" else "badge-danger")
        min_badge = '<span class="badge badge-info" style="margin-left: 0.5rem;">Minimum Charge Applied</span>' if breakdown.get("minimum_charge_applied") else ""

        content = f"""
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
            <div>
                <a href="/charges" class="btn btn-sm btn-outline-secondary">&larr; Back to Charges List</a>
            </div>
            <div>
                <span class="badge {status_cls}" style="font-size: 0.9rem;">{escape_html(charge['status'].upper())}</span>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <h2 class="card-title">Usage Charge #{charge['id']} Details</h2>
                <p class="card-subtitle">Finalized at {charge['formatted_created_at']} for {escape_html(charge['member_name'])} on {escape_html(charge['machine_name'])} ({escape_html(charge['machine_code'])})</p>
            </div>
            <div class="card-body">
                <div class="row" style="display: flex; gap: 1.5rem; flex-wrap: wrap;">
                    <!-- Left: Metadata -->
                    <div style="flex: 1; min-width: 280px;">
                        <h4 style="border-bottom: 2px solid #e2e8f0; padding-bottom: 0.5rem;">Session Overview</h4>
                        <table class="table table-sm">
                            <tr>
                                <th>Member:</th>
                                <td><a href="/members/{charge['member_id']}">{escape_html(charge['member_name'])}</a> ({escape_html(charge['member_number'])})</td>
                            </tr>
                            <tr>
                                <th>Machine:</th>
                                <td><a href="/machines/{charge['machine_id']}">{escape_html(charge['machine_name'])}</a> (<code>{escape_html(charge['machine_code'])}</code>)</td>
                            </tr>
                            <tr>
                                <th>Reservation:</th>
                                <td>{f'<a href="/reservations/{charge["reservation_id"]}">Reservation #{charge["reservation_id"]}</a>' if charge.get("reservation_id") else 'Direct Walk-in'}</td>
                            </tr>
                            <tr>
                                <th>Check-in Time:</th>
                                <td>{charge['formatted_start_time']}</td>
                            </tr>
                            <tr>
                                <th>Check-out Time:</th>
                                <td>{charge['formatted_end_time']}</td>
                            </tr>
                            <tr>
                                <th>Total Usage:</th>
                                <td><strong>{charge['duration_minutes']} minutes</strong> ({breakdown.get('duration_hours', 0)} hours)</td>
                            </tr>
                        </table>
                    </div>

                    <!-- Right: Tariff Snapshot Breakdown -->
                    <div style="flex: 1; min-width: 280px; background: #f8fafc; padding: 1rem; border-radius: 6px;">
                        <h4 style="border-bottom: 2px solid #cbd5e1; padding-bottom: 0.5rem;">Rate Snapshot at Checkout {min_badge}</h4>
                        <table class="table table-sm">
                            <tr>
                                <th>Off-Peak Rate:</th>
                                <td>{format_cents_currency(breakdown.get('hourly_rate_cents', 0))}/hr</td>
                            </tr>
                            <tr>
                                <th>Off-Peak Usage:</th>
                                <td>{breakdown.get('offpeak_minutes', 0)} min &rarr; <strong>{format_cents_currency(breakdown.get('offpeak_cost_cents', 0))}</strong></td>
                            </tr>
                            <tr>
                                <th>Peak Rate ({escape_html(breakdown.get('peak_hours_start', '17:00'))} - {escape_html(breakdown.get('peak_hours_end', '21:00'))}):</th>
                                <td>{format_cents_currency(breakdown.get('peak_hourly_rate_cents', 0))}/hr</td>
                            </tr>
                            <tr>
                                <th>Peak Usage:</th>
                                <td>{breakdown.get('peak_minutes', 0)} min &rarr; <strong>{format_cents_currency(breakdown.get('peak_cost_cents', 0))}</strong></td>
                            </tr>
                            <tr>
                                <th>Minimum Charge Threshold:</th>
                                <td>{format_cents_currency(breakdown.get('minimum_charge_cents', 0))}</td>
                            </tr>
                            <tr style="border-top: 2px solid #94a3b8;">
                                <th>Base Charge Subtotal:</th>
                                <td><strong>{charge['formatted_base_charge']}</strong></td>
                            </tr>
                            <tr>
                                <th>Total Adjustments:</th>
                                <td style="color: {'#16a34a' if charge.get('total_adjustments_cents', 0) <= 0 else '#dc2626'}; font-weight: bold;">{charge.get('formatted_total_adjustments', '€0.00')}</td>
                            </tr>
                            <tr style="border-top: 2px solid #334155; font-size: 1.1rem;">
                                <th>Final Payable Charge:</th>
                                <td><span style="color: #1e293b; font-weight: 700;">{charge['formatted_final_charge']}</span></td>
                            </tr>
                        </table>
                    </div>
                </div>

                <!-- Adjustments Ledger -->
                <div style="margin-top: 2rem;">
                    <h3 style="font-size: 1.2rem; border-bottom: 2px solid #e2e8f0; padding-bottom: 0.5rem;">Financial Adjustments Ledger</h3>
                    {adj_table_html}
                </div>

                <!-- Admin Adjustment Form -->
                {adj_form_html}
            </div>
        </div>
        """
        csrf_val = getattr(req, "csrf_token", "")
        html = render_page(f"Charge #{charge_id} - Billing", content, user=req.user, active_nav="charges", csrf_token=csrf_val)
        return Response.html(html)

    @router.post("/charges/{id}/adjust")
    def html_charge_adjust_post(req: Request) -> Response:
        if not req.user:
            return Response.redirect("/auth/login?next=" + req.path)

        if not can_adjust_charges(req.user):
            return Response.html("<h1>403 Forbidden</h1><p>Only administrators can create financial adjustments.</p>", status_code=403)

        charge_id_raw = req.route_params.get("id")
        try:
            charge_id = int(charge_id_raw)
        except (ValueError, TypeError):
            return Response.redirect("/charges?error=Invalid+charge+ID")

        form_data = req.form()
        amount_raw = form_data.get("adjustment_amount") or ""
        reason = form_data.get("reason") or ""
        client_ip = req.client_address[0] if req.client_address else "127.0.0.1"

        try:
            adj_cents = parse_currency_to_cents(amount_raw, "Adjustment amount")
            if adj_cents == 0:
                raise ValueError("Adjustment amount cannot be zero.")
            if not reason.strip():
                raise ValueError("A clear reason is required.")

            create_charge_adjustment(
                charge_id=charge_id,
                adjustment_cents=adj_cents,
                reason=reason.strip(),
                actor_user=req.user,
                ip_address=client_ip,
            )
            return Response.redirect(f"/charges/{charge_id}?success=Adjustment+applied+successfully.")
        except Exception as e:
            return Response.redirect(f"/charges/{charge_id}?error={escape_html(str(e))}")
