"""Inventory, Consumables, Replacement Parts, and Immutable Movement Ledger Service for ForgeDesk."""

import datetime
import json
import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union

from forgedesk.audit.service import record_audit_event
from forgedesk.db.connection import get_connection, query_all, query_one, transaction
from forgedesk.utils.datetime_tz import now_rome, now_rome_iso

logger = logging.getLogger("forgedesk.inventory.service")

VALID_MOVEMENT_TYPES = ("receipt", "reservation", "consumption", "release", "correction")
VALID_REFERENCE_TYPES = ("manual", "reservation", "maintenance", "adjustment", "initial_stock", "receipt")


# -----------------------------------------------------------------------------
# Input Validation Helpers
# -----------------------------------------------------------------------------

def validate_sku(sku: Optional[str]) -> str:
    """Validate and normalize inventory SKU code (e.g. FIL-PLA-BLK-1KG)."""
    if not sku or not sku.strip():
        raise ValueError("Inventory SKU cannot be empty.")
    clean = sku.strip().upper()
    if not re.match(r"^[A-Z0-9_-]{2,32}$", clean):
        raise ValueError(
            "Inventory SKU must be 2-32 characters and contain only uppercase letters, numbers, hyphens, and underscores."
        )
    return clean


def validate_item_name(name: Optional[str]) -> str:
    """Validate inventory item display name."""
    if not name or not name.strip():
        raise ValueError("Item name cannot be empty.")
    clean = name.strip()
    if len(clean) > 120:
        raise ValueError("Item name cannot exceed 120 characters.")
    return clean


def validate_category(category: Optional[str]) -> str:
    """Validate inventory item category."""
    if not category or not category.strip():
        raise ValueError("Category cannot be empty.")
    clean = category.strip()
    if len(clean) > 60:
        raise ValueError("Category name cannot exceed 60 characters.")
    return clean


def validate_unit(unit: Optional[str]) -> str:
    """Validate inventory measurement unit (pcs, kg, spool, sheet, m, etc.)."""
    if not unit or not unit.strip():
        return "pcs"
    clean = unit.strip().lower()
    if len(clean) > 20:
        raise ValueError("Unit of measure cannot exceed 20 characters.")
    return clean


def validate_cents(val: Any, field_name: str = "Unit cost") -> int:
    """Validate monetary amount in integer cents."""
    if val is None or val == "":
        return 0
    try:
        cents = int(val)
        if cents < 0:
            raise ValueError(f"{field_name} cannot be negative.")
        return cents
    except (ValueError, TypeError) as err:
        raise ValueError(f"{field_name} must be a valid non-negative integer in cents (got '{val}').") from err


def validate_positive_quantity(val: Any, field_name: str = "Quantity") -> int:
    """Validate strictly positive integer quantity (> 0)."""
    try:
        qty = int(val)
        if qty <= 0:
            raise ValueError(f"{field_name} must be a strictly positive integer (> 0), got {qty}.")
        return qty
    except (ValueError, TypeError) as err:
        raise ValueError(f"{field_name} must be a valid integer (> 0), got '{val}'.") from err


def validate_non_zero_quantity(val: Any, field_name: str = "Quantity delta") -> int:
    """Validate non-zero integer quantity."""
    try:
        qty = int(val)
        if qty == 0:
            raise ValueError(f"{field_name} cannot be zero.")
        return qty
    except (ValueError, TypeError) as err:
        raise ValueError(f"{field_name} must be a valid non-zero integer, got '{val}'.") from err


def validate_reason(reason: Optional[str], field_name: str = "Reason") -> str:
    """Validate mandatory reason explanation."""
    if not reason or not reason.strip():
        raise ValueError(f"{field_name} is required and cannot be blank.")
    clean = reason.strip()
    if len(clean) < 3:
        raise ValueError(f"{field_name} must contain at least 3 characters explaining the movement.")
    if len(clean) > 500:
        raise ValueError(f"{field_name} cannot exceed 500 characters.")
    return clean


# -----------------------------------------------------------------------------
# Stock Derivation Engine (Calculated Directly from Immutable Ledger)
# -----------------------------------------------------------------------------

