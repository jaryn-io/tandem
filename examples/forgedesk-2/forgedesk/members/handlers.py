"""HTTP Request Handlers for Members, Qualifications, and Machine Categories."""

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
    PERM_MEMBERS_MANAGE,
    PERM_MEMBERS_VIEW_ALL,
    PERM_MEMBERS_VIEW_OWN,
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
from forgedesk.members.service import (
    VALID_MEMBERSHIP_STATUSES,
    change_membership_status,
    check_member_qualification,
    create_machine_category,
    create_member,
    delete_member,
    generate_next_member_number,
    get_machine_category_by_id,
    get_member_by_id,
    get_member_by_user_id,
    get_qualification_by_id,
    get_unlinked_users,
    grant_qualification,
    list_machine_categories,
    list_member_qualifications,
    list_members,
    revoke_qualification,
    update_member,
)
from forgedesk.utils.datetime_tz import now_rome, now_rome_iso

logger = logging.getLogger("forgedesk.members.handlers")


def register_member_routes(router: Router) -> None:
    """Register all member, qualification, and category routes."""

    # -------------------------------------------------------------------------
    # HTML View Handlers
    # -------------------------------------------------------------------------

    @router.get("/members")
    @require_auth
    def members_list_view(req: Request) -> Response:
        """Render members management page with filtering, search, and metrics."""
        user = req.user
        user_role = get_user_role(user)

        # If ordinary Member, redirect to their personal member profile view if linked
        if user_role == ROLE_MEMBER:
            member_id = user.get("member_id")
            if member_id:
                return Response.redirect(f"/members/{member_id}")

        status_filter = req.query("status", "").strip().lower()
        search_query = req.query("q", "").strip()

        members = list_members(
            status=status_filter if status_filter in VALID_MEMBERSHIP_STATUSES else None,
            search=search_query if search_query else None,
        )

        # Compute summary counts
        all_members = list_members()
        total_count = len(all_members)
        active_count = sum(1 for m in all_members if m.get("membership_status") == "active" and not m.get("is_expired"))
        suspended_count = sum(1 for m in all_members if m.get("membership_status") == "suspended")
        expired_count = sum(1 for m in all_members if m.get("membership_status") == "expired" or m.get("is_expired"))

        can_manage = has_permission(user, PERM_MEMBERS_MANAGE)
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"

        # Build table rows
        rows = []
        for m in members:
            mem_no = escape_html(m["member_number"])
            full_name = escape_html(m["full_name"])
            email = escape_html(m["email"])
            phone = escape_html(m.get("phone") or "—")
            expiry = escape_html(m.get("membership_expiry") or "None")

            status = m.get("membership_status", "active")
            if m.get("is_expired") or status == "expired":
                status_badge = '<span class="badge badge-danger">Expired</span>'
            elif status == "suspended":
                status_badge = '<span class="badge badge-warning">Suspended</span>'
            else:
                status_badge = '<span class="badge badge-success">Active</span>'

            # Qualifications summary
            total_q = m.get("total_qualifications", 0)
            active_q = m.get("active_qualifications", 0)
            if total_q == 0:
                qual_badge = '<span style="color: #94a3b8; font-size: 0.85rem;">None</span>'
            elif active_q == total_q:
                qual_badge = f'<span class="badge badge-success">{active_q} Active</span>'
            else:
                qual_badge = f'<span class="badge badge-warning">{active_q}/{total_q} Valid</span>'

            # User account link
            if m.get("linked_username"):
                user_tag = f"<code>{escape_html(m['linked_username'])}</code> <span class='role-badge role-{m.get('linked_user_role','member')}'>{escape_html(m.get('linked_user_role','').upper())}</span>"
            else:
                user_tag = '<span style="color: #94a3b8; font-size: 0.85rem;">Unlinked</span>'

            # Action buttons
            actions = [
                f'<a href="/members/{m["id"]}" class="btn btn-sm btn-outline-primary" style="padding: 0.2rem 0.5rem; font-size: 0.8rem;">Profile</a>'
            ]
            if can_manage:
                actions.append(
                    f'<a href="/members/{m["id"]}/edit" class="btn btn-sm btn-outline-secondary" style="padding: 0.2rem 0.5rem; font-size: 0.8rem;">Edit</a>'
                )

            rows.append(
                f"""
                <tr>
                    <td><code>{mem_no}</code></td>
                    <td><strong><a href="/members/{m['id']}" style="color: inherit; text-decoration: none;">{full_name}</a></strong></td>
                    <td>{email}<br><small style="color: #64748b;">{phone}</small></td>
                    <td>{status_badge}</td>
                    <td><small>{expiry}</small></td>
                    <td>{qual_badge}</td>
                    <td>{user_tag}</td>
                    <td style="white-space: nowrap;">{" ".join(actions)}</td>
                </tr>
                """
            )

        new_member_button = ""
        if can_manage:
            new_member_button = '<a href="/members/new" class="btn btn-primary">+ Register New Member</a>'

        banner_html = ""
        if flash_msg:
            banner_html = f'<div class="alert alert-{flash_class}" style="margin-bottom: 1.25rem;">{escape_html(flash_msg)}</div>'

        content = f"""
        {banner_html}
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
            <div>
                <h1 style="font-size: 1.75rem; font-weight: 800; color: #0f172a; margin-bottom: 0.25rem;">Makerspace Members</h1>
                <p style="color: #64748b; font-size: 0.95rem;">Manage member records, subscription status, and certified machine category qualifications.</p>
            </div>
            <div>
                {new_member_button}
            </div>
        </div>

        <!-- Metrics Row -->
        <div class="metrics-row">
            <div class="metric-card">
                <span class="metric-label">Total Registered</span>
                <span class="metric-value">{total_count}</span>
                <span class="metric-subtext">All Member Records</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Active & Valid</span>
                <span class="metric-value" style="color: #16a34a;">{active_count}</span>
                <span class="metric-subtext">Eligible for Bookings</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Suspended</span>
                <span class="metric-value" style="color: #d97706;">{suspended_count}</span>
                <span class="metric-subtext">Temporarily Restricted</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Expired</span>
                <span class="metric-value" style="color: #dc2626;">{expired_count}</span>
                <span class="metric-subtext">Requires Membership Renewal</span>
            </div>
        </div>

        <!-- Filter & Search Card -->
        <div class="card" style="margin-bottom: 1.5rem;">
            <div class="card-body" style="padding: 1rem 1.25rem;">
                <form action="/members" method="GET" style="display: flex; gap: 1rem; flex-wrap: wrap; align-items: center;">
                    <div style="flex: 1; min-width: 250px;">
                        <input type="text" name="q" value="{escape_html(search_query)}" class="form-control" placeholder="Search by name, email, member number, or phone...">
                    </div>
                    <div style="min-width: 160px;">
                        <select name="status" class="form-control">
                            <option value="">All Statuses</option>
                            <option value="active" {"selected" if status_filter == "active" else ""}>Active Only</option>
                            <option value="suspended" {"selected" if status_filter == "suspended" else ""}>Suspended Only</option>
                            <option value="expired" {"selected" if status_filter == "expired" else ""}>Expired Only</option>
                        </select>
                    </div>
                    <button type="submit" class="btn btn-primary">Filter</button>
                    <a href="/members" class="btn btn-secondary">Reset</a>
                </form>
            </div>
        </div>

        <!-- Members Table -->
        <div class="card">
            <div class="card-header">
                <h2 class="card-title">Member Directory ({len(members)})</h2>
            </div>
            <div class="card-body" style="padding: 0;">
                <div style="overflow-x: auto;">
                    <table class="table" style="width: 100%; margin: 0;">
                        <thead>
                            <tr>
                                <th>Member #</th>
                                <th>Full Name</th>
                                <th>Contact Details</th>
                                <th>Status</th>
                                <th>Expires</th>
                                <th>Qualifications</th>
                                <th>Linked Account</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {"".join(rows) if rows else '<tr><td colspan="8" style="text-align: center; padding: 2rem; color: #64748b;">No members found matching the selected criteria.</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        """
        return Response.html(render_page("Members & Qualifications", content, user=user, active_nav="members", csrf_token=csrf_token))

    @router.get("/members/new")
    @require_permission(PERM_MEMBERS_MANAGE)
    def member_create_view(req: Request, form_data: Optional[Dict[str, Any]] = None, error_msg: str = "") -> Response:
        """Render new member registration form with value retention on error."""
        user = req.user
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)
        next_mem_no = generate_next_member_number()

        fd = form_data or {}
        val_full_name = fd.get("full_name", "")
        val_member_number = fd.get("member_number", next_mem_no)
        val_email = fd.get("email", "")
        val_phone = fd.get("phone", "")
        val_status = fd.get("membership_status", "active")
        val_expiry = fd.get("membership_expiry") or (now_rome() + datetime.timedelta(days=365)).strftime("%Y-%m-%d")
        val_user_id = str(fd.get("user_id", ""))
        val_notes = fd.get("notes", "")

        # Unlinked users dropdown
        unlinked_users = get_unlinked_users()
        user_options = ['<option value="">-- Do not link user account (Profile only) --</option>']
        for u in unlinked_users:
            sel = " selected" if str(u["id"]) == val_user_id else ""
            user_options.append(
                f'<option value="{u["id"]}"{sel}>{escape_html(u["full_name"])} ({escape_html(u["username"])}) - Role: {escape_html(u["role"].upper())}</option>'
            )

        err_text = error_msg or req.query("error", "")
        banner_html = f'<div class="alert alert-danger" style="margin-bottom: 1.25rem;"><strong>Registration Error:</strong> {escape_html(err_text)}</div>' if err_text else ""

        content = f"""
        {banner_html}
        <div style="max-width: 760px; margin: 0 auto;">
            <div style="margin-bottom: 1rem;">
                <a href="/members" style="color: #2563eb; text-decoration: none; font-size: 0.9rem;">&larr; Back to Member Directory</a>
            </div>
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title">Register New Makerspace Member</h2>
                </div>
                <div class="card-body">
                    <form action="/members/new" method="POST">
                        {csrf_field}
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem;">
                            <div class="form-group">
                                <label class="form-label" for="full_name">Full Name *</label>
                                <input type="text" id="full_name" name="full_name" value="{escape_html(val_full_name)}" class="form-control" required placeholder="e.g. Marco Valli">
                            </div>
                            <div class="form-group">
                                <label class="form-label" for="member_number">Member Number</label>
                                <input type="text" id="member_number" name="member_number" value="{escape_html(val_member_number)}" class="form-control" placeholder="e.g. {next_mem_no}">
                                <small style="color: #64748b; font-size: 0.75rem;">Leave as default or specify custom identifier.</small>
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem;">
                            <div class="form-group">
                                <label class="form-label" for="email">Email Address *</label>
                                <input type="email" id="email" name="email" value="{escape_html(val_email)}" class="form-control" required placeholder="member@example.com">
                            </div>
                            <div class="form-group">
                                <label class="form-label" for="phone">Phone Number</label>
                                <input type="text" id="phone" name="phone" value="{escape_html(val_phone)}" class="form-control" placeholder="+39 340 123 4567">
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem;">
                            <div class="form-group">
                                <label class="form-label" for="membership_status">Membership Status *</label>
                                <select id="membership_status" name="membership_status" class="form-control" required>
                                    <option value="active" {"selected" if val_status == "active" else ""}>Active (Eligible for Bookings)</option>
                                    <option value="suspended" {"selected" if val_status == "suspended" else ""}>Suspended (Restricted Access)</option>
                                    <option value="expired" {"selected" if val_status == "expired" else ""}>Expired (Requires Renewal)</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label" for="membership_expiry">Membership Expiry Date (YYYY-MM-DD)</label>
                                <input type="date" id="membership_expiry" name="membership_expiry" value="{escape_html(val_expiry)}" class="form-control">
                            </div>
                        </div>

                        <div class="form-group" style="margin-bottom: 1rem;">
                            <label class="form-label" for="user_id">Link to System User Account</label>
                            <select id="user_id" name="user_id" class="form-control">
                                {"".join(user_options)}
                            </select>
                            <small style="color: #64748b; font-size: 0.75rem;">Links this member profile to an authentication login for self-service booking.</small>
                        </div>

                        <div class="form-group" style="margin-bottom: 1.5rem;">
                            <label class="form-label" for="notes">Internal Operational Notes</label>
                            <textarea id="notes" name="notes" class="form-control" rows="3" placeholder="Emergency contact, specific skill background, or makerspace notes...">{escape_html(val_notes)}</textarea>
                        </div>

                        <div style="display: flex; justify-content: flex-end; gap: 0.75rem;">
                            <a href="/members" class="btn btn-secondary">Cancel</a>
                            <button type="submit" class="btn btn-primary">Create Member Record</button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
        """
        return Response.html(render_page("New Member", content, user=user, active_nav="members", csrf_token=csrf_token))

    @router.post("/members/new")
    @require_permission(PERM_MEMBERS_MANAGE)
    def member_create_action(req: Request) -> Response:
        """Handle new member creation submission with form retention on validation failure."""
        full_name = req.form_value("full_name", "").strip()
        email = req.form_value("email", "").strip()
        phone = req.form_value("phone", "").strip()
        member_number = req.form_value("member_number", "").strip()
        status = req.form_value("membership_status", "active").strip()
        expiry = req.form_value("membership_expiry", "").strip()
        notes = req.form_value("notes", "").strip()
        user_id_raw = req.form_value("user_id", "").strip()

        user_id = int(user_id_raw) if user_id_raw else None

        try:
            new_id = create_member(
                full_name=full_name,
                email=email,
                phone=phone if phone else None,
                user_id=user_id,
                member_number=member_number if member_number else None,
                membership_status=status,
                membership_expiry=expiry if expiry else None,
                notes=notes if notes else None,
                actor_user=req.user,
            )
            return Response.redirect(f"/members/{new_id}?success=Member+record+created+successfully", status_code=303)
        except Exception as e:
            logger.warning("Failed to create member: %s", e)
            return member_create_view(req, form_data=req.form(), error_msg=str(e))

    @router.get("/members/{id}")
    @require_auth
    def member_detail_view(req: Request) -> Response:
        """Display comprehensive member profile, qualifications list, and management actions."""
        user = req.user
        member_id_str = req.route_params.get("id", "")
        try:
            member_id = int(member_id_str)
        except ValueError:
            return Response.redirect("/members?error=Invalid+member+ID", status_code=303)

        member = get_member_by_id(member_id, include_qualifications=True)
        if not member:
            return Response.redirect("/members?error=Member+not+found", status_code=303)

        # Permission check: Member role can only view their own profile
        user_role = get_user_role(user)
        if user_role == ROLE_MEMBER and user.get("member_id") != member_id:
            return Response.html(
                "<h1>403 Forbidden</h1><p>You are only authorized to view your own member profile.</p>",
                status_code=403,
            )

        can_manage = has_permission(user, PERM_MEMBERS_MANAGE)
        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        flash_msg = req.query("success") or req.query("error")
        flash_class = "success" if req.query("success") else "danger"

        # Membership status badge
        status = member["membership_status"]
        if member.get("is_expired") or status == "expired":
            status_badge = '<span class="badge badge-danger">Expired</span>'
            status_explain = f'Membership expired on <strong>{escape_html(member.get("membership_expiry"))}</strong>. Renewal required to make reservations.'
        elif status == "suspended":
            status_badge = '<span class="badge badge-warning">Suspended</span>'
            status_explain = "Membership is suspended. Machine reservations and check-in are disabled."
        else:
            status_badge = '<span class="badge badge-success">Active</span>'
            exp_text = f'valid until {escape_html(member.get("membership_expiry"))}' if member.get("membership_expiry") else 'no expiration date set'
            status_explain = f'Member is in good standing and eligible for reservations ({exp_text}).'

        # Qualifications Table
        qualifications = member.get("qualifications", [])
        qual_rows = []
        for q in qualifications:
            q_status = q.get("status")
            if q_status == "expired":
                q_badge = '<span class="badge badge-danger">Expired</span>'
            elif q_status == "pending_start":
                q_badge = '<span class="badge badge-warning">Starts Future</span>'
            else:
                q_badge = '<span class="badge badge-success">Valid</span>'

            expiry_display = escape_html(q.get("expiry_date") or "Permanent / Lifetime")
            verifier = escape_html(q.get("verified_by_name") or "System / Instructor")

            # Revoke form
            actions_html = "—"
            if can_manage:
                actions_html = f"""
                <form action="/members/{member_id}/qualifications/{q['id']}/delete" method="POST" onsubmit="return confirm('Revoke this qualification?');" style="display: inline;">
                    {csrf_field}
                    <button type="submit" class="btn btn-sm btn-outline-danger" style="padding: 0.15rem 0.4rem; font-size: 0.75rem;">Revoke</button>
                </form>
                """

            qual_rows.append(
                f"""
                <tr>
                    <td><strong>{escape_html(q['qualification_name'])}</strong></td>
                    <td>{escape_html(q['category_name'])} (<code>{escape_html(q['category_code'])}</code>)</td>
                    <td>{escape_html(q['issue_date'])}</td>
                    <td>{expiry_display}</td>
                    <td>{q_badge}</td>
                    <td><small>{verifier}</small></td>
                    <td>{actions_html}</td>
                </tr>
                """
            )

        # Machine Categories for Grant Qualification dropdown
        all_categories = list_machine_categories()
        cat_options = "".join(
            f'<option value="{c["id"]}">{escape_html(c["name"])} ({escape_html(c["code"])})</option>'
            for c in all_categories
        )

        today_iso = now_rome_iso()[:10]
        one_year_iso = (now_rome() + datetime.timedelta(days=365)).strftime("%Y-%m-%d")

        grant_qual_card = ""
        if can_manage:
            grant_qual_card = f"""
            <div class="card" style="margin-top: 1.5rem;">
                <div class="card-header">
                    <h3 class="card-title" style="font-size: 1.05rem;">Grant / Certify Machine Qualification</h3>
                </div>
                <div class="card-body">
                    <form action="/members/{member_id}/qualifications/add" method="POST">
                        {csrf_field}
                        <div style="display: grid; grid-template-columns: 1.2fr 1fr; gap: 1rem; margin-bottom: 1rem;">
                            <div class="form-group">
                                <label class="form-label" for="category_id">Machine Category *</label>
                                <select id="category_id" name="category_id" class="form-control" required>
                                    {cat_options}
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label" for="qualification_name">Qualification Title</label>
                                <input type="text" id="qualification_name" name="qualification_name" class="form-control" placeholder="e.g. Certified 3D Printing Operator">
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem;">
                            <div class="form-group">
                                <label class="form-label" for="issue_date">Issue Date *</label>
                                <input type="date" id="issue_date" name="issue_date" value="{today_iso}" class="form-control" required>
                            </div>
                            <div class="form-group">
                                <label class="form-label" for="expiry_date">Expiry Date (Leave blank for Permanent)</label>
                                <input type="date" id="expiry_date" name="expiry_date" value="{one_year_iso}" class="form-control">
                            </div>
                        </div>

                        <div class="form-group" style="margin-bottom: 1rem;">
                            <label class="form-label" for="notes">Certification Notes / Verification Evidence</label>
                            <input type="text" id="notes" name="notes" class="form-control" placeholder="Passed workshop safety induction, completed practical test...">
                        </div>

                        <div style="display: flex; justify-content: flex-end;">
                            <button type="submit" class="btn btn-primary">+ Issue Qualification</button>
                        </div>
                    </form>
                </div>
            </div>
            """

        status_change_card = ""
        if can_manage:
            status_change_card = f"""
            <div class="card" style="margin-top: 1.5rem;">
                <div class="card-header">
                    <h3 class="card-title" style="font-size: 1.05rem;">Quick Membership Status Update</h3>
                </div>
                <div class="card-body">
                    <form action="/members/{member_id}/status" method="POST" style="display: flex; gap: 1rem; align-items: flex-end; flex-wrap: wrap;">
                        {csrf_field}
                        <div class="form-group" style="flex: 1; min-width: 180px; margin-bottom: 0;">
                            <label class="form-label">Set New Status</label>
                            <select name="status" class="form-control">
                                <option value="active" {"selected" if status == "active" else ""}>Active (Good Standing)</option>
                                <option value="suspended" {"selected" if status == "suspended" else ""}>Suspended (Blocked)</option>
                                <option value="expired" {"selected" if status == "expired" else ""}>Expired (Lapsed)</option>
                            </select>
                        </div>
                        <div class="form-group" style="flex: 2; min-width: 220px; margin-bottom: 0;">
                            <label class="form-label">Reason / Notes</label>
                            <input type="text" name="notes" class="form-control" placeholder="Reason for status change...">
                        </div>
                        <button type="submit" class="btn btn-secondary">Update Status</button>
                    </form>
                </div>
            </div>
            """

        banner_html = ""
        if flash_msg:
            banner_html = f'<div class="alert alert-{flash_class}" style="margin-bottom: 1.25rem;">{escape_html(flash_msg)}</div>'

        content = f"""
        {banner_html}
        <div style="max-width: 960px; margin: 0 auto;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                <a href="/members" style="color: #2563eb; text-decoration: none; font-size: 0.9rem;">&larr; Back to Members Directory</a>
                <div>
                    {"<a href='/members/" + str(member_id) + "/edit' class='btn btn-sm btn-outline-primary'>Edit Member</a>" if can_manage else ""}
                </div>
            </div>

            <!-- Member Summary Header Card -->
            <div class="card">
                <div class="card-header">
                    <div style="display: flex; align-items: center; gap: 0.75rem;">
                        <h2 class="card-title" style="margin: 0;">{escape_html(member['full_name'])}</h2>
                        <code>{escape_html(member['member_number'])}</code>
                    </div>
                    <div>
                        {status_badge}
                    </div>
                </div>
                <div class="card-body">
                    <p style="color: #475569; font-size: 0.9rem; margin-bottom: 1.25rem;">
                        {status_explain}
                    </p>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem;">
                        <div>
                            <h4 style="font-size: 0.95rem; color: #334155; margin-bottom: 0.5rem;">Contact & Account</h4>
                            <p style="margin-bottom: 0.25rem;"><strong>Email:</strong> {escape_html(member['email'])}</p>
                            <p style="margin-bottom: 0.25rem;"><strong>Phone:</strong> {escape_html(member.get('phone') or 'Not specified')}</p>
                            <p style="margin-bottom: 0.25rem;"><strong>System Account:</strong> {escape_html(member.get('linked_username') or 'Unlinked')}</p>
                        </div>
                        <div>
                            <h4 style="font-size: 0.95rem; color: #334155; margin-bottom: 0.5rem;">Membership Details</h4>
                            <p style="margin-bottom: 0.25rem;"><strong>Status:</strong> {status_badge}</p>
                            <p style="margin-bottom: 0.25rem;"><strong>Expiry Date:</strong> {escape_html(member.get('membership_expiry') or 'Indefinite')}</p>
                            <p style="margin-bottom: 0.25rem;"><strong>Member Since:</strong> {escape_html(member.get('created_at', '')[:10])}</p>
                        </div>
                    </div>

                    {f'<div style="margin-top: 1rem; padding: 0.75rem; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px;"><small><strong>Notes:</strong> {escape_html(member.get("notes"))}</small></div>' if member.get('notes') else ''}
                </div>
            </div>

            <!-- Qualifications List Card -->
            <div class="card">
                <div class="card-header">
                    <h3 class="card-title" style="font-size: 1.1rem;">Machine Category Qualifications ({len(qualifications)})</h3>
                </div>
                <div class="card-body" style="padding: 0;">
                    <div style="overflow-x: auto;">
                        <table class="table" style="width: 100%; margin: 0;">
                            <thead>
                                <tr>
                                    <th>Qualification Title</th>
                                    <th>Category</th>
                                    <th>Issued</th>
                                    <th>Expires</th>
                                    <th>Status</th>
                                    <th>Verified By</th>
                                    <th>Action</th>
                                </tr>
                            </thead>
                            <tbody>
                                {"".join(qual_rows) if qual_rows else '<tr><td colspan="7" style="text-align: center; padding: 2rem; color: #64748b;">No machine qualifications registered for this member.</td></tr>'}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            {grant_qual_card}
            {status_change_card}
        </div>
        """
        return Response.html(render_page(f"Member: {member['full_name']}", content, user=user, active_nav="members", csrf_token=csrf_token))

    @router.get("/members/{id}/edit")
    @require_permission(PERM_MEMBERS_MANAGE)
    def member_edit_view(req: Request, form_data: Optional[Dict[str, Any]] = None, error_msg: str = "") -> Response:
        """Render member edit form with value retention on error."""
        user = req.user
        member_id_str = req.route_params.get("id", "")
        try:
            member_id = int(member_id_str)
        except ValueError:
            return Response.redirect("/members?error=Invalid+member+ID", status_code=303)

        member = get_member_by_id(member_id, include_qualifications=False)
        if not member:
            return Response.redirect("/members?error=Member+not+found", status_code=303)

        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        fd = form_data or {}
        val_full_name = fd.get("full_name", member['full_name'])
        val_email = fd.get("email", member['email'])
        val_phone = fd.get("phone", member.get('phone') or '')
        val_status = fd.get("membership_status", member['membership_status'])
        val_expiry = fd.get("membership_expiry", member.get('membership_expiry') or '')
        val_user_id = str(fd.get("user_id", member.get("user_id") or ""))
        val_notes = fd.get("notes", member.get('notes') or '')

        # Unlinked users or currently linked user
        unlinked_users = get_unlinked_users(current_member_user_id=member.get("user_id"))
        user_options = ['<option value="">-- No linked system user account --</option>']
        for u in unlinked_users:
            selected = "selected" if str(u["id"]) == val_user_id else ""
            user_options.append(
                f'<option value="{u["id"]}" {selected}>{escape_html(u["full_name"])} ({escape_html(u["username"])}) - Role: {escape_html(u["role"].upper())}</option>'
            )

        err_text = error_msg or req.query("error", "")
        banner_html = f'<div class="alert alert-danger" style="margin-bottom: 1.25rem;"><strong>Update Error:</strong> {escape_html(err_text)}</div>' if err_text else ""

        content = f"""
        {banner_html}
        <div style="max-width: 760px; margin: 0 auto;">
            <div style="margin-bottom: 1rem;">
                <a href="/members/{member_id}" style="color: #2563eb; text-decoration: none; font-size: 0.9rem;">&larr; Back to Member Profile</a>
            </div>
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title">Edit Member: {escape_html(member['full_name'])} ({escape_html(member['member_number'])})</h2>
                </div>
                <div class="card-body">
                    <form action="/members/{member_id}/edit" method="POST">
                        {csrf_field}
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem;">
                            <div class="form-group">
                                <label class="form-label" for="full_name">Full Name *</label>
                                <input type="text" id="full_name" name="full_name" value="{escape_html(val_full_name)}" class="form-control" required>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Member Number (Immutable)</label>
                                <input type="text" value="{escape_html(member['member_number'])}" class="form-control" disabled>
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem;">
                            <div class="form-group">
                                <label class="form-label" for="email">Email Address *</label>
                                <input type="email" id="email" name="email" value="{escape_html(val_email)}" class="form-control" required>
                            </div>
                            <div class="form-group">
                                <label class="form-label" for="phone">Phone Number</label>
                                <input type="text" id="phone" name="phone" value="{escape_html(val_phone)}" class="form-control">
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem;">
                            <div class="form-group">
                                <label class="form-label" for="membership_status">Membership Status *</label>
                                <select id="membership_status" name="membership_status" class="form-control" required>
                                    <option value="active" {"selected" if val_status == 'active' else ""}>Active (Eligible)</option>
                                    <option value="suspended" {"selected" if val_status == 'suspended' else ""}>Suspended (Restricted)</option>
                                    <option value="expired" {"selected" if val_status == 'expired' else ""}>Expired (Renewal Needed)</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label" for="membership_expiry">Membership Expiry Date (YYYY-MM-DD)</label>
                                <input type="date" id="membership_expiry" name="membership_expiry" value="{escape_html(val_expiry)}" class="form-control">
                            </div>
                        </div>

                        <div class="form-group" style="margin-bottom: 1rem;">
                            <label class="form-label" for="user_id">Linked User Login Account</label>
                            <select id="user_id" name="user_id" class="form-control">
                                {"".join(user_options)}
                            </select>
                        </div>

                        <div class="form-group" style="margin-bottom: 1.5rem;">
                            <label class="form-label" for="notes">Operational Notes</label>
                            <textarea id="notes" name="notes" class="form-control" rows="3">{escape_html(val_notes)}</textarea>
                        </div>

                        <div style="display: flex; justify-content: space-between; align-items: center;">
                            <a href="/members/{member_id}" class="btn btn-secondary">Cancel</a>
                            <button type="submit" class="btn btn-primary">Save Changes</button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
        """
        return Response.html(render_page(f"Edit Member #{member_id}", content, user=user, active_nav="members", csrf_token=csrf_token))

    @router.post("/members/{id}/edit")
    @require_permission(PERM_MEMBERS_MANAGE)
    def member_edit_action(req: Request) -> Response:
        """Process member edit form submission with value retention on error."""
        member_id_str = req.route_params.get("id", "")
        try:
            member_id = int(member_id_str)
        except ValueError:
            return Response.redirect("/members?error=Invalid+member+ID", status_code=303)

        full_name = req.form_value("full_name", "").strip()
        email = req.form_value("email", "").strip()
        phone = req.form_value("phone", "").strip()
        status = req.form_value("membership_status", "active").strip()
        expiry = req.form_value("membership_expiry", "").strip()
        notes = req.form_value("notes", "").strip()
        user_id_raw = req.form_value("user_id", "").strip()

        user_id = int(user_id_raw) if user_id_raw else None

        success, message = update_member(
            member_id=member_id,
            full_name=full_name,
            email=email,
            phone=phone if phone else None,
            user_id=user_id,
            membership_status=status,
            membership_expiry=expiry if expiry else None,
            notes=notes if notes else None,
            actor_user=req.user,
        )

        if success:
            return Response.redirect(f"/members/{member_id}?success={message.replace(' ', '+')}", status_code=303)
        else:
            return member_edit_view(req, form_data=req.form(), error_msg=message)

    @router.post("/members/{id}/status")
    @require_permission(PERM_MEMBERS_MANAGE)
    def member_status_action(req: Request) -> Response:
        """Handle quick status change action."""
        member_id_str = req.route_params.get("id", "")
        try:
            member_id = int(member_id_str)
        except ValueError:
            return Response.redirect("/members?error=Invalid+member+ID", status_code=303)

        new_status = req.form_value("status", "")
        notes = req.form_value("notes", "")

        success, message = change_membership_status(member_id, new_status, actor_user=req.user, notes=notes)
        param = "success" if success else "error"
        return Response.redirect(f"/members/{member_id}?{param}={message.replace(' ', '+')}", status_code=303)

    @router.post("/members/{id}/qualifications/add")
    @require_permission(PERM_MEMBERS_MANAGE)
    def member_grant_qualification_action(req: Request) -> Response:
        """Handle granting or recertifying a machine qualification."""
        member_id_str = req.route_params.get("id", "")
        try:
            member_id = int(member_id_str)
        except ValueError:
            return Response.redirect("/members?error=Invalid+member+ID", status_code=303)

        category_id_str = req.form_value("category_id", "")
        qual_name = req.form_value("qualification_name", "")
        issue_date = req.form_value("issue_date", "")
        expiry_date = req.form_value("expiry_date", "")
        notes = req.form_value("notes", "")

        try:
            category_id = int(category_id_str)
            grant_qualification(
                member_id=member_id,
                category_id=category_id,
                qualification_name=qual_name,
                issue_date=issue_date,
                expiry_date=expiry_date if expiry_date else None,
                notes=notes,
                actor_user=req.user,
            )
            return Response.redirect(f"/members/{member_id}?success=Qualification+granted+successfully", status_code=303)
        except Exception as e:
            logger.warning("Failed to grant qualification: %s", e)
            msg = str(e).replace(" ", "+")
            return Response.redirect(f"/members/{member_id}?error={msg}", status_code=303)

    @router.post("/members/{id}/qualifications/{qid}/delete")
    @require_permission(PERM_MEMBERS_MANAGE)
    def member_revoke_qualification_action(req: Request) -> Response:
        """Handle revoking a qualification."""
        member_id_str = req.route_params.get("id", "")
        qid_str = req.route_params.get("qid", "")
        try:
            member_id = int(member_id_str)
            qid = int(qid_str)
        except ValueError:
            return Response.redirect("/members?error=Invalid+ID", status_code=303)

        success, message = revoke_qualification(qid, actor_user=req.user)
        param = "success" if success else "error"
        return Response.redirect(f"/members/{member_id}?{param}={message.replace(' ', '+')}", status_code=303)

    # -------------------------------------------------------------------------
    # REST JSON API Handlers
    # -------------------------------------------------------------------------

    @router.get("/api/members")
    @require_auth
    def api_list_members(req: Request) -> Response:
        """API endpoint listing members."""
        user = req.user
        user_role = get_user_role(user)

        # For Member role, limit to own member record only
        if user_role == ROLE_MEMBER:
            member_id = user.get("member_id")
            if not member_id and user.get("id"):
                m = get_member_by_user_id(user["id"], include_qualifications=False)
                member_id = m["id"] if m else None

            if member_id:
                member = get_member_by_id(member_id, include_qualifications=True)
                members = [member] if member else []
            else:
                members = []
            return Response.json({"members": members, "count": len(members)})

        status = req.query("status")
        search = req.query("search")
        members = list_members(status=status, search=search)
        return Response.json({"members": members, "count": len(members)})

    @router.get("/api/members/{id}")
    @require_auth
    def api_get_member(req: Request) -> Response:
        """API endpoint retrieving detailed member info."""
        member_id_str = req.route_params.get("id", "")
        try:
            member_id = int(member_id_str)
        except ValueError:
            return Response.json({"error": "Bad Request", "message": "Invalid member ID."}, status_code=400)

        # Check ownership if Member role
        user_role = get_user_role(req.user)
        if user_role == ROLE_MEMBER:
            user_member_id = req.user.get("member_id")
            if not user_member_id and req.user.get("id"):
                m = get_member_by_user_id(req.user["id"], include_qualifications=False)
                user_member_id = m["id"] if m else None

            if user_member_id != member_id:
                return Response.json({"error": "Forbidden", "message": "Access denied to other members' profiles."}, status_code=403)

        member = get_member_by_id(member_id, include_qualifications=True)
        if not member:
            return Response.json({"error": "Not Found", "message": f"Member #{member_id} not found."}, status_code=404)

        return Response.json({"member": member})

    @router.post("/api/members")
    @require_permission(PERM_MEMBERS_MANAGE)
    def api_create_member(req: Request) -> Response:
        """API endpoint creating a member."""
        data = req.json()
        try:
            new_id = create_member(
                full_name=data.get("full_name", ""),
                email=data.get("email", ""),
                phone=data.get("phone"),
                user_id=data.get("user_id"),
                member_number=data.get("member_number"),
                membership_status=data.get("membership_status", "active"),
                membership_expiry=data.get("membership_expiry"),
                notes=data.get("notes"),
                actor_user=req.user,
            )
            created = get_member_by_id(new_id)
            return Response.json({"status": "success", "member": created}, status_code=201)
        except ValueError as e:
            return Response.json({"error": "Bad Request", "message": str(e)}, status_code=400)
        except Exception as e:
            logger.error("API create member failed: %s", e)
            return Response.json({"error": "Internal Server Error", "message": str(e)}, status_code=500)

    @router.put("/api/members/{id}")
    @require_permission(PERM_MEMBERS_MANAGE)
    def api_update_member(req: Request) -> Response:
        """API endpoint updating a member."""
        member_id_str = req.route_params.get("id", "")
        try:
            member_id = int(member_id_str)
        except ValueError:
            return Response.json({"error": "Bad Request", "message": "Invalid member ID."}, status_code=400)

        data = req.json()
        success, message = update_member(
            member_id=member_id,
            full_name=data.get("full_name"),
            email=data.get("email"),
            phone=data.get("phone"),
            user_id=data.get("user_id", False),
            membership_status=data.get("membership_status"),
            membership_expiry=data.get("membership_expiry"),
            notes=data.get("notes"),
            actor_user=req.user,
        )

        if not success:
            return Response.json({"error": "Bad Request", "message": message}, status_code=400)

        updated = get_member_by_id(member_id)
        return Response.json({"status": "success", "message": message, "member": updated})

    @router.patch("/api/members/{id}/status")
    @require_permission(PERM_MEMBERS_MANAGE)
    def api_update_member_status(req: Request) -> Response:
        """API endpoint updating member status."""
        member_id_str = req.route_params.get("id", "")
        try:
            member_id = int(member_id_str)
        except ValueError:
            return Response.json({"error": "Bad Request", "message": "Invalid member ID."}, status_code=400)

        data = req.json()
        new_status = data.get("status", "")
        notes = data.get("notes")

        success, message = change_membership_status(member_id, new_status, actor_user=req.user, notes=notes)
        if not success:
            return Response.json({"error": "Bad Request", "message": message}, status_code=400)

        updated = get_member_by_id(member_id)
        return Response.json({"status": "success", "message": message, "member": updated})

    @router.get("/api/members/{id}/qualifications")
    @require_auth
    def api_list_qualifications(req: Request) -> Response:
        """API endpoint listing member qualifications."""
        member_id_str = req.route_params.get("id", "")
        try:
            member_id = int(member_id_str)
        except ValueError:
            return Response.json({"error": "Bad Request", "message": "Invalid member ID."}, status_code=400)

        # Check ownership if Member role
        user_role = get_user_role(req.user)
        if user_role == ROLE_MEMBER:
            user_member_id = req.user.get("member_id")
            if not user_member_id and req.user.get("id"):
                m = get_member_by_user_id(req.user["id"], include_qualifications=False)
                user_member_id = m["id"] if m else None

            if user_member_id != member_id:
                return Response.json({"error": "Forbidden", "message": "Access denied to other members' qualifications."}, status_code=403)

        quals = list_member_qualifications(member_id)
        return Response.json({"member_id": member_id, "qualifications": quals})

    @router.post("/api/members/{id}/qualifications")
    @require_permission(PERM_MEMBERS_MANAGE)
    def api_grant_qualification(req: Request) -> Response:
        """API endpoint granting a qualification."""
        member_id_str = req.route_params.get("id", "")
        try:
            member_id = int(member_id_str)
        except ValueError:
            return Response.json({"error": "Bad Request", "message": "Invalid member ID."}, status_code=400)

        data = req.json()
        try:
            qid = grant_qualification(
                member_id=member_id,
                category_id=int(data["category_id"]),
                qualification_name=data.get("qualification_name", ""),
                issue_date=data.get("issue_date", now_rome_iso()[:10]),
                expiry_date=data.get("expiry_date"),
                notes=data.get("notes"),
                actor_user=req.user,
            )
            qual = get_qualification_by_id(qid)
            return Response.json({"status": "success", "qualification": qual}, status_code=201)
        except ValueError as e:
            return Response.json({"error": "Bad Request", "message": str(e)}, status_code=400)
        except Exception as e:
            logger.error("API grant qualification failed: %s", e)
            return Response.json({"error": "Internal Server Error", "message": str(e)}, status_code=500)

    @router.delete("/api/members/{id}/qualifications/{qid}")
    @require_permission(PERM_MEMBERS_MANAGE)
    def api_revoke_qualification(req: Request) -> Response:
        """API endpoint revoking a qualification."""
        qid_str = req.route_params.get("qid", "")
        try:
            qid = int(qid_str)
        except ValueError:
            return Response.json({"error": "Bad Request", "message": "Invalid qualification ID."}, status_code=400)

        success, message = revoke_qualification(qid, actor_user=req.user)
        if not success:
            return Response.json({"error": "Not Found", "message": message}, status_code=404)

        return Response.json({"status": "success", "message": message})

    @router.get("/api/members/{id}/check-qualification")
    @require_auth
    def api_check_qualification(req: Request) -> Response:
        """API endpoint to evaluate qualification and eligibility at a specific point in time."""
        member_id_str = req.route_params.get("id", "")
        try:
            member_id = int(member_id_str)
        except ValueError:
            return Response.json({"error": "Bad Request", "message": "Invalid member ID."}, status_code=400)

        # Check ownership if Member role
        user_role = get_user_role(req.user)
        if user_role == ROLE_MEMBER:
            user_member_id = req.user.get("member_id")
            if not user_member_id and req.user.get("id"):
                m = get_member_by_user_id(req.user["id"], include_qualifications=False)
                user_member_id = m["id"] if m else None

            if user_member_id != member_id:
                return Response.json({"error": "Forbidden", "message": "Access denied to evaluate other members' qualifications."}, status_code=403)

        cat_id_str = req.query("category_id")
        target_time = req.query("datetime")

        cat_id = None
        if cat_id_str:
            try:
                cat_id = int(cat_id_str)
            except ValueError:
                return Response.json({"error": "Bad Request", "message": "Invalid category ID."}, status_code=400)

        result = check_member_qualification(member_id, required_category_id=cat_id, target_datetime=target_time)
        return Response.json(result)

    @router.get("/api/machine-categories")
    @require_auth
    def api_list_categories(req: Request) -> Response:
        """API endpoint listing machine categories."""
        categories = list_machine_categories()
        return Response.json({"categories": categories, "count": len(categories)})

    @router.post("/api/machine-categories")
    @require_permission(PERM_MEMBERS_MANAGE)
    def api_create_category(req: Request) -> Response:
        """API endpoint creating a machine category."""
        data = req.json()
        code = data.get("code", "")
        name = data.get("name", "")
        desc = data.get("description")

        try:
            cat_id = create_machine_category(code=code, name=name, description=desc, actor_user=req.user)
            cat = get_machine_category_by_id(cat_id)
            return Response.json({"status": "success", "category": cat}, status_code=201)
        except ValueError as e:
            return Response.json({"error": "Bad Request", "message": str(e)}, status_code=400)
