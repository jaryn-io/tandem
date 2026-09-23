"""Inventory management and immutable movement ledger package for ForgeDesk."""

from forgedesk.inventory.service import (
    create_inventory_item,
    get_inventory_item_by_id,
    get_inventory_item_by_sku,
    get_inventory_metrics,
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
from forgedesk.inventory.handlers import register_inventory_routes

__all__ = [
    "create_inventory_item",
    "get_inventory_item_by_id",
    "get_inventory_item_by_sku",
    "get_inventory_metrics",
    "get_item_stock_summary",
    "get_ledger_history",
    "list_inventory_categories",
    "list_inventory_items",
    "list_inventory_items_with_stock",
    "record_consumption",
    "record_correction",
    "record_receipt",
    "record_release",
    "record_reservation",
    "register_inventory_routes",
    "update_inventory_item",
]
