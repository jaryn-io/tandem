"""HTTP Request Handlers for Inventory Management and Immutable Movement Ledger."""

import datetime
import html
import json
import logging
import urllib.parse
from typing import Any, Dict, List, Optional

from forgedesk.audit.service import record_audit_event
from forgedesk.auth.middleware import (
    is_api_request,
    require_auth,
    require_permission,
)
from forgedesk.auth.permissions import (
    PERM_INVENTORY_CONSUME,
    PERM_INVENTORY_MANAGE,
    PERM_INVENTORY_VIEW,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    get_user_role,
    has_permission,
    is_operator_or_admin,
)
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router
from forgedesk.core.templates import csrf_input, escape_html, render_page
from forgedesk.inventory.service import (
    VALID_MOVEMENT_TYPES,
    VALID_REFERENCE_TYPES,
    compute_item_stock_balances,
    create_inventory_item,
    get_inventory_item_by_id,
    get_inventory_item_by_sku,
    get_inventory_metrics,
    get_item_active_holds,
    get_item_stock_summary,
    get_ledger_history,
    list_inventory_categories,
    list_inventory_items,
    list_inventory_items_with_stock,
    record_consumption,
    record_correction,
    record_receipt,
    record_release,
    record_reservation,
    update_inventory_item,
)
from forgedesk.utils.datetime_tz import format_display, now_rome

logger = logging.getLogger("forgedesk.inventory.handlers")