def compute_item_stock_balances(
    item_id: int,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Derive the exact current stock balances for an item exclusively from its immutable ledger.
    
    Returns:
        on_hand: Total physical quantity in the makerspace (receipts + consumptions + corrections).
        reserved: Total quantity currently reserved for active bookings/maintenance.
        available: Net quantity available for new reservations or walk-in use (max(0, on_hand - reserved)).
        movement_count: Total number of ledger entries recorded for this item.
        last_movement_at: ISO timestamp of the most recent ledger entry, or None.
    """
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        # 1. Calculate physical on-hand stock:
        # receipts (+ABS(quantity)), consumptions (-ABS(quantity)), corrections (+quantity signed)
        row_on_hand = conn.execute(
            """
            SELECT 
                COALESCE(SUM(
                    CASE 
                        WHEN movement_type = 'receipt' THEN ABS(quantity)
                        WHEN movement_type = 'consumption' THEN -ABS(quantity)
                        WHEN movement_type = 'correction' THEN quantity
                        ELSE 0
                    END
                ), 0) AS on_hand_total,
                COUNT(*) AS total_movements,
                MAX(created_at) AS last_movement
            FROM inventory_ledger
            WHERE item_id = ?;
            """,
            (item_id,),
        ).fetchone()

        on_hand = int(row_on_hand["on_hand_total"]) if row_on_hand else 0
        movement_count = int(row_on_hand["total_movements"]) if row_on_hand else 0
        last_movement_at = row_on_hand["last_movement"] if row_on_hand else None

        # 2. Calculate active reserved stock grouped by reference:
        # For each reservation reference (or manual/maintenance hold):
        # active_hold = MAX(0, reservations - releases - consumptions_against_reservation)
        # Note: If consumption occurred with reference_type in ('reservation', 'maintenance')
        # and reference_id, it clears that specific reserved allocation.
        rows_ref = conn.execute(
            """
            SELECT 
                reference_type,
                COALESCE(reference_id, '') AS ref_id,
                COALESCE(SUM(CASE WHEN movement_type = 'reservation' THEN ABS(quantity) ELSE 0 END), 0) AS res_qty,
                COALESCE(SUM(CASE WHEN movement_type = 'release' THEN ABS(quantity) ELSE 0 END), 0) AS rel_qty,
                COALESCE(SUM(CASE WHEN movement_type = 'consumption' THEN ABS(quantity) ELSE 0 END), 0) AS con_qty
            FROM inventory_ledger
            WHERE item_id = ?
            GROUP BY reference_type, COALESCE(reference_id, '')
            HAVING res_qty > 0;
            """,
            (item_id,),
        ).fetchall()

        total_reserved = 0
        for r in rows_ref:
            res_qty = int(r["res_qty"])
            rel_qty = int(r["rel_qty"])
            con_qty = int(r["con_qty"])
            # Active reserved hold for this reference
            net_hold = max(0, res_qty - rel_qty - con_qty)
            total_reserved += net_hold

        # Guard against anomalies
        on_hand = max(0, on_hand)
        total_reserved = max(0, total_reserved)
        # Reserved cannot exceed on-hand
        if total_reserved > on_hand:
            total_reserved = on_hand

        available = max(0, on_hand - total_reserved)

        return {
            "on_hand": on_hand,
            "reserved": total_reserved,
            "available": available,
            "movement_count": movement_count,
            "last_movement_at": last_movement_at,
        }
    finally:
        if owns_conn:
            conn.close()


def get_item_active_holds(
    item_id: int,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """Return a breakdown of active reservations and holds currently allocated for this item."""
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        rows = conn.execute(
            """
            SELECT 
                reference_type,
                COALESCE(reference_id, '') AS reference_id,
                COALESCE(SUM(CASE WHEN movement_type = 'reservation' THEN ABS(quantity) ELSE 0 END), 0) AS res_qty,
                COALESCE(SUM(CASE WHEN movement_type = 'release' THEN ABS(quantity) ELSE 0 END), 0) AS rel_qty,
                COALESCE(SUM(CASE WHEN movement_type = 'consumption' THEN ABS(quantity) ELSE 0 END), 0) AS con_qty,
                MAX(created_at) AS last_updated_at
            FROM inventory_ledger
            WHERE item_id = ?
            GROUP BY reference_type, COALESCE(reference_id, '')
            HAVING res_qty > 0;
            """,
            (item_id,),
        ).fetchall()

        holds = []
        for r in rows:
            res_qty = int(r["res_qty"])
            rel_qty = int(r["rel_qty"])
            con_qty = int(r["con_qty"])
            active_qty = max(0, res_qty - rel_qty - con_qty)
            if active_qty > 0:
                holds.append({
                    "reference_type": r["reference_type"],
                    "reference_id": r["reference_id"],
                    "reserved_quantity": res_qty,
                    "released_quantity": rel_qty,
                    "consumed_quantity": con_qty,
                    "active_hold_quantity": active_qty,
                    "last_updated_at": r["last_updated_at"],
                })
        return holds
    finally:
        if owns_conn:
            conn.close()


# -----------------------------------------------------------------------------
# Inventory Item Queries and CRUD
# -----------------------------------------------------------------------------

def get_inventory_item_by_id(
    item_id: int,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch an inventory item by ID."""
    row = query_one("SELECT * FROM inventory_items WHERE id = ?;", (item_id,), conn=conn)
    return dict(row) if row else None


def get_inventory_item_by_sku(
    sku: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch an inventory item by SKU."""
    clean_sku = sku.strip().upper() if sku else ""
    row = query_one("SELECT * FROM inventory_items WHERE sku = ?;", (clean_sku,), conn=conn)
    return dict(row) if row else None


def get_item_stock_summary(
    item_id: int,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch an inventory item along with its live derived ledger stock metrics."""
    item = get_inventory_item_by_id(item_id, conn=conn)
    if not item:
        return None

    balances = compute_item_stock_balances(item_id, conn=conn)
    on_hand = balances["on_hand"]
    reserved = balances["reserved"]
    available = balances["available"]
    min_stock = item["minimum_stock"]

    is_low_stock = available <= min_stock
    total_val_cents = on_hand * item["unit_cost_cents"]

    item_summary = dict(item)
    item_summary.update({
        "on_hand": on_hand,
        "reserved": reserved,
        "available": available,
        "is_low_stock": is_low_stock,
        "total_value_cents": total_val_cents,
        "total_value_formatted": f"€ {total_val_cents / 100:.2f}",
        "unit_cost_formatted": f"€ {item['unit_cost_cents'] / 100:.2f}",
        "movement_count": balances["movement_count"],
        "last_movement_at": balances["last_movement_at"],
    })
    return item_summary


def list_inventory_items(
    category: Optional[str] = None,
    search: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """List inventory items matching category and search filters."""
    sql = "SELECT * FROM inventory_items WHERE 1=1"
    params: List[Any] = []

    if category:
        sql += " AND category = ?"
        params.append(category.strip())

    if search:
        s = f"%{search.strip()}%"
        sql += " AND (sku LIKE ? OR name LIKE ? OR description LIKE ? OR location LIKE ?)"
        params.extend([s, s, s, s])

    sql += " ORDER BY category ASC, name ASC;"
    return query_all(sql, tuple(params), conn=conn)


def list_inventory_items_with_stock(
    category: Optional[str] = None,
    search: Optional[str] = None,
    low_stock_only: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """List all inventory items with their derived on-hand, reserved, and available stock balances."""
    raw_items = list_inventory_items(category=category, search=search, conn=conn)
    results = []

    for item in raw_items:
        balances = compute_item_stock_balances(item["id"], conn=conn)
        on_hand = balances["on_hand"]
        reserved = balances["reserved"]
        available = balances["available"]
        min_stock = item["minimum_stock"]
        is_low_stock = available <= min_stock

        if low_stock_only and not is_low_stock:
            continue

        total_val_cents = on_hand * item["unit_cost_cents"]

        summary = dict(item)
        summary.update({
            "on_hand": on_hand,
            "reserved": reserved,
            "available": available,
            "is_low_stock": is_low_stock,
            "total_value_cents": total_val_cents,
            "total_value_formatted": f"€ {total_val_cents / 100:.2f}",
            "unit_cost_formatted": f"€ {item['unit_cost_cents'] / 100:.2f}",
            "movement_count": balances["movement_count"],
            "last_movement_at": balances["last_movement_at"],
        })
        results.append(summary)

    return results


def list_inventory_categories(conn: Optional[sqlite3.Connection] = None) -> List[str]:
    """List all distinct categories used across inventory items."""
    rows = query_all("SELECT DISTINCT category FROM inventory_items ORDER BY category ASC;", conn=conn)
    return [r["category"] for r in rows if r.get("category")]


def get_inventory_metrics(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Compute high-level workshop inventory and ledger metrics."""
    items = list_inventory_items_with_stock(conn=conn)
    total_skus = len(items)
    total_on_hand_units = sum(i["on_hand"] for i in items)
    total_reserved_units = sum(i["reserved"] for i in items)
    total_available_units = sum(i["available"] for i in items)
    total_value_cents = sum(i["total_value_cents"] for i in items)
    low_stock_count = sum(1 for i in items if i["is_low_stock"])

    ledger_count_row = query_one("SELECT COUNT(*) AS c FROM inventory_ledger;", conn=conn)
    total_ledger_entries = int(ledger_count_row["c"]) if ledger_count_row else 0

    return {
        "total_skus": total_skus,
        "total_on_hand_units": total_on_hand_units,
        "total_reserved_units": total_reserved_units,
        "total_available_units": total_available_units,
        "total_value_cents": total_value_cents,
        "total_value_formatted": f"€ {total_value_cents / 100:.2f}",
        "low_stock_count": low_stock_count,
        "total_ledger_entries": total_ledger_entries,
    }


def create_inventory_item(
    sku: str,
    name: str,
    category: str,
    unit: str = "pcs",
    unit_cost_cents: int = 0,
    minimum_stock: int = 0,
    location: Optional[str] = None,
    description: Optional[str] = None,
    initial_stock: int = 0,
    actor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create a new inventory item and optionally record an initial stock receipt in the immutable ledger.
    """
    clean_sku = validate_sku(sku)
    clean_name = validate_item_name(name)
    clean_cat = validate_category(category)
    clean_unit = validate_unit(unit)
    clean_cost = validate_cents(unit_cost_cents, "Unit cost")
    clean_min = validate_cents(minimum_stock, "Minimum stock")
    clean_loc = location.strip() if location else None
    clean_desc = description.strip() if description else None
    init_qty = int(initial_stock) if initial_stock else 0

    if init_qty < 0:
        raise ValueError("Initial stock quantity cannot be negative.")

    now_iso = now_rome_iso()

    with transaction(immediate=True) as conn:
        # Check uniqueness of SKU
        existing = conn.execute("SELECT id FROM inventory_items WHERE sku = ?;", (clean_sku,)).fetchone()
        if existing:
            raise ValueError(f"An inventory item with SKU '{clean_sku}' already exists (ID {existing['id']}).")

        cur = conn.execute(
            """
            INSERT INTO inventory_items (
                sku, name, category, unit, unit_cost_cents, minimum_stock, location, description, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                clean_sku,
                clean_name,
                clean_cat,
                clean_unit,
                clean_cost,
                clean_min,
                clean_loc,
                clean_desc,
                now_iso,
                now_iso,
            ),
        )
        new_item_id = cur.lastrowid

        # Audit creation
        record_audit_event(
            action="inventory.item_created",
            object_type="inventory_item",
            object_id=clean_sku,
            actor=actor,
            after={
                "id": new_item_id,
                "sku": clean_sku,
                "name": clean_name,
                "category": clean_cat,
                "unit": clean_unit,
                "unit_cost_cents": clean_cost,
                "minimum_stock": clean_min,
                "location": clean_loc,
            },
            conn=conn,
        )

        # Record initial stock receipt if requested
        if init_qty > 0:
            actor_id = actor.get("id") or actor.get("user_id") if actor else None
            actor_name = actor.get("name") or actor.get("username") if actor else "System"
            conn.execute(
                """
                INSERT INTO inventory_ledger (
                    item_id, movement_type, quantity, unit_cost_cents, reference_type, reference_id, reason, actor_id, actor_name, created_at
                ) VALUES (?, 'receipt', ?, ?, 'initial_stock', ?, ?, ?, ?, ?);
                """,
                (
                    new_item_id,
                    init_qty,
                    clean_cost,
                    f"INIT-{clean_sku}",
                    "Initial warehouse stock registration",
                    actor_id,
                    actor_name,
                    now_iso,
                ),
            )
            record_audit_event(
                action="inventory.receipt",
                object_type="inventory_ledger",
                object_id=clean_sku,
                actor=actor,
                after={
                    "item_id": new_item_id,
                    "sku": clean_sku,
                    "movement_type": "receipt",
                    "quantity": init_qty,
                    "unit_cost_cents": clean_cost,
                    "reference_type": "initial_stock",
                    "reason": "Initial warehouse stock registration",
                },
                conn=conn,
            )

    return get_item_stock_summary(new_item_id)  # type: ignore[return-value]


def update_inventory_item(
    item_id: int,
    name: Optional[str] = None,
    category: Optional[str] = None,
    unit: Optional[str] = None,
    unit_cost_cents: Optional[int] = None,
    minimum_stock: Optional[int] = None,
    location: Optional[str] = None,
    description: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Update inventory item metadata."""
    now_iso = now_rome_iso()

    with transaction(immediate=True) as conn:
        existing = conn.execute("SELECT * FROM inventory_items WHERE id = ?;", (item_id,)).fetchone()
        if not existing:
            raise ValueError(f"Inventory item ID {item_id} not found.")

        old_dict = dict(existing)

        new_name = validate_item_name(name) if name is not None else old_dict["name"]
        new_cat = validate_category(category) if category is not None else old_dict["category"]
        new_unit = validate_unit(unit) if unit is not None else old_dict["unit"]
        new_cost = validate_cents(unit_cost_cents, "Unit cost") if unit_cost_cents is not None else old_dict["unit_cost_cents"]
        new_min = validate_cents(minimum_stock, "Minimum stock") if minimum_stock is not None else old_dict["minimum_stock"]
        new_loc = location.strip() if location is not None else old_dict["location"]
        new_desc = description.strip() if description is not None else old_dict["description"]

        conn.execute(
            """
            UPDATE inventory_items
            SET name = ?, category = ?, unit = ?, unit_cost_cents = ?, minimum_stock = ?, location = ?, description = ?, updated_at = ?
            WHERE id = ?;
            """,
            (
                new_name,
                new_cat,
                new_unit,
                new_cost,
                new_min,
                new_loc,
                new_desc,
                now_iso,
                item_id,
            ),
        )

        record_audit_event(
            action="inventory.item_updated",
            object_type="inventory_item",
            object_id=old_dict["sku"],
            actor=actor,
            before=old_dict,
            after={
                "id": item_id,
                "sku": old_dict["sku"],
                "name": new_name,
                "category": new_cat,
                "unit": new_unit,
                "unit_cost_cents": new_cost,
                "minimum_stock": new_min,
                "location": new_loc,
            },
            conn=conn,
        )

    return get_item_stock_summary(item_id)  # type: ignore[return-value]


# -----------------------------------------------------------------------------
# Immutable Movement Ledger Operations (Append-Only)
# -----------------------------------------------------------------------------

def record_receipt(
    item_id: int,
    quantity: int,
    unit_cost_cents: Optional[int] = None,
    reference_type: str = "receipt",
    reference_id: Optional[str] = None,
    reason: str = "Stock receipt",
    actor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Record stock delivery/receipt into makerspace inventory.
    Increases on-hand and available stock.
    """
    qty = validate_positive_quantity(quantity, "Receipt quantity")
    clean_reason = validate_reason(reason, "Receipt reason")
    ref_type = reference_type.strip().lower() if reference_type else "receipt"
    if ref_type not in VALID_REFERENCE_TYPES:
        ref_type = "receipt"
    ref_id = reference_id.strip() if reference_id else None
    now_iso = now_rome_iso()

    actor_id = actor.get("id") or actor.get("user_id") if actor else None
    actor_name = actor.get("name") or actor.get("username") or actor.get("full_name") if actor else "System"

    with transaction(immediate=True) as conn:
        item = conn.execute("SELECT * FROM inventory_items WHERE id = ?;", (item_id,)).fetchone()
        if not item:
            raise ValueError(f"Inventory item ID {item_id} not found.")

        cost = validate_cents(unit_cost_cents, "Unit cost") if unit_cost_cents is not None else item["unit_cost_cents"]

        # Append to immutable ledger
        cur = conn.execute(
            """
            INSERT INTO inventory_ledger (
                item_id, movement_type, quantity, unit_cost_cents, reference_type, reference_id, reason, actor_id, actor_name, created_at
            ) VALUES (?, 'receipt', ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                item_id,
                qty,
                cost,
                ref_type,
                ref_id,
                clean_reason,
                actor_id,
                actor_name,
                now_iso,
            ),
        )
        ledger_entry_id = cur.lastrowid

        # Update item's updated_at timestamp
        conn.execute("UPDATE inventory_items SET updated_at = ? WHERE id = ?;", (now_iso, item_id))

        record_audit_event(
            action="inventory.receipt",
            object_type="inventory_ledger",
            object_id=item["sku"],
            actor=actor,
            after={
                "ledger_id": ledger_entry_id,
                "item_id": item_id,
                "sku": item["sku"],
                "movement_type": "receipt",
                "quantity": qty,
                "unit_cost_cents": cost,
                "reference_type": ref_type,
                "reference_id": ref_id,
                "reason": clean_reason,
            },
            conn=conn,
        )

        stock_summary = compute_item_stock_balances(item_id, conn=conn)

    return {
        "ledger_id": ledger_entry_id,
        "item_id": item_id,
        "sku": item["sku"],
        "movement_type": "receipt",
        "quantity": qty,
        "unit_cost_cents": cost,
        "reference_type": ref_type,
        "reference_id": ref_id,
        "reason": clean_reason,
        "created_at": now_iso,
        "stock": stock_summary,
    }


def record_reservation(
    item_id: int,
    quantity: int,
    reference_type: str = "reservation",
    reference_id: Optional[str] = None,
    reason: str = "Reserved for project/booking",
    actor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Reserve/earmark stock for an upcoming reservation, project, or maintenance job.
    Decreases available stock without changing physical on-hand stock.
    Guarantees no over-allocation or negative available stock under concurrency.
    """
    qty = validate_positive_quantity(quantity, "Reservation quantity")
    clean_reason = validate_reason(reason, "Reservation reason")
    ref_type = reference_type.strip().lower() if reference_type else "reservation"
    if ref_type not in VALID_REFERENCE_TYPES:
        ref_type = "reservation"
    ref_id = str(reference_id).strip() if reference_id is not None else None
    now_iso = now_rome_iso()

    actor_id = actor.get("id") or actor.get("user_id") if actor else None
    actor_name = actor.get("name") or actor.get("username") or actor.get("full_name") if actor else "System"

    with transaction(immediate=True) as conn:
        item = conn.execute("SELECT * FROM inventory_items WHERE id = ?;", (item_id,)).fetchone()
        if not item:
            raise ValueError(f"Inventory item ID {item_id} not found.")

        # Derive current balances under transaction write lock
        current_balances = compute_item_stock_balances(item_id, conn=conn)
        available = current_balances["available"]

        if available < qty:
            raise ValueError(
                f"Insufficient available stock for '{item['sku']}': requested to reserve {qty} {item['unit']}, "
                f"but only {available} {item['unit']} currently available (On-hand: {current_balances['on_hand']}, "
                f"Reserved: {current_balances['reserved']})."
            )

        cur = conn.execute(
            """
            INSERT INTO inventory_ledger (
                item_id, movement_type, quantity, unit_cost_cents, reference_type, reference_id, reason, actor_id, actor_name, created_at
            ) VALUES (?, 'reservation', ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                item_id,
                qty,
                item["unit_cost_cents"],
                ref_type,
                ref_id,
                clean_reason,
                actor_id,
                actor_name,
                now_iso,
            ),
        )
        ledger_entry_id = cur.lastrowid
        conn.execute("UPDATE inventory_items SET updated_at = ? WHERE id = ?;", (now_iso, item_id))

        record_audit_event(
            action="inventory.reservation",
            object_type="inventory_ledger",
            object_id=item["sku"],
            actor=actor,
            after={
                "ledger_id": ledger_entry_id,
                "item_id": item_id,
                "sku": item["sku"],
                "movement_type": "reservation",
                "quantity": qty,
                "reference_type": ref_type,
                "reference_id": ref_id,
                "reason": clean_reason,
                "remaining_available": available - qty,
            },
            conn=conn,
        )

        stock_summary = compute_item_stock_balances(item_id, conn=conn)

    return {
        "ledger_id": ledger_entry_id,
        "item_id": item_id,
        "sku": item["sku"],
        "movement_type": "reservation",
        "quantity": qty,
        "reference_type": ref_type,
        "reference_id": ref_id,
        "reason": clean_reason,
        "created_at": now_iso,
        "stock": stock_summary,
    }


def record_consumption(
    item_id: int,
    quantity: int,
    reference_type: str = "manual",
    reference_id: Optional[str] = None,
    reason: str = "Material consumed",
    actor: Optional[Dict[str, Any]] = None,
    unit_cost_cents: Optional[int] = None,
    from_reservation: bool = False,
) -> Dict[str, Any]:
    """
    Record physical consumption / usage of inventory items.
    Decreases on-hand stock.
    If from_reservation=True or reference_id matches an active hold, consumes against that reservation hold.
    If direct consumption, verifies available stock >= quantity.
    Prevents negative on-hand or available stock under concurrent executions.
    """
    qty = validate_positive_quantity(quantity, "Consumption quantity")
    clean_reason = validate_reason(reason, "Consumption reason")
    ref_type = reference_type.strip().lower() if reference_type else "manual"
    if ref_type not in VALID_REFERENCE_TYPES:
        ref_type = "manual"
    ref_id = str(reference_id).strip() if reference_id is not None else None
    now_iso = now_rome_iso()

    actor_id = actor.get("id") or actor.get("user_id") if actor else None
    actor_name = actor.get("name") or actor.get("username") or actor.get("full_name") if actor else "System"

    with transaction(immediate=True) as conn:
        item = conn.execute("SELECT * FROM inventory_items WHERE id = ?;", (item_id,)).fetchone()
        if not item:
            raise ValueError(f"Inventory item ID {item_id} not found.")

        cost = validate_cents(unit_cost_cents, "Unit cost") if unit_cost_cents is not None else item["unit_cost_cents"]
        current_balances = compute_item_stock_balances(item_id, conn=conn)
        on_hand = current_balances["on_hand"]
        available = current_balances["available"]

        # Check physical on-hand availability
        if on_hand < qty:
            raise ValueError(
                f"Cannot consume {qty} {item['unit']} of '{item['sku']}': only {on_hand} {item['unit']} physically on-hand."
            )

        # Check if there is an active reservation hold for this reference
        has_active_hold = False
        active_hold_qty = 0
        if ref_id:
            holds = get_item_active_holds(item_id, conn=conn)
            for h in holds:
                if h["reference_id"] == ref_id and h["reference_type"] == ref_type:
                    has_active_hold = True
                    active_hold_qty = h["active_hold_quantity"]
                    break

        if not has_active_hold and not from_reservation:
            # Direct walk-in consumption requires available stock >= qty
            if available < qty:
                raise ValueError(
                    f"Cannot consume {qty} {item['unit']} of '{item['sku']}': only {available} {item['unit']} available "
                    f"(On-hand: {on_hand}, Reserved: {current_balances['reserved']})."
                )

        # Append consumption to ledger (recorded with negative quantity)
        cur = conn.execute(
            """
            INSERT INTO inventory_ledger (
                item_id, movement_type, quantity, unit_cost_cents, reference_type, reference_id, reason, actor_id, actor_name, created_at
            ) VALUES (?, 'consumption', ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                item_id,
                -qty,
                cost,
                ref_type,
                ref_id,
                clean_reason,
                actor_id,
                actor_name,
                now_iso,
            ),
        )
        ledger_entry_id = cur.lastrowid
        conn.execute("UPDATE inventory_items SET updated_at = ? WHERE id = ?;", (now_iso, item_id))

        record_audit_event(
            action="inventory.consumption",
            object_type="inventory_ledger",
            object_id=item["sku"],
            actor=actor,
            after={
                "ledger_id": ledger_entry_id,
                "item_id": item_id,
                "sku": item["sku"],
                "movement_type": "consumption",
                "quantity": -qty,
                "unit_cost_cents": cost,
                "reference_type": ref_type,
                "reference_id": ref_id,
                "reason": clean_reason,
                "was_reserved": has_active_hold or from_reservation,
            },
            conn=conn,
        )

        stock_summary = compute_item_stock_balances(item_id, conn=conn)

    return {
        "ledger_id": ledger_entry_id,
        "item_id": item_id,
        "sku": item["sku"],
        "movement_type": "consumption",
        "quantity": -qty,
        "unit_cost_cents": cost,
        "reference_type": ref_type,
        "reference_id": ref_id,
        "reason": clean_reason,
        "created_at": now_iso,
        "stock": stock_summary,
    }


def record_release(
    item_id: int,
    quantity: int,
    reference_type: str = "reservation",
    reference_id: Optional[str] = None,
    reason: str = "Released unused reservation",
    actor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Release previously reserved stock back to the general available pool.
    Increases available stock without altering on-hand physical stock.
    """
    qty = validate_positive_quantity(quantity, "Release quantity")
    clean_reason = validate_reason(reason, "Release reason")
    ref_type = reference_type.strip().lower() if reference_type else "reservation"
    if ref_type not in VALID_REFERENCE_TYPES:
        ref_type = "reservation"
    ref_id = str(reference_id).strip() if reference_id is not None else None
    now_iso = now_rome_iso()

    actor_id = actor.get("id") or actor.get("user_id") if actor else None
    actor_name = actor.get("name") or actor.get("username") or actor.get("full_name") if actor else "System"

    with transaction(immediate=True) as conn:
        item = conn.execute("SELECT * FROM inventory_items WHERE id = ?;", (item_id,)).fetchone()
        if not item:
            raise ValueError(f"Inventory item ID {item_id} not found.")

        current_balances = compute_item_stock_balances(item_id, conn=conn)
        total_reserved = current_balances["reserved"]

        if total_reserved <= 0:
            raise ValueError(f"Cannot release stock for '{item['sku']}': no active reservations exist for this item.")

        if ref_id:
            holds = get_item_active_holds(item_id, conn=conn)
            hold_qty = 0
            for h in holds:
                if h["reference_id"] == ref_id and h["reference_type"] == ref_type:
                    hold_qty = h["active_hold_quantity"]
                    break
            if hold_qty < qty:
                raise ValueError(
                    f"Cannot release {qty} units for reference '{ref_id}': only {hold_qty} units currently reserved under this reference."
                )
        else:
            if total_reserved < qty:
                raise ValueError(
                    f"Cannot release {qty} units for '{item['sku']}': only {total_reserved} total units currently reserved."
                )

        cur = conn.execute(
            """
            INSERT INTO inventory_ledger (
                item_id, movement_type, quantity, unit_cost_cents, reference_type, reference_id, reason, actor_id, actor_name, created_at
            ) VALUES (?, 'release', ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                item_id,
                qty,
                item["unit_cost_cents"],
                ref_type,
                ref_id,
                clean_reason,
                actor_id,
                actor_name,
                now_iso,
            ),
        )
        ledger_entry_id = cur.lastrowid
        conn.execute("UPDATE inventory_items SET updated_at = ? WHERE id = ?;", (now_iso, item_id))

        record_audit_event(
            action="inventory.release",
            object_type="inventory_ledger",
            object_id=item["sku"],
            actor=actor,
            after={
                "ledger_id": ledger_entry_id,
                "item_id": item_id,
                "sku": item["sku"],
                "movement_type": "release",
                "quantity": qty,
                "reference_type": ref_type,
                "reference_id": ref_id,
                "reason": clean_reason,
            },
            conn=conn,
        )

        stock_summary = compute_item_stock_balances(item_id, conn=conn)

    return {
        "ledger_id": ledger_entry_id,
        "item_id": item_id,
        "sku": item["sku"],
        "movement_type": "release",
        "quantity": qty,
        "reference_type": ref_type,
        "reference_id": ref_id,
        "reason": clean_reason,
        "created_at": now_iso,
        "stock": stock_summary,
    }


def record_correction(
    item_id: int,
    quantity_delta: int,
    reason: str,
    reference_type: str = "adjustment",
    reference_id: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Record a manual inventory correction / reconciliation with mandatory motivation.
    Positive delta adds units (e.g. found stock), negative delta removes units (e.g. damaged/lost).
    Strictly verifies that resulting on-hand and available quantities remain non-negative (>= 0).
    """
    delta = validate_non_zero_quantity(quantity_delta, "Correction delta")
    clean_reason = validate_reason(reason, "Correction justification reason")
    ref_type = reference_type.strip().lower() if reference_type else "adjustment"
    if ref_type not in VALID_REFERENCE_TYPES:
        ref_type = "adjustment"
    ref_id = str(reference_id).strip() if reference_id is not None else None
    now_iso = now_rome_iso()

    actor_id = actor.get("id") or actor.get("user_id") if actor else None
    actor_name = actor.get("name") or actor.get("username") or actor.get("full_name") if actor else "System"

    with transaction(immediate=True) as conn:
        item = conn.execute("SELECT * FROM inventory_items WHERE id = ?;", (item_id,)).fetchone()
        if not item:
            raise ValueError(f"Inventory item ID {item_id} not found.")

        current_balances = compute_item_stock_balances(item_id, conn=conn)
        on_hand = current_balances["on_hand"]
        reserved = current_balances["reserved"]
        available = current_balances["available"]

        new_on_hand = on_hand + delta
        new_available = available + delta

        if new_on_hand < 0:
            raise ValueError(
                f"Invalid correction of {delta} {item['unit']} for '{item['sku']}': "
                f"on-hand stock ({on_hand}) would become negative ({new_on_hand})."
            )

        if new_available < 0:
            raise ValueError(
                f"Invalid correction of {delta} {item['unit']} for '{item['sku']}': "
                f"available stock ({available}) would become negative ({new_available}) due to existing reserved commitments ({reserved})."
            )

        cur = conn.execute(
            """
            INSERT INTO inventory_ledger (
                item_id, movement_type, quantity, unit_cost_cents, reference_type, reference_id, reason, actor_id, actor_name, created_at
            ) VALUES (?, 'correction', ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                item_id,
                delta,
                item["unit_cost_cents"],
                ref_type,
                ref_id,
                clean_reason,
                actor_id,
                actor_name,
                now_iso,
            ),
        )
        ledger_entry_id = cur.lastrowid
        conn.execute("UPDATE inventory_items SET updated_at = ? WHERE id = ?;", (now_iso, item_id))

        record_audit_event(
            action="inventory.correction",
            object_type="inventory_ledger",
            object_id=item["sku"],
            actor=actor,
            after={
                "ledger_id": ledger_entry_id,
                "item_id": item_id,
                "sku": item["sku"],
                "movement_type": "correction",
                "quantity_delta": delta,
                "reason": clean_reason,
                "reference_type": ref_type,
                "reference_id": ref_id,
                "on_hand_after": new_on_hand,
                "available_after": new_available,
            },
            conn=conn,
        )

        stock_summary = compute_item_stock_balances(item_id, conn=conn)

    return {
        "ledger_id": ledger_entry_id,
        "item_id": item_id,
        "sku": item["sku"],
        "movement_type": "correction",
        "quantity": delta,
        "reference_type": ref_type,
        "reference_id": ref_id,
        "reason": clean_reason,
        "created_at": now_iso,
        "stock": stock_summary,
    }


# -----------------------------------------------------------------------------
# Ledger History Queries
# -----------------------------------------------------------------------------

def get_ledger_history(
    item_id: Optional[int] = None,
    movement_type: Optional[str] = None,
    reference_type: Optional[str] = None,
    reference_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    conn: Optional[sqlite3.Connection] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Fetch paginated immutable ledger entries with joined item metadata and total count.
    """
    where_clauses: List[str] = []
    params: List[Any] = []

    if item_id is not None:
        where_clauses.append("l.item_id = ?")
        params.append(item_id)

    if movement_type:
        clean_mt = movement_type.strip().lower()
        if clean_mt in VALID_MOVEMENT_TYPES:
            where_clauses.append("l.movement_type = ?")
            params.append(clean_mt)

    if reference_type:
        clean_rt = reference_type.strip().lower()
        if clean_rt in VALID_REFERENCE_TYPES:
            where_clauses.append("l.reference_type = ?")
            params.append(clean_rt)

    if reference_id:
        where_clauses.append("l.reference_id = ?")
        params.append(str(reference_id).strip())

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Count total matching rows
    count_sql = f"SELECT COUNT(*) AS c FROM inventory_ledger l {where_sql};"
    row_count = query_one(count_sql, tuple(params), conn=conn)
    total_count = int(row_count["c"]) if row_count else 0

    # Fetch rows with joined item data
    sql = f"""
        SELECT 
            l.id,
            l.item_id,
            l.movement_type,
            l.quantity,
            l.unit_cost_cents,
            l.reference_type,
            l.reference_id,
            l.reason,
            l.actor_id,
            l.actor_name,
            l.created_at,
            i.sku AS item_sku,
            i.name AS item_name,
            i.category AS item_category,
            i.unit AS item_unit
        FROM inventory_ledger l
        JOIN inventory_items i ON l.item_id = i.id
        {where_sql}
        ORDER BY l.id DESC
        LIMIT ? OFFSET ?;
    """
    query_params = list(params) + [max(1, limit), max(0, offset)]
    rows = query_all(sql, tuple(query_params), conn=conn)

    return rows, total_count