def register_inventory_routes(router: Router) -> None:
    """Register all HTML and API routes for inventory management and immutable ledger."""

    # =========================================================================
    # HTML View Handlers
    # =========================================================================

    @router.get("/inventory")
    @require_auth
    @require_permission(PERM_INVENTORY_VIEW)
    def inventory_dashboard_view(req: Request) -> Response:
        """Render main inventory catalog, derived stock balances, filters, and recent ledger entries."""
        user = req.user
        can_manage = has_permission(user, PERM_INVENTORY_MANAGE)
        can_consume = has_permission(user, PERM_INVENTORY_CONSUME)

        cat_filter = req.query("category", "").strip()
        search_query = req.query("q", "").strip()
        low_stock_filter = req.query("low_stock", "").strip() == "1"

        items = list_inventory_items_with_stock(
            category=cat_filter if cat_filter else None,
            search=search_query if search_query else None,
            low_stock_only=low_stock_filter,
        )

        all_categories = list_inventory_categories()
        metrics = get_inventory_metrics()
        recent_ledger, total_ledger_count = get_ledger_history(limit=15)

        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        success_msg = req.query("success")
        error_msg = req.query("error")
        flash_messages = []
        if success_msg:
            flash_messages.append({"category": "success", "text": success_msg})
        if error_msg:
            flash_messages.append({"category": "danger", "text": error_msg})

        # Category filter options
        cat_options = ['<option value="">All Categories</option>']
        for c in all_categories:
            selected = " selected" if c == cat_filter else ""
            cat_options.append(f'<option value="{escape_html(c)}"{selected}>{escape_html(c)}</option>')
        cat_options_html = "\n".join(cat_options)

        # Build items table rows
        rows = []
        if not items:
            rows.append(
                '<tr><td colspan="9" style="text-align: center; color: var(--color-text-muted); padding: 2rem;">'
                'No inventory items found matching current filters.</td></tr>'
            )
        else:
            for it in items:
                sku_esc = escape_html(it["sku"])
                name_esc = escape_html(it["name"])
                cat_esc = escape_html(it["category"])
                unit_esc = escape_html(it["unit"])
                cost_esc = escape_html(it["unit_cost_formatted"])
                on_hand = it["on_hand"]
                reserved = it["reserved"]
                available = it["available"]
                min_stock = it["minimum_stock"]
                is_low = it["is_low_stock"]

                stock_badge = ""
                if is_low:
                    stock_badge = '<span class="role-badge role-admin" style="font-size: 0.65rem; margin-left: 0.35rem;">LOW STOCK</span>'

                avail_class = "text-danger" if available == 0 else ("text-warning" if is_low else "text-success")

                action_buttons = [f'<a href="/inventory/{it["id"]}" class="btn btn-sm btn-outline-primary">Details</a>']
                if can_manage:
                    action_buttons.append(
                        f'<button type="button" class="btn btn-sm btn-outline-secondary" '
                        f'onclick="openReceiptModal({it["id"]}, \'{sku_esc}\', \'{name_esc}\', \'{unit_esc}\')">Receive</button>'
                    )
                if can_consume:
                    action_buttons.append(
                        f'<button type="button" class="btn btn-sm btn-outline-warning" '
                        f'onclick="openConsumeModal({it["id"]}, \'{sku_esc}\', \'{name_esc}\', \'{unit_esc}\', {available})">Consume</button>'
                    )

                actions_html = '<div style="display: flex; gap: 0.35rem;">' + "".join(action_buttons) + "</div>"

                rows.append(f"""
                <tr>
                    <td><strong><a href="/inventory/{it['id']}" style="color: var(--color-primary);">{sku_esc}</a></strong></td>
                    <td>{name_esc} {stock_badge}</td>
                    <td><span class="role-badge" style="background: #f1f5f9; color: #475569;">{cat_esc}</span></td>
                    <td>{unit_esc}</td>
                    <td style="font-family: var(--font-mono);">{cost_esc}</td>
                    <td style="font-family: var(--font-mono); text-align: center;"><strong>{on_hand}</strong></td>
                    <td style="font-family: var(--font-mono); text-align: center; color: #d97706;">{reserved}</td>
                    <td style="font-family: var(--font-mono); text-align: center;" class="{avail_class}"><strong>{available}</strong></td>
                    <td style="font-family: var(--font-mono); text-align: center; color: var(--color-text-muted);">{min_stock}</td>
                    <td>{actions_html}</td>
                </tr>
                """)

        items_table_html = "\n".join(rows)

        # Build recent ledger rows
        ledger_rows = []
        if not recent_ledger:
            ledger_rows.append(
                '<tr><td colspan="7" style="text-align: center; color: var(--color-text-muted); padding: 1.5rem;">'
                'No ledger movements recorded yet.</td></tr>'
            )
        else:
            for l in recent_ledger:
                m_type = l["movement_type"]
                qty = l["quantity"]
                qty_str = f"+{qty}" if qty > 0 else str(qty)
                m_badge_class = {
                    "receipt": "background: #dcfce7; color: #166534;",
                    "reservation": "background: #fef3c7; color: #92400e;",
                    "consumption": "background: #fee2e2; color: #991b1b;",
                    "release": "background: #e0f2fe; color: #075985;",
                    "correction": "background: #f3e8ff; color: #6b21a8;",
                }.get(m_type, "background: #f1f5f9; color: #334155;")

                time_display = format_display(l["created_at"]) if l.get("created_at") else ""

                ledger_rows.append(f"""
                <tr>
                    <td style="font-family: var(--font-mono); font-size: 0.85rem;">#{l['id']}</td>
                    <td style="font-size: 0.85rem; color: var(--color-text-muted);">{escape_html(time_display)}</td>
                    <td><strong><a href="/inventory/{l['item_id']}">{escape_html(l['item_sku'])}</a></strong> <small style="color: var(--color-text-muted);">({escape_html(l['item_name'])})</small></td>
                    <td><span class="role-badge" style="{m_badge_class}">{escape_html(m_type.upper())}</span></td>
                    <td style="font-family: var(--font-mono); font-weight: 700; text-align: center;">{qty_str} {escape_html(l['item_unit'])}</td>
                    <td><small style="color: var(--color-text-muted);">{escape_html(l.get('actor_name') or 'System')}</small></td>
                    <td><span style="font-size: 0.9rem;">{escape_html(l['reason'])}</span></td>
                </tr>
                """)
        ledger_table_html = "\n".join(ledger_rows)

        # Header action buttons
        header_actions = []
        if can_manage:
            header_actions.append('<button type="button" class="btn btn-primary" onclick="openModal(\'modal-new-item\')">+ New Inventory Item</button>')
            header_actions.append('<button type="button" class="btn btn-outline-secondary" onclick="openModal(\'modal-quick-movement\')">Record Movement</button>')

        header_actions_html = f'<div style="display: flex; gap: 0.5rem;">{"".join(header_actions)}</div>' if header_actions else ""

        # Construct full HTML content
        content = f"""
        <div class="metrics-row">
            <div class="metric-card">
                <span class="metric-label">Tracked Items (SKUs)</span>
                <span class="metric-value">{metrics['total_skus']}</span>
                <span class="metric-subtext">{len(all_categories)} Active Categories</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Physical On-Hand Units</span>
                <span class="metric-value">{metrics['total_on_hand_units']}</span>
                <span class="metric-subtext">Total Units in Workshop</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Reserved Units</span>
                <span class="metric-value" style="color: #d97706;">{metrics['total_reserved_units']}</span>
                <span class="metric-subtext">Committed to Bookings</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Net Available Units</span>
                <span class="metric-value" style="color: #16a34a;">{metrics['total_available_units']}</span>
                <span class="metric-subtext">Available for Use</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Total Inventory Value</span>
                <span class="metric-value">{metrics['total_value_formatted']}</span>
                <span class="metric-subtext">At Cost Price</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Low Stock Alerts</span>
                <span class="metric-value" style="color: {'#dc2626' if metrics['low_stock_count'] > 0 else '#16a34a'};">{metrics['low_stock_count']}</span>
                <span class="metric-subtext">At or Below Minimum</span>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <div style="display: flex; align-items: center; gap: 1rem;">
                    <h2 class="card-title">Inventory Catalog & Derived Stock</h2>
                    <span class="tz-badge">{len(items)} Items</span>
                </div>
                {header_actions_html}
            </div>
            <div class="card-body">
                <!-- Search & Filters -->
                <form method="GET" action="/inventory" style="display: flex; gap: 1rem; align-items: center; margin-bottom: 1.25rem; flex-wrap: wrap;">
                    <div style="flex: 1; min-width: 220px;">
                        <input type="text" name="q" value="{escape_html(search_query)}" placeholder="Search SKU, name, location..." class="form-control">
                    </div>
                    <div style="min-width: 180px;">
                        <select name="category" class="form-control" onchange="this.form.submit()">
                            {cat_options_html}
                        </select>
                    </div>
                    <div style="display: flex; align-items: center; gap: 0.4rem;">
                        <input type="checkbox" id="low_stock_cb" name="low_stock" value="1" {'checked' if low_stock_filter else ''} onchange="this.form.submit()">
                        <label for="low_stock_cb" style="font-size: 0.9rem; cursor: pointer;">Low Stock Only</label>
                    </div>
                    <div>
                        <button type="submit" class="btn btn-secondary">Filter</button>
                        <a href="/inventory" class="btn btn-outline-secondary" style="margin-left: 0.25rem;">Clear</a>
                    </div>
                </form>

                <div class="table-responsive">
                    <table class="table">
                        <thead>
                            <tr>
                                <th>SKU</th>
                                <th>Name</th>
                                <th>Category</th>
                                <th>Unit</th>
                                <th>Unit Cost</th>
                                <th style="text-align: center;">On-Hand</th>
                                <th style="text-align: center;">Reserved</th>
                                <th style="text-align: center;">Available</th>
                                <th style="text-align: center;">Min</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {items_table_html}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Recent Immutable Ledger Movements -->
        <div class="card">
            <div class="card-header">
                <div style="display: flex; align-items: center; gap: 1rem;">
                    <h3 class="card-title">Recent Immutable Movement Ledger Activity</h3>
                    <span class="tz-badge">{total_ledger_count} Total Entries</span>
                </div>
            </div>
            <div class="card-body">
                <p style="font-size: 0.85rem; color: var(--color-text-muted); margin-bottom: 1rem;">
                    Every stock movement is recorded in an immutable, append-only ledger. All quantities and balances are derived dynamically from these verified movements.
                </p>
                <div class="table-responsive">
                    <table class="table">
                        <thead>
                            <tr>
                                <th>#</th>
                                <th>Timestamp</th>
                                <th>Item</th>
                                <th>Movement</th>
                                <th style="text-align: center;">Delta</th>
                                <th>Actor</th>
                                <th>Reason & Reference</th>
                            </tr>
                        </thead>
                        <tbody>
                            {ledger_table_html}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Modal: New Inventory Item -->
        <div id="modal-new-item" class="modal-overlay" style="display:none;">
            <div class="modal-dialog">
                <div class="modal-header">
                    <h3>Create New Inventory Item</h3>
                    <button type="button" class="btn-close" onclick="closeModal('modal-new-item')">&times;</button>
                </div>
                <form action="/inventory/items/new" method="POST">
                    {csrf_field}
                    <div class="modal-body">
                        <div class="form-group">
                            <label class="form-label">SKU Code *</label>
                            <input type="text" name="sku" required class="form-control" placeholder="e.g. FIL-PLA-WHT-1KG" style="text-transform: uppercase;">
                            <small class="form-hint">Unique alphanumeric identifier (2-32 characters, uppercase, hyphens, numbers).</small>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Item Name *</label>
                            <input type="text" name="name" required class="form-control" placeholder="e.g. White PLA 3D Printer Filament 1.75mm">
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div class="form-group">
                                <label class="form-label">Category *</label>
                                <input type="text" name="category" required class="form-control" placeholder="e.g. 3D Printing, Woodworking, Fasteners" list="cat-suggestions">
                                <datalist id="cat-suggestions">
                                    {''.join([f'<option value="{escape_html(c)}">' for c in all_categories])}
                                </datalist>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Measurement Unit *</label>
                                <input type="text" name="unit" value="pcs" required class="form-control" placeholder="e.g. pcs, kg, spool, sheet, m">
                            </div>
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div class="form-group">
                                <label class="form-label">Unit Cost (cents) *</label>
                                <input type="number" name="unit_cost_cents" value="0" min="0" required class="form-control" placeholder="1800 for €18.00">
                                <small class="form-hint">Cost per unit represented in integer cents.</small>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Minimum Stock Alert Level</label>
                                <input type="number" name="minimum_stock" value="0" min="0" class="form-control" placeholder="Alert threshold">
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Storage Location</label>
                            <input type="text" name="location" class="form-control" placeholder="e.g. Cabinet C, Shelf 2">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Description</label>
                            <textarea name="description" class="form-control" rows="2" placeholder="Specifications, vendor part number, safety notes..."></textarea>
                        </div>
                        <div class="form-group" style="background: #f8fafc; padding: 0.75rem; border-radius: var(--radius-md); border: 1px dashed var(--color-border);">
                            <label class="form-label" style="font-weight: 600;">Initial Stock Quantity (optional)</label>
                            <input type="number" name="initial_stock" value="0" min="0" class="form-control">
                            <small class="form-hint">If &gt; 0, automatically appends an initial stock receipt to the ledger.</small>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" onclick="closeModal('modal-new-item')">Cancel</button>
                        <button type="submit" class="btn btn-primary">Create Item</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Modal: Quick Receipt -->
        <div id="modal-receipt" class="modal-overlay" style="display:none;">
            <div class="modal-dialog">
                <div class="modal-header">
                    <h3>Record Stock Delivery / Receipt</h3>
                    <button type="button" class="btn-close" onclick="closeModal('modal-receipt')">&times;</button>
                </div>
                <form action="/inventory/movements/receipt" method="POST">
                    {csrf_field}
                    <input type="hidden" id="receipt_item_id" name="item_id" value="">
                    <div class="modal-body">
                        <div class="form-group">
                            <label class="form-label">Item</label>
                            <input type="text" id="receipt_item_name" readonly class="form-control" style="background: #f1f5f9; font-weight: 600;">
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div class="form-group">
                                <label class="form-label">Quantity Received *</label>
                                <input type="number" name="quantity" min="1" required class="form-control" placeholder="Quantity">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Unit Cost in Cents (optional)</label>
                                <input type="number" name="unit_cost_cents" min="0" class="form-control" placeholder="Leave empty for catalog default">
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Reference ID (e.g. PO / Invoice #)</label>
                            <input type="text" name="reference_id" class="form-control" placeholder="e.g. PO-2026-0881">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Receipt Reason / Notes *</label>
                            <input type="text" name="reason" required class="form-control" value="Stock receipt from supplier" placeholder="Mandatory explanation">
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" onclick="closeModal('modal-receipt')">Cancel</button>
                        <button type="submit" class="btn btn-success">Confirm Stock Receipt</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Modal: Quick Consume -->
        <div id="modal-consume" class="modal-overlay" style="display:none;">
            <div class="modal-dialog">
                <div class="modal-header">
                    <h3>Record Material Consumption</h3>
                    <button type="button" class="btn-close" onclick="closeModal('modal-consume')">&times;</button>
                </div>
                <form action="/inventory/movements/consume" method="POST">
                    {csrf_field}
                    <input type="hidden" id="consume_item_id" name="item_id" value="">
                    <div class="modal-body">
                        <div class="form-group">
                            <label class="form-label">Item</label>
                            <input type="text" id="consume_item_name" readonly class="form-control" style="background: #f1f5f9; font-weight: 600;">
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div class="form-group">
                                <label class="form-label">Quantity to Consume *</label>
                                <input type="number" id="consume_quantity" name="quantity" min="1" required class="form-control">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Reference Type</label>
                                <select name="reference_type" class="form-control">
                                    <option value="manual">Manual / Walk-In</option>
                                    <option value="reservation">Reservation</option>
                                    <option value="maintenance">Maintenance</option>
                                </select>
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Reference ID (e.g. Reservation ID / Job ID)</label>
                            <input type="text" name="reference_id" class="form-control" placeholder="e.g. Booking #42">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Consumption Reason *</label>
                            <input type="text" name="reason" required class="form-control" placeholder="e.g. Used for laser cut architectural model">
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" onclick="closeModal('modal-consume')">Cancel</button>
                        <button type="submit" class="btn btn-danger">Confirm Consumption</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Modal: Quick Movement (General) -->
        <div id="modal-quick-movement" class="modal-overlay" style="display:none;">
            <div class="modal-dialog">
                <div class="modal-header">
                    <h3>Record General Inventory Movement</h3>
                    <button type="button" class="btn-close" onclick="closeModal('modal-quick-movement')">&times;</button>
                </div>
                <form id="form-general-movement" action="/inventory/movements/receipt" method="POST" onsubmit="return handleGeneralMovementSubmit(this)">
                    {csrf_field}
                    <div class="modal-body">
                        <div class="form-group">
                            <label class="form-label">Movement Type *</label>
                            <select id="gen_mov_type" class="form-control" onchange="updateGeneralMovementForm(this.value)">
                                <option value="receipt">Receipt (+ Stock delivery)</option>
                                <option value="reserve">Reservation (Hold stock for booking)</option>
                                <option value="consume">Consumption (- Physical usage)</option>
                                <option value="release">Release (Free held reservation)</option>
                                <option value="correct">Correction (+/- Motivated inventory reconciliation)</option>
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Target Item *</label>
                            <select name="item_id" required class="form-control">
                                {''.join([f'<option value="{it["id"]}">{escape_html(it["sku"])} — {escape_html(it["name"])} (Avail: {it["available"]})</option>' for it in items])}
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label" id="gen_qty_label">Quantity *</label>
                            <input type="number" id="gen_qty_input" name="quantity" required class="form-control" placeholder="Amount">
                            <small id="gen_qty_hint" class="form-hint">Must be a strictly positive integer.</small>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Reference Type</label>
                            <select name="reference_type" class="form-control">
                                <option value="manual">Manual</option>
                                <option value="reservation">Reservation</option>
                                <option value="maintenance">Maintenance</option>
                                <option value="adjustment">Adjustment</option>
                                <option value="receipt">Receipt</option>
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Reference ID (optional)</label>
                            <input type="text" name="reference_id" class="form-control" placeholder="e.g. RES-101 or MNT-44">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Mandatory Motivation / Reason *</label>
                            <input type="text" name="reason" required class="form-control" placeholder="Detailed motivation for ledger audit trail">
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" onclick="closeModal('modal-quick-movement')">Cancel</button>
                        <button type="submit" class="btn btn-primary">Submit Ledger Movement</button>
                    </div>
                </form>
            </div>
        </div>

        <script>
        function openReceiptModal(id, sku, name, unit) {{
            document.getElementById('receipt_item_id').value = id;
            document.getElementById('receipt_item_name').value = sku + ' (' + name + ')';
            openModal('modal-receipt');
        }}

        function openConsumeModal(id, sku, name, unit, avail) {{
            document.getElementById('consume_item_id').value = id;
            document.getElementById('consume_item_name').value = sku + ' (' + name + ') - Avail: ' + avail + ' ' + unit;
            document.getElementById('consume_quantity').max = avail > 0 ? avail : 1;
            openModal('modal-consume');
        }}

        function updateGeneralMovementForm(type) {{
            const form = document.getElementById('form-general-movement');
            const qtyLabel = document.getElementById('gen_qty_label');
            const qtyHint = document.getElementById('gen_qty_hint');
            const qtyInput = document.getElementById('gen_qty_input');

            if (type === 'receipt') {{
                form.action = '/inventory/movements/receipt';
                qtyLabel.textContent = 'Quantity Received *';
                qtyInput.name = 'quantity';
                qtyInput.min = '1';
                qtyHint.textContent = 'Must be a strictly positive integer (> 0).';
            }} else if (type === 'reserve') {{
                form.action = '/inventory/movements/reserve';
                qtyLabel.textContent = 'Quantity to Reserve *';
                qtyInput.name = 'quantity';
                qtyInput.min = '1';
                qtyHint.textContent = 'Must be a strictly positive integer (> 0) <= available stock.';
            }} else if (type === 'consume') {{
                form.action = '/inventory/movements/consume';
                qtyLabel.textContent = 'Quantity to Consume *';
                qtyInput.name = 'quantity';
                qtyInput.min = '1';
                qtyHint.textContent = 'Must be a strictly positive integer (> 0).';
            }} else if (type === 'release') {{
                form.action = '/inventory/movements/release';
                qtyLabel.textContent = 'Quantity to Release *';
                qtyInput.name = 'quantity';
                qtyInput.min = '1';
                qtyHint.textContent = 'Releases previously reserved stock back to available pool.';
            }} else if (type === 'correct') {{
                form.action = '/inventory/movements/correct';
                qtyLabel.textContent = 'Correction Delta (+/-) *';
                qtyInput.name = 'quantity_delta';
                qtyInput.removeAttribute('min');
                qtyHint.textContent = 'Can be positive (+found stock) or negative (-lost/damaged stock). Non-zero.';
            }}
        }}

        function handleGeneralMovementSubmit(form) {{
            return true;
        }}
        </script>
        """

        html_out = render_page(
            title="Inventory Ledger",
            content_html=content,
            user=user,
            flash_messages=flash_messages,
            active_nav="inventory",
            csrf_token=csrf_token,
        )
        return Response.html(html_out)

    @router.get("/inventory/{id}")
    @require_auth
    @require_permission(PERM_INVENTORY_VIEW)
    def inventory_item_detail_view(req: Request) -> Response:
        """Render individual inventory item breakdown, active holds, and full ledger history."""
        user = req.user
        can_manage = has_permission(user, PERM_INVENTORY_MANAGE)
        can_consume = has_permission(user, PERM_INVENTORY_CONSUME)

        item_id_raw = req.route_params.get("id", "")
        try:
            item_id = int(item_id_raw)
        except ValueError:
            return Response.redirect("/inventory?error=Invalid+inventory+item+ID")

        item = get_item_stock_summary(item_id)
        if not item:
            return Response.redirect("/inventory?error=Inventory+item+not+found")

        holds = get_item_active_holds(item_id)
        ledger_entries, total_ledger_count = get_ledger_history(item_id=item_id, limit=200)

        csrf_token = getattr(req, "csrf_token", "")
        csrf_field = csrf_input(csrf_token)

        success_msg = req.query("success")
        error_msg = req.query("error")
        flash_messages = []
        if success_msg:
            flash_messages.append({"category": "success", "text": success_msg})
        if error_msg:
            flash_messages.append({"category": "danger", "text": error_msg})

        # Holds table
        holds_rows = []
        if not holds:
            holds_rows.append('<tr><td colspan="5" style="text-align: center; color: var(--color-text-muted); padding: 1rem;">No active reservations or holds for this item.</td></tr>')
        else:
            for h in holds:
                holds_rows.append(f"""
                <tr>
                    <td><span class="role-badge" style="background: #fef3c7; color: #92400e;">{escape_html(h['reference_type'].upper())}</span></td>
                    <td><strong>{escape_html(h['reference_id'] or 'General')}</strong></td>
                    <td style="font-family: var(--font-mono); text-align: center;">{h['reserved_quantity']}</td>
                    <td style="font-family: var(--font-mono); text-align: center;">{h['released_quantity'] + h['consumed_quantity']}</td>
                    <td style="font-family: var(--font-mono); font-weight: 700; text-align: center; color: #d97706;">{h['active_hold_quantity']} {escape_html(item['unit'])}</td>
                </tr>
                """)
        holds_table_html = "\n".join(holds_rows)

        # Ledger history table
        ledger_rows = []
        if not ledger_entries:
            ledger_rows.append('<tr><td colspan="7" style="text-align: center; color: var(--color-text-muted); padding: 1.5rem;">No movements in ledger.</td></tr>')
        else:
            for l in ledger_entries:
                m_type = l["movement_type"]
                qty = l["quantity"]
                qty_str = f"+{qty}" if qty > 0 else str(qty)
                m_badge_class = {
                    "receipt": "background: #dcfce7; color: #166534;",
                    "reservation": "background: #fef3c7; color: #92400e;",
                    "consumption": "background: #fee2e2; color: #991b1b;",
                    "release": "background: #e0f2fe; color: #075985;",
                    "correction": "background: #f3e8ff; color: #6b21a8;",
                }.get(m_type, "background: #f1f5f9; color: #334155;")

                time_display = format_display(l["created_at"]) if l.get("created_at") else ""
                ref_str = f"{l['reference_type']}: {l['reference_id']}" if l.get("reference_id") else l["reference_type"]

                ledger_rows.append(f"""
                <tr>
                    <td style="font-family: var(--font-mono); font-size: 0.85rem;">#{l['id']}</td>
                    <td style="font-size: 0.85rem; color: var(--color-text-muted);">{escape_html(time_display)}</td>
                    <td><span class="role-badge" style="{m_badge_class}">{escape_html(m_type.upper())}</span></td>
                    <td style="font-family: var(--font-mono); font-weight: 700; text-align: center;">{qty_str} {escape_html(item['unit'])}</td>
                    <td><small style="color: var(--color-text-muted);">{escape_html(ref_str)}</small></td>
                    <td><small style="color: var(--color-text-muted);">{escape_html(l.get('actor_name') or 'System')}</small></td>
                    <td>{escape_html(l['reason'])}</td>
                </tr>
                """)
        ledger_table_html = "\n".join(ledger_rows)

        sku_esc = escape_html(item["sku"])
        name_esc = escape_html(item["name"])
        cat_esc = escape_html(item["category"])
        unit_esc = escape_html(item["unit"])
        loc_esc = escape_html(item.get("location") or "Not assigned")
        desc_esc = escape_html(item.get("description") or "No description provided.")

        content = f"""
        <div style="margin-bottom: 1rem;">
            <a href="/inventory" style="color: var(--color-primary); text-decoration: none; font-weight: 500;">&larr; Back to Inventory Catalog</a>
        </div>

        <div class="metrics-row">
            <div class="metric-card">
                <span class="metric-label">Physical On-Hand Stock</span>
                <span class="metric-value">{item['on_hand']} <small style="font-size: 1rem; font-weight: 400;">{unit_esc}</small></span>
                <span class="metric-subtext">Total Physical Inventory</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Reserved Commitments</span>
                <span class="metric-value" style="color: #d97706;">{item['reserved']} <small style="font-size: 1rem; font-weight: 400;">{unit_esc}</small></span>
                <span class="metric-subtext">Allocated to Active Jobs</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Net Available Stock</span>
                <span class="metric-value" style="color: {'#dc2626' if item['available'] == 0 else ('#d97706' if item['is_low_stock'] else '#16a34a')};">{item['available']} <small style="font-size: 1rem; font-weight: 400;">{unit_esc}</small></span>
                <span class="metric-subtext">Free for New Bookings</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Total Valuation</span>
                <span class="metric-value">{item['total_value_formatted']}</span>
                <span class="metric-subtext">At {item['unit_cost_formatted']} / {unit_esc}</span>
            </div>
        </div>

        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem;">
            <!-- Item Information -->
            <div class="card">
                <div class="card-header">
                    <div style="display: flex; align-items: center; gap: 0.75rem;">
                        <h2 class="card-title">{name_esc}</h2>
                        {'<span class="role-badge role-admin" style="font-size: 0.7rem;">LOW STOCK</span>' if item['is_low_stock'] else ''}
                    </div>
                    {f'<button type="button" class="btn btn-sm btn-outline-secondary" onclick="openModal(\'modal-edit-item\')">Edit Item</button>' if can_manage else ''}
                </div>
                <div class="card-body">
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem;">
                        <div>
                            <span style="font-size: 0.8rem; color: var(--color-text-muted); text-transform: uppercase;">SKU Identifier</span>
                            <div style="font-family: var(--font-mono); font-weight: 700; font-size: 1.1rem; color: var(--color-primary);">{sku_esc}</div>
                        </div>
                        <div>
                            <span style="font-size: 0.8rem; color: var(--color-text-muted); text-transform: uppercase;">Category</span>
                            <div><span class="role-badge" style="background: #f1f5f9; color: #475569;">{cat_esc}</span></div>
                        </div>
                        <div>
                            <span style="font-size: 0.8rem; color: var(--color-text-muted); text-transform: uppercase;">Unit Cost</span>
                            <div style="font-family: var(--font-mono); font-weight: 600;">{item['unit_cost_formatted']}</div>
                        </div>
                        <div>
                            <span style="font-size: 0.8rem; color: var(--color-text-muted); text-transform: uppercase;">Minimum Stock Level</span>
                            <div style="font-family: var(--font-mono); font-weight: 600;">{item['minimum_stock']} {unit_esc}</div>
                        </div>
                        <div>
                            <span style="font-size: 0.8rem; color: var(--color-text-muted); text-transform: uppercase;">Storage Location</span>
                            <div>{loc_esc}</div>
                        </div>
                        <div>
                            <span style="font-size: 0.8rem; color: var(--color-text-muted); text-transform: uppercase;">Ledger Movements</span>
                            <div>{item['movement_count']} total entries</div>
                        </div>
                    </div>
                    <div style="border-top: 1px solid var(--color-border); padding-top: 0.75rem;">
                        <span style="font-size: 0.8rem; color: var(--color-text-muted); text-transform: uppercase;">Description / Notes</span>
                        <p style="margin-top: 0.25rem; font-size: 0.95rem;">{desc_esc}</p>
                    </div>
                </div>
            </div>

            <!-- Quick Actions & Active Holds -->
            <div class="card">
                <div class="card-header">
                    <h3 class="card-title">Stock Actions & Active Holds</h3>
                </div>
                <div class="card-body">
                    <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1.5rem;">
                        {f'<button type="button" class="btn btn-success btn-sm" onclick="openModal(\'modal-receipt\')">+ Record Receipt</button>' if can_manage else ''}
                        {f'<button type="button" class="btn btn-warning btn-sm" onclick="openModal(\'modal-reserve\')">Reserve Material</button>' if can_consume else ''}
                        {f'<button type="button" class="btn btn-danger btn-sm" onclick="openModal(\'modal-consume\')">- Record Consumption</button>' if can_consume else ''}
                        {f'<button type="button" class="btn btn-info btn-sm" onclick="openModal(\'modal-release\')">Release Reservation</button>' if can_manage else ''}
                        {f'<button type="button" class="btn btn-secondary btn-sm" onclick="openModal(\'modal-correct\')">Manual Correction</button>' if can_manage else ''}
                    </div>

                    <h4 style="font-size: 0.95rem; font-weight: 700; margin-bottom: 0.5rem;">Active Reservation Holds</h4>
                    <div class="table-responsive">
                        <table class="table" style="font-size: 0.85rem;">
                            <thead>
                                <tr>
                                    <th>Ref Type</th>
                                    <th>Ref ID</th>
                                    <th style="text-align: center;">Reserved</th>
                                    <th style="text-align: center;">Used/Rel</th>
                                    <th style="text-align: center;">Active Hold</th>
                                </tr>
                            </thead>
                            <tbody>
                                {holds_table_html}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>

        <!-- Full Immutable Ledger Table -->
        <div class="card">
            <div class="card-header">
                <div style="display: flex; align-items: center; gap: 1rem;">
                    <h3 class="card-title">Complete Immutable Movement Ledger</h3>
                    <span class="tz-badge">{total_ledger_count} Verified Entries</span>
                </div>
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table">
                        <thead>
                            <tr>
                                <th>#</th>
                                <th>Timestamp</th>
                                <th>Movement</th>
                                <th style="text-align: center;">Quantity Delta</th>
                                <th>Reference</th>
                                <th>Actor</th>
                                <th>Reason / Motivation</th>
                            </tr>
                        </thead>
                        <tbody>
                            {ledger_table_html}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Modal: Edit Item -->
        <div id="modal-edit-item" class="modal-overlay" style="display:none;">
            <div class="modal-dialog">
                <div class="modal-header">
                    <h3>Edit Item: {sku_esc}</h3>
                    <button type="button" class="btn-close" onclick="closeModal('modal-edit-item')">&times;</button>
                </div>
                <form action="/inventory/items/{item_id}/edit" method="POST">
                    {csrf_field}
                    <div class="modal-body">
                        <div class="form-group">
                            <label class="form-label">Item Name *</label>
                            <input type="text" name="name" value="{name_esc}" required class="form-control">
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div class="form-group">
                                <label class="form-label">Category *</label>
                                <input type="text" name="category" value="{cat_esc}" required class="form-control">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Measurement Unit *</label>
                                <input type="text" name="unit" value="{unit_esc}" required class="form-control">
                            </div>
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div class="form-group">
                                <label class="form-label">Unit Cost (cents) *</label>
                                <input type="number" name="unit_cost_cents" value="{item['unit_cost_cents']}" min="0" required class="form-control">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Minimum Stock Alert Level</label>
                                <input type="number" name="minimum_stock" value="{item['minimum_stock']}" min="0" class="form-control">
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Storage Location</label>
                            <input type="text" name="location" value="{loc_esc}" class="form-control">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Description</label>
                            <textarea name="description" class="form-control" rows="2">{desc_esc}</textarea>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" onclick="closeModal('modal-edit-item')">Cancel</button>
                        <button type="submit" class="btn btn-primary">Save Changes</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Modal: Receipt for this item -->
        <div id="modal-receipt" class="modal-overlay" style="display:none;">
            <div class="modal-dialog">
                <div class="modal-header">
                    <h3>Record Stock Delivery: {sku_esc}</h3>
                    <button type="button" class="btn-close" onclick="closeModal('modal-receipt')">&times;</button>
                </div>
                <form action="/inventory/movements/receipt" method="POST">
                    {csrf_field}
                    <input type="hidden" name="item_id" value="{item_id}">
                    <div class="modal-body">
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div class="form-group">
                                <label class="form-label">Quantity Received ({unit_esc}) *</label>
                                <input type="number" name="quantity" min="1" required class="form-control">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Unit Cost in Cents (optional)</label>
                                <input type="number" name="unit_cost_cents" value="{item['unit_cost_cents']}" min="0" class="form-control">
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Reference ID (PO / Delivery Note #)</label>
                            <input type="text" name="reference_id" class="form-control" placeholder="e.g. REC-2026-099">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Reason / Delivery Note *</label>
                            <input type="text" name="reason" value="Warehouse stock delivery" required class="form-control">
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" onclick="closeModal('modal-receipt')">Cancel</button>
                        <button type="submit" class="btn btn-success">Confirm Receipt</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Modal: Reserve for this item -->
        <div id="modal-reserve" class="modal-overlay" style="display:none;">
            <div class="modal-dialog">
                <div class="modal-header">
                    <h3>Reserve Material: {sku_esc}</h3>
                    <button type="button" class="btn-close" onclick="closeModal('modal-reserve')">&times;</button>
                </div>
                <form action="/inventory/movements/reserve" method="POST">
                    {csrf_field}
                    <input type="hidden" name="item_id" value="{item_id}">
                    <div class="modal-body">
                        <p style="font-size: 0.9rem; color: var(--color-text-muted); margin-bottom: 1rem;">
                            Currently available to reserve: <strong>{item['available']} {unit_esc}</strong>.
                        </p>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div class="form-group">
                                <label class="form-label">Quantity to Reserve *</label>
                                <input type="number" name="quantity" min="1" max="{item['available'] if item['available'] > 0 else 1}" required class="form-control">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Reference Type</label>
                                <select name="reference_type" class="form-control">
                                    <option value="reservation">Reservation</option>
                                    <option value="maintenance">Maintenance</option>
                                    <option value="manual">Manual Project Hold</option>
                                </select>
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Reference ID (e.g. Reservation ID #) *</label>
                            <input type="text" name="reference_id" required class="form-control" placeholder="e.g. RES-42">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Reservation Purpose *</label>
                            <input type="text" name="reason" required class="form-control" placeholder="e.g. Reserved for CNC milled enclosure">
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" onclick="closeModal('modal-reserve')">Cancel</button>
                        <button type="submit" class="btn btn-warning">Confirm Reservation</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Modal: Consume for this item -->
        <div id="modal-consume" class="modal-overlay" style="display:none;">
            <div class="modal-dialog">
                <div class="modal-header">
                    <h3>Record Material Consumption: {sku_esc}</h3>
                    <button type="button" class="btn-close" onclick="closeModal('modal-consume')">&times;</button>
                </div>
                <form action="/inventory/movements/consume" method="POST">
                    {csrf_field}
                    <input type="hidden" name="item_id" value="{item_id}">
                    <div class="modal-body">
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div class="form-group">
                                <label class="form-label">Quantity Consumed ({unit_esc}) *</label>
                                <input type="number" name="quantity" min="1" max="{item['on_hand'] if item['on_hand'] > 0 else 1}" required class="form-control">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Reference Type</label>
                                <select name="reference_type" class="form-control">
                                    <option value="manual">Walk-in / Direct</option>
                                    <option value="reservation">Reservation</option>
                                    <option value="maintenance">Maintenance</option>
                                </select>
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Reference ID (if from reservation/job)</label>
                            <input type="text" name="reference_id" class="form-control" placeholder="e.g. RES-42">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Consumption Reason *</label>
                            <input type="text" name="reason" required class="form-control" placeholder="e.g. Consumed for robot chassis print">
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" onclick="closeModal('modal-consume')">Cancel</button>
                        <button type="submit" class="btn btn-danger">Confirm Consumption</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Modal: Release for this item -->
        <div id="modal-release" class="modal-overlay" style="display:none;">
            <div class="modal-dialog">
                <div class="modal-header">
                    <h3>Release Reserved Stock: {sku_esc}</h3>
                    <button type="button" class="btn-close" onclick="closeModal('modal-release')">&times;</button>
                </div>
                <form action="/inventory/movements/release" method="POST">
                    {csrf_field}
                    <input type="hidden" name="item_id" value="{item_id}">
                    <div class="modal-body">
                        <p style="font-size: 0.9rem; color: var(--color-text-muted); margin-bottom: 1rem;">
                            Currently reserved: <strong>{item['reserved']} {unit_esc}</strong>.
                        </p>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div class="form-group">
                                <label class="form-label">Quantity to Release *</label>
                                <input type="number" name="quantity" min="1" max="{item['reserved'] if item['reserved'] > 0 else 1}" required class="form-control">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Reference Type</label>
                                <select name="reference_type" class="form-control">
                                    <option value="reservation">Reservation</option>
                                    <option value="maintenance">Maintenance</option>
                                    <option value="manual">Manual Hold</option>
                                </select>
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Reference ID (e.g. Booking #)</label>
                            <input type="text" name="reference_id" class="form-control" placeholder="e.g. RES-42">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Release Reason *</label>
                            <input type="text" name="reason" value="Cancelled or unused reservation release" required class="form-control">
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" onclick="closeModal('modal-release')">Cancel</button>
                        <button type="submit" class="btn btn-info">Confirm Release</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Modal: Correction for this item -->
        <div id="modal-correct" class="modal-overlay" style="display:none;">
            <div class="modal-dialog">
                <div class="modal-header">
                    <h3>Manual Inventory Correction: {sku_esc}</h3>
                    <button type="button" class="btn-close" onclick="closeModal('modal-correct')">&times;</button>
                </div>
                <form action="/inventory/movements/correct" method="POST">
                    {csrf_field}
                    <input type="hidden" name="item_id" value="{item_id}">
                    <div class="modal-body">
                        <p style="font-size: 0.9rem; color: var(--color-text-muted); margin-bottom: 1rem;">
                            Current on-hand: <strong>{item['on_hand']} {unit_esc}</strong> | Available: <strong>{item['available']} {unit_esc}</strong>.
                        </p>
                        <div class="form-group">
                            <label class="form-label">Quantity Delta (+/-) *</label>
                            <input type="number" name="quantity_delta" required class="form-control" placeholder="e.g. -2 for damaged/discarded, +3 for found stock">
                            <small class="form-hint">Enter positive integer to add units or negative integer to deduct units.</small>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Mandatory Motivation / Reason *</label>
                            <textarea name="reason" required class="form-control" rows="3" placeholder="Explain discrepancy, damage report, or stocktake recount justification..."></textarea>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" onclick="closeModal('modal-correct')">Cancel</button>
                        <button type="submit" class="btn btn-secondary">Record Stock Correction</button>
                    </div>
                </form>
            </div>
        </div>
        """

        html_out = render_page(
            title=f"Inventory: {item['sku']}",
            content_html=content,
            user=user,
            flash_messages=flash_messages,
            active_nav="inventory",
            csrf_token=csrf_token,
        )
        return Response.html(html_out)

    # =========================================================================
    # HTML Form Action Handlers (POST)
    # =========================================================================

    @router.post("/inventory/items/new")
    @require_auth
    @require_permission(PERM_INVENTORY_MANAGE)
    def handle_create_item_form(req: Request) -> Response:
        """Handle HTML form submission for creating a new inventory item."""
        sku = req.form_value("sku", "")
        name = req.form_value("name", "")
        category = req.form_value("category", "")
        unit = req.form_value("unit", "pcs")
        cost_cents = req.form_value("unit_cost_cents", "0")
        min_stock = req.form_value("minimum_stock", "0")
        location = req.form_value("location", "")
        description = req.form_value("description", "")
        init_stock = req.form_value("initial_stock", "0")

        try:
            item = create_inventory_item(
                sku=sku,
                name=name,
                category=category,
                unit=unit,
                unit_cost_cents=int(cost_cents or 0),
                minimum_stock=int(min_stock or 0),
                location=location,
                description=description,
                initial_stock=int(init_stock or 0),
                actor=req.user,
            )
            msg = urllib.parse.quote_plus(f"Inventory item '{item['sku']}' created successfully.")
            return Response.redirect(f"/inventory/{item['id']}?success={msg}")
        except Exception as e:
            logger.warning("Error creating inventory item: %s", e)
            msg = urllib.parse.quote_plus(str(e))
            return Response.redirect(f"/inventory?error={msg}")

    @router.post("/inventory/items/{id}/edit")
    @require_auth
    @require_permission(PERM_INVENTORY_MANAGE)
    def handle_edit_item_form(req: Request) -> Response:
        """Handle HTML form submission for updating an inventory item."""
        item_id_raw = req.route_params.get("id", "")
        try:
            item_id = int(item_id_raw)
        except ValueError:
            return Response.redirect("/inventory?error=Invalid+inventory+item+ID")

        name = req.form_value("name")
        category = req.form_value("category")
        unit = req.form_value("unit")
        cost_cents = req.form_value("unit_cost_cents")
        min_stock = req.form_value("minimum_stock")
        location = req.form_value("location")
        description = req.form_value("description")

        try:
            item = update_inventory_item(
                item_id=item_id,
                name=name,
                category=category,
                unit=unit,
                unit_cost_cents=int(cost_cents) if cost_cents is not None and cost_cents != "" else None,
                minimum_stock=int(min_stock) if min_stock is not None and min_stock != "" else None,
                location=location,
                description=description,
                actor=req.user,
            )
            msg = urllib.parse.quote_plus(f"Inventory item '{item['sku']}' updated successfully.")
            return Response.redirect(f"/inventory/{item['id']}?success={msg}")
        except Exception as e:
            logger.warning("Error updating inventory item ID %d: %s", item_id, e)
            msg = urllib.parse.quote_plus(str(e))
            return Response.redirect(f"/inventory/{item_id}?error={msg}")

    @router.post("/inventory/movements/receipt")
    @require_auth
    @require_permission(PERM_INVENTORY_MANAGE)
    def handle_receipt_form(req: Request) -> Response:
        """Handle HTML form submission for stock receipt."""
        item_id_raw = req.form_value("item_id", "")
        qty_raw = req.form_value("quantity", "")
        cost_raw = req.form_value("unit_cost_cents", "")
        ref_type = req.form_value("reference_type", "receipt")
        ref_id = req.form_value("reference_id", "")
        reason = req.form_value("reason", "Stock receipt")

        try:
            item_id = int(item_id_raw)
            qty = int(qty_raw)
            cost = int(cost_raw) if cost_raw and cost_raw.strip() else None
            res = record_receipt(
                item_id=item_id,
                quantity=qty,
                unit_cost_cents=cost,
                reference_type=ref_type,
                reference_id=ref_id if ref_id else None,
                reason=reason,
                actor=req.user,
            )
            msg = urllib.parse.quote_plus(f"Recorded receipt of {qty} units for '{res['sku']}'.")
            return Response.redirect(f"/inventory/{item_id}?success={msg}")
        except Exception as e:
            logger.warning("Error recording receipt: %s", e)
            msg = urllib.parse.quote_plus(str(e))
            redir = f"/inventory/{item_id_raw}" if item_id_raw.isdigit() else "/inventory"
            return Response.redirect(f"{redir}?error={msg}")

    @router.post("/inventory/movements/reserve")
    @require_auth
    def handle_reserve_form(req: Request) -> Response:
        """Handle HTML form submission for stock reservation."""
        if not (has_permission(req.user, PERM_INVENTORY_CONSUME) or has_permission(req.user, PERM_INVENTORY_MANAGE)):
            return Response.redirect("/inventory?error=Unauthorized+permission+denied")

        item_id_raw = req.form_value("item_id", "")
        qty_raw = req.form_value("quantity", "")
        ref_type = req.form_value("reference_type", "reservation")
        ref_id = req.form_value("reference_id", "")
        reason = req.form_value("reason", "Reserved for project")

        try:
            item_id = int(item_id_raw)
            qty = int(qty_raw)
            res = record_reservation(
                item_id=item_id,
                quantity=qty,
                reference_type=ref_type,
                reference_id=ref_id if ref_id else None,
                reason=reason,
                actor=req.user,
            )
            msg = urllib.parse.quote_plus(f"Reserved {qty} units of '{res['sku']}' for '{ref_id or 'booking'}'.")
            return Response.redirect(f"/inventory/{item_id}?success={msg}")
        except Exception as e:
            logger.warning("Error recording reservation: %s", e)
            msg = urllib.parse.quote_plus(str(e))
            redir = f"/inventory/{item_id_raw}" if item_id_raw.isdigit() else "/inventory"
            return Response.redirect(f"{redir}?error={msg}")

    @router.post("/inventory/movements/consume")
    @require_auth
    def handle_consume_form(req: Request) -> Response:
        """Handle HTML form submission for stock consumption."""
        if not (has_permission(req.user, PERM_INVENTORY_CONSUME) or has_permission(req.user, PERM_INVENTORY_MANAGE)):
            return Response.redirect("/inventory?error=Unauthorized+permission+denied")

        item_id_raw = req.form_value("item_id", "")
        qty_raw = req.form_value("quantity", "")
        ref_type = req.form_value("reference_type", "manual")
        ref_id = req.form_value("reference_id", "")
        reason = req.form_value("reason", "Material consumed")

        try:
            item_id = int(item_id_raw)
            qty = int(qty_raw)
            res = record_consumption(
                item_id=item_id,
                quantity=qty,
                reference_type=ref_type,
                reference_id=ref_id if ref_id else None,
                reason=reason,
                actor=req.user,
            )
            msg = urllib.parse.quote_plus(f"Recorded consumption of {qty} units of '{res['sku']}'.")
            return Response.redirect(f"/inventory/{item_id}?success={msg}")
        except Exception as e:
            logger.warning("Error recording consumption: %s", e)
            msg = urllib.parse.quote_plus(str(e))
            redir = f"/inventory/{item_id_raw}" if item_id_raw.isdigit() else "/inventory"
            return Response.redirect(f"{redir}?error={msg}")

    @router.post("/inventory/movements/release")
    @require_auth
    @require_permission(PERM_INVENTORY_MANAGE)
    def handle_release_form(req: Request) -> Response:
        """Handle HTML form submission for releasing reserved stock."""
        item_id_raw = req.form_value("item_id", "")
        qty_raw = req.form_value("quantity", "")
        ref_type = req.form_value("reference_type", "reservation")
        ref_id = req.form_value("reference_id", "")
        reason = req.form_value("reason", "Released unused reservation")

        try:
            item_id = int(item_id_raw)
            qty = int(qty_raw)
            res = record_release(
                item_id=item_id,
                quantity=qty,
                reference_type=ref_type,
                reference_id=ref_id if ref_id else None,
                reason=reason,
                actor=req.user,
            )
            msg = urllib.parse.quote_plus(f"Released {qty} reserved units of '{res['sku']}'.")
            return Response.redirect(f"/inventory/{item_id}?success={msg}")
        except Exception as e:
            logger.warning("Error recording release: %s", e)
            msg = urllib.parse.quote_plus(str(e))
            redir = f"/inventory/{item_id_raw}" if item_id_raw.isdigit() else "/inventory"
            return Response.redirect(f"{redir}?error={msg}")

    @router.post("/inventory/movements/correct")
    @require_auth
    @require_permission(PERM_INVENTORY_MANAGE)
    def handle_correct_form(req: Request) -> Response:
        """Handle HTML form submission for manual stock correction with mandatory motivation."""
        item_id_raw = req.form_value("item_id", "")
        delta_raw = req.form_value("quantity_delta", "")
        reason = req.form_value("reason", "")
        ref_type = req.form_value("reference_type", "adjustment")
        ref_id = req.form_value("reference_id", "")

        try:
            item_id = int(item_id_raw)
            delta = int(delta_raw)
            res = record_correction(
                item_id=item_id,
                quantity_delta=delta,
                reason=reason,
                reference_type=ref_type,
                reference_id=ref_id if ref_id else None,
                actor=req.user,
            )
            delta_sign = f"+{delta}" if delta > 0 else str(delta)
            msg = urllib.parse.quote_plus(f"Stock correction ({delta_sign}) recorded for '{res['sku']}'.")
            return Response.redirect(f"/inventory/{item_id}?success={msg}")
        except Exception as e:
            logger.warning("Error recording stock correction: %s", e)
            msg = urllib.parse.quote_plus(str(e))
            redir = f"/inventory/{item_id_raw}" if item_id_raw.isdigit() else "/inventory"
            return Response.redirect(f"{redir}?error={msg}")

    # =========================================================================
    # JSON API Routes
    # =========================================================================

    @router.get("/api/inventory/items")
    @require_auth
    @require_permission(PERM_INVENTORY_VIEW)
    def api_list_items(req: Request) -> Response:
        """JSON endpoint listing inventory items with derived stock."""
        category = req.query("category", "").strip() or None
        search = req.query("q", "").strip() or None
        low_stock = req.query("low_stock", "").strip() == "1"

        items = list_inventory_items_with_stock(
            category=category,
            search=search,
            low_stock_only=low_stock,
        )
        return Response.json({"success": True, "items": items, "count": len(items)})

    @router.get("/api/inventory/items/{id}")
    @require_auth
    @require_permission(PERM_INVENTORY_VIEW)
    def api_get_item(req: Request) -> Response:
        """JSON endpoint returning a single inventory item with derived stock summary."""
        item_id_raw = req.route_params.get("id", "")
        try:
            item_id = int(item_id_raw)
        except ValueError:
            return Response.json({"success": False, "error": "Invalid item ID"}, status_code=400)

        item = get_item_stock_summary(item_id)
        if not item:
            return Response.json({"success": False, "error": "Item not found"}, status_code=404)

        holds = get_item_active_holds(item_id)
        return Response.json({"success": True, "item": item, "active_holds": holds})

    @router.get("/api/inventory/items/{id}/stock")
    @require_auth
    @require_permission(PERM_INVENTORY_VIEW)
    def api_get_item_stock(req: Request) -> Response:
        """JSON endpoint returning stock balances for a given item ID."""
        item_id_raw = req.route_params.get("id", "")
        try:
            item_id = int(item_id_raw)
        except ValueError:
            return Response.json({"success": False, "error": "Invalid item ID"}, status_code=400)

        balances = compute_item_stock_balances(item_id)
        return Response.json({"success": True, "item_id": item_id, "stock": balances})

    @router.post("/api/inventory/items")
    @require_auth
    @require_permission(PERM_INVENTORY_MANAGE)
    def api_create_item(req: Request) -> Response:
        """JSON endpoint to create an inventory item."""
        data = req.json() or {}
        sku = data.get("sku") or req.form_value("sku", "")
        name = data.get("name") or req.form_value("name", "")
        category = data.get("category") or req.form_value("category", "")
        unit = data.get("unit") or req.form_value("unit", "pcs")
        cost = data.get("unit_cost_cents") or req.form_value("unit_cost_cents", 0)
        min_stock = data.get("minimum_stock") or req.form_value("minimum_stock", 0)
        loc = data.get("location") or req.form_value("location", "")
        desc = data.get("description") or req.form_value("description", "")
        init_stock = data.get("initial_stock") or req.form_value("initial_stock", 0)

        try:
            item = create_inventory_item(
                sku=sku,
                name=name,
                category=category,
                unit=unit,
                unit_cost_cents=int(cost),
                minimum_stock=int(min_stock),
                location=loc,
                description=desc,
                initial_stock=int(init_stock),
                actor=req.user,
            )
            return Response.json({"success": True, "item": item}, status_code=201)
        except Exception as e:
            return Response.json({"success": False, "error": str(e)}, status_code=400)

    @router.put("/api/inventory/items/{id}")
    @require_auth
    @require_permission(PERM_INVENTORY_MANAGE)
    def api_update_item(req: Request) -> Response:
        """JSON endpoint to update an inventory item."""
        item_id_raw = req.route_params.get("id", "")
        try:
            item_id = int(item_id_raw)
        except ValueError:
            return Response.json({"success": False, "error": "Invalid item ID"}, status_code=400)

        data = req.json() or {}
        try:
            item = update_inventory_item(
                item_id=item_id,
                name=data.get("name"),
                category=data.get("category"),
                unit=data.get("unit"),
                unit_cost_cents=int(data["unit_cost_cents"]) if "unit_cost_cents" in data else None,
                minimum_stock=int(data["minimum_stock"]) if "minimum_stock" in data else None,
                location=data.get("location"),
                description=data.get("description"),
                actor=req.user,
            )
            return Response.json({"success": True, "item": item})
        except Exception as e:
            return Response.json({"success": False, "error": str(e)}, status_code=400)

    @router.get("/api/inventory/ledger")
    @require_auth
    @require_permission(PERM_INVENTORY_VIEW)
    def api_get_ledger(req: Request) -> Response:
        """JSON endpoint to query immutable ledger history."""
        item_id_raw = req.query("item_id", "").strip()
        item_id = int(item_id_raw) if item_id_raw.isdigit() else None
        m_type = req.query("movement_type", "").strip() or None
        r_type = req.query("reference_type", "").strip() or None
        r_id = req.query("reference_id", "").strip() or None
        limit_raw = req.query("limit", "100").strip()
        offset_raw = req.query("offset", "0").strip()
        limit = int(limit_raw) if limit_raw.isdigit() else 100
        offset = int(offset_raw) if offset_raw.isdigit() else 0

        entries, total = get_ledger_history(
            item_id=item_id,
            movement_type=m_type,
            reference_type=r_type,
            reference_id=r_id,
            limit=limit,
            offset=offset,
        )
        return Response.json({
            "success": True,
            "entries": entries,
            "total_count": total,
            "limit": limit,
            "offset": offset,
        })

    @router.post("/api/inventory/receipt")
    @require_auth
    @require_permission(PERM_INVENTORY_MANAGE)
    def api_record_receipt(req: Request) -> Response:
        """JSON endpoint to record a stock receipt."""
        data = req.json() or {}
        try:
            res = record_receipt(
                item_id=int(data["item_id"]),
                quantity=int(data["quantity"]),
                unit_cost_cents=int(data["unit_cost_cents"]) if "unit_cost_cents" in data and data["unit_cost_cents"] is not None else None,
                reference_type=data.get("reference_type", "receipt"),
                reference_id=data.get("reference_id"),
                reason=data.get("reason", "Stock receipt"),
                actor=req.user,
            )
            return Response.json({"success": True, "movement": res}, status_code=201)
        except Exception as e:
            return Response.json({"success": False, "error": str(e)}, status_code=400)

    @router.post("/api/inventory/reserve")
    @require_auth
    def api_record_reservation(req: Request) -> Response:
        """JSON endpoint to reserve inventory stock."""
        if not (has_permission(req.user, PERM_INVENTORY_CONSUME) or has_permission(req.user, PERM_INVENTORY_MANAGE)):
            return Response.json({"success": False, "error": "Permission denied"}, status_code=403)

        data = req.json() or {}
        try:
            res = record_reservation(
                item_id=int(data["item_id"]),
                quantity=int(data["quantity"]),
                reference_type=data.get("reference_type", "reservation"),
                reference_id=data.get("reference_id"),
                reason=data.get("reason", "Reserved for project"),
                actor=req.user,
            )
            return Response.json({"success": True, "movement": res}, status_code=201)
        except Exception as e:
            return Response.json({"success": False, "error": str(e)}, status_code=400)

    @router.post("/api/inventory/consume")
    @require_auth
    def api_record_consumption(req: Request) -> Response:
        """JSON endpoint to record material consumption."""
        if not (has_permission(req.user, PERM_INVENTORY_CONSUME) or has_permission(req.user, PERM_INVENTORY_MANAGE)):
            return Response.json({"success": False, "error": "Permission denied"}, status_code=403)

        data = req.json() or {}
        try:
            res = record_consumption(
                item_id=int(data["item_id"]),
                quantity=int(data["quantity"]),
                reference_type=data.get("reference_type", "manual"),
                reference_id=data.get("reference_id"),
                reason=data.get("reason", "Material consumed"),
                actor=req.user,
            )
            return Response.json({"success": True, "movement": res}, status_code=201)
        except Exception as e:
            return Response.json({"success": False, "error": str(e)}, status_code=400)

    @router.post("/api/inventory/release")
    @require_auth
    @require_permission(PERM_INVENTORY_MANAGE)
    def api_record_release(req: Request) -> Response:
        """JSON endpoint to release reserved stock."""
        data = req.json() or {}
        try:
            res = record_release(
                item_id=int(data["item_id"]),
                quantity=int(data["quantity"]),
                reference_type=data.get("reference_type", "reservation"),
                reference_id=data.get("reference_id"),
                reason=data.get("reason", "Released unused reservation"),
                actor=req.user,
            )
            return Response.json({"success": True, "movement": res}, status_code=201)
        except Exception as e:
            return Response.json({"success": False, "error": str(e)}, status_code=400)

    @router.post("/api/inventory/correct")
    @require_auth
    @require_permission(PERM_INVENTORY_MANAGE)
    def api_record_correction(req: Request) -> Response:
        """JSON endpoint for manual stock correction with mandatory justification."""
        data = req.json() or {}
        try:
            res = record_correction(
                item_id=int(data["item_id"]),
                quantity_delta=int(data["quantity_delta"]),
                reason=data.get("reason", ""),
                reference_type=data.get("reference_type", "adjustment"),
                reference_id=data.get("reference_id"),
                actor=req.user,
            )
            return Response.json({"success": True, "movement": res}, status_code=201)
        except Exception as e:
            return Response.json({"success": False, "error": str(e)}, status_code=400)
