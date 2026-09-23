"""System Service - CSV Import/Export, Online SQLite Backup, and Verified Restore for ForgeDesk."""

import csv
import datetime
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from forgedesk.audit.service import record_audit_event
from forgedesk.config import BACKUP_DIR, DB_PATH
from forgedesk.db.connection import get_connection, query_all, query_one, transaction
from forgedesk.db.migrations import apply_migrations
from forgedesk.utils.datetime_tz import format_display, format_iso, now_rome, now_rome_iso, parse_datetime

logger = logging.getLogger("forgedesk.system.service")

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_REGEX = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_cents(amount_str: Any, default_cents: int = 0) -> int:
    """Safely parse a decimal currency string (e.g. '12.50') into integer cents (1250)."""
    if amount_str is None or str(amount_str).strip() == "":
        return default_cents
    try:
        clean = str(amount_str).strip().replace("€", "").replace(",", ".").strip()
        val = float(clean)
        return max(0, int(round(val * 100)))
    except (ValueError, TypeError):
        raise ValueError(f"Invalid monetary format '{amount_str}'. Expected decimal (e.g. 12.50).")


# =============================================================================
# CSV Sample Templates
# =============================================================================

SAMPLE_CSV_TEMPLATES: Dict[str, str] = {
    "members": (
        "full_name,email,phone,membership_status,membership_expiry,notes,qualifications\n"
        "\"Marco Verdi\",\"marco.verdi@example.com\",\"+39 340 1122334\",\"active\",\"2026-12-31\",\"New maker member\",\"3d_printers:2026-12-31;laser_cutters:2026-10-31\"\n"
        "\"Giulia Romano\",\"giulia.romano@example.com\",\"+39 347 5566778\",\"active\",\"2027-06-30\",\"Electronics specialist\",\"electronics;cnc_mills\"\n"
        "\"Luca Ferrari\",\"luca.ferrari@example.com\",\"+39 333 9988776\",\"active\",\"2026-11-15\",\"Woodworking hobbyist\",\"woodworking\"\n"
    ),
    "machines": (
        "code,name,category_code,capacity,state,operating_hours_start,operating_hours_end,hourly_rate,minimum_charge,peak_hourly_rate,peak_hours_start,peak_hours_end,location,description\n"
        "\"ULTI-S5-01\",\"Ultimaker S5 Pro\",\"3d_printers\",1,\"available\",\"08:00\",\"22:00\",\"4.50\",\"2.00\",\"6.00\",\"17:00\",\"21:00\",\"Lab 3D - Bench 2\",\"Dual extrusion high precision FDM 3D printer\"\n"
        "\"TROTEC-SP500\",\"Trotec Speedy 500 Laser\",\"laser_cutters\",1,\"available\",\"09:00\",\"21:00\",\"25.00\",\"10.00\",\"30.00\",\"17:00\",\"21:00\",\"Laser Room 1\",\"120W CO2 laser cutter and engraver\"\n"
        "\"HAKKO-FX951\",\"Hakko FX-951 Soldering Station\",\"electronics\",2,\"available\",\"08:00\",\"22:00\",\"0.00\",\"0.00\",\"0.00\",\"17:00\",\"21:00\",\"Electronics Bay 3\",\"Lead-free ESD-safe soldering station\"\n"
    ),
    "inventory": (
        "sku,name,category,unit,unit_cost,minimum_stock,initial_quantity,location,description\n"
        "\"MAT-PLA-BLK-1KG\",\"PLA Filament 1.75mm Black 1kg\",\"filament\",\"spool\",\"22.00\",3,10,\"Shelf B2\",\"Premium PLA 1.75mm black filament spool\"\n"
        "\"MAT-ACRYL-3MM-CLR\",\"Cast Acrylic Sheet 3mm Clear 600x400\",\"sheets\",\"pcs\",\"14.50\",5,20,\"Rack Laser-1\",\"Optically clear cast acrylic for laser cutting\"\n"
        "\"MAT-NOZZLE-04-BRS\",\"0.4mm Brass V6 Nozzle\",\"spare_parts\",\"pcs\",\"4.50\",4,15,\"Drawer 3D-Parts\",\"Replacement 0.4mm brass hotend nozzles\"\n"
    ),
}


def get_sample_csv(entity_type: str) -> str:
    """Retrieve sample CSV template for an entity type."""
    return SAMPLE_CSV_TEMPLATES.get(entity_type.lower(), "")


# =============================================================================
# CSV Import Preview & Row-by-Row Validation Engine
# =============================================================================

def _get_category_cache() -> Dict[str, Dict[str, Any]]:
    """Load machine categories cache for fast code / name lookups."""
    rows = query_all("SELECT id, code, name FROM machine_categories;")
    cache: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        cache[r["code"].lower()] = dict(r)
        cache[r["name"].lower()] = dict(r)
    return cache


def preview_csv_import(
    entity_type: str,
    csv_text: str,
    actor_user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Parse CSV text, perform row-by-row validation against business rules,
    and return structured preview data with detailed errors per row.
    """
    clean_type = entity_type.strip().lower()
    if clean_type not in ("members", "machines", "inventory"):
        raise ValueError(f"Unsupported import entity type '{entity_type}'. Supported: members, machines, inventory.")

    if not csv_text or not csv_text.strip():
        raise ValueError("CSV input is empty.")

    try:
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
    except Exception as e:
        raise ValueError(f"Malformed CSV syntax: {e}")

    if not reader.fieldnames:
        raise ValueError("CSV contains no valid header row.")

    headers = [h.strip() for h in reader.fieldnames if h]
    category_cache = _get_category_cache()

    existing_member_emails = {
        r["email"].lower() for r in query_all("SELECT email FROM members;") if r.get("email")
    }
    existing_machine_codes = {
        r["code"].upper() for r in query_all("SELECT code FROM machines;") if r.get("code")
    }
    existing_skus = {
        r["sku"].upper() for r in query_all("SELECT sku FROM inventory_items;") if r.get("sku")
    }

    seen_emails_in_batch: Set[str] = set()
    seen_machine_codes_in_batch: Set[str] = set()
    seen_skus_in_batch: Set[str] = set()

    rows_preview: List[Dict[str, Any]] = []
    valid_count = 0
    invalid_count = 0

    for idx, raw_row in enumerate(reader, start=1):
        row_errors: List[str] = []
        parsed_data: Dict[str, Any] = {}

        # ---------------------------------------------------------------------
        # 1. Members Validation
        # ---------------------------------------------------------------------
        if clean_type == "members":
            full_name = raw_row.get("full_name", "").strip()
            email = raw_row.get("email", "").strip()
            phone = raw_row.get("phone", "").strip()
            status = (raw_row.get("membership_status") or "active").strip().lower()
            expiry = raw_row.get("membership_expiry", "").strip()
            notes = raw_row.get("notes", "").strip()
            qualifications_raw = raw_row.get("qualifications", "").strip()

            if not full_name:
                row_errors.append("Field 'full_name' is required.")
            if not email:
                row_errors.append("Field 'email' is required.")
            elif not EMAIL_REGEX.match(email):
                row_errors.append(f"Invalid email format '{email}'.")
            elif email.lower() in existing_member_emails:
                row_errors.append(f"Email '{email}' is already registered to an existing member.")
            elif email.lower() in seen_emails_in_batch:
                row_errors.append(f"Duplicate email '{email}' in current CSV batch.")
            else:
                seen_emails_in_batch.add(email.lower())

            if status not in ("active", "suspended", "expired"):
                row_errors.append(f"Invalid membership_status '{status}'. Must be active, suspended, or expired.")

            if expiry:
                try:
                    datetime.date.fromisoformat(expiry)
                except ValueError:
                    row_errors.append(f"Invalid membership_expiry date '{expiry}'. Expected YYYY-MM-DD.")

            # Validate qualifications
            parsed_quals: List[Dict[str, Any]] = []
            if qualifications_raw:
                qual_tokens = [q.strip() for q in qualifications_raw.split(";") if q.strip()]
                for qtok in qual_tokens:
                    cat_key, _, exp_date = qtok.partition(":")
                    cat_key_clean = cat_key.strip().lower()
                    cat_match = category_cache.get(cat_key_clean)
                    if not cat_match:
                        row_errors.append(f"Unknown machine category code or name '{cat_key}' in qualifications.")
                    else:
                        qual_expiry = None
                        if exp_date:
                            exp_clean = exp_date.strip()
                            try:
                                datetime.date.fromisoformat(exp_clean)
                                qual_expiry = exp_clean
                            except ValueError:
                                row_errors.append(f"Invalid expiry date '{exp_clean}' for qualification '{cat_key}'.")
                        parsed_quals.append({
                            "category_id": cat_match["id"],
                            "category_code": cat_match["code"],
                            "category_name": cat_match["name"],
                            "expiry_date": qual_expiry,
                        })

            parsed_data = {
                "full_name": full_name,
                "email": email,
                "phone": phone,
                "membership_status": status,
                "membership_expiry": expiry or None,
                "notes": notes or None,
                "qualifications": parsed_quals,
            }

        # ---------------------------------------------------------------------
        # 2. Machines Validation
        # ---------------------------------------------------------------------
        elif clean_type == "machines":
            code = raw_row.get("code", "").strip().upper()
            name = raw_row.get("name", "").strip()
            cat_code = raw_row.get("category_code", "").strip().lower()
            capacity_raw = raw_row.get("capacity", "1").strip()
            state = (raw_row.get("state") or "available").strip().lower()
            hours_start = (raw_row.get("operating_hours_start") or "08:00").strip()
            hours_end = (raw_row.get("operating_hours_end") or "22:00").strip()
            rate_raw = raw_row.get("hourly_rate", "0").strip()
            min_raw = raw_row.get("minimum_charge", "0").strip()
            peak_rate_raw = raw_row.get("peak_hourly_rate", "0").strip()
            peak_start = (raw_row.get("peak_hours_start") or "17:00").strip()
            peak_end = (raw_row.get("peak_hours_end") or "21:00").strip()
            location = raw_row.get("location", "").strip()
            description = raw_row.get("description", "").strip()

            if not code:
                row_errors.append("Field 'code' is required.")
            elif code in existing_machine_codes:
                row_errors.append(f"Machine code '{code}' already exists in database.")
            elif code in seen_machine_codes_in_batch:
                row_errors.append(f"Duplicate machine code '{code}' in current CSV batch.")
            else:
                seen_machine_codes_in_batch.add(code)

            if not name:
                row_errors.append("Field 'name' is required.")

            cat_match = category_cache.get(cat_code)
            if not cat_code:
                row_errors.append("Field 'category_code' is required.")
            elif not cat_match:
                row_errors.append(f"Unknown machine category code '{cat_code}'.")

            capacity = 1
            try:
                capacity = int(capacity_raw)
                if capacity < 1:
                    row_errors.append("Capacity must be at least 1.")
            except ValueError:
                row_errors.append(f"Invalid integer capacity '{capacity_raw}'.")

            if state not in ("available", "temporarily_unavailable", "under_maintenance", "retired"):
                row_errors.append(f"Invalid machine state '{state}'. Must be available, temporarily_unavailable, under_maintenance, or retired.")

            if not TIME_REGEX.match(hours_start) or not TIME_REGEX.match(hours_end):
                row_errors.append(f"Invalid operating hours format '{hours_start}-{hours_end}'. Expected HH:MM.")

            hourly_cents = 0
            min_cents = 0
            peak_cents = 0
            try:
                hourly_cents = parse_cents(rate_raw)
                min_cents = parse_cents(min_raw)
                peak_cents = parse_cents(peak_rate_raw)
            except ValueError as e:
                row_errors.append(str(e))

            parsed_data = {
                "code": code,
                "name": name,
                "category_id": cat_match["id"] if cat_match else None,
                "category_code": cat_match["code"] if cat_match else cat_code,
                "capacity": capacity,
                "state": state,
                "operating_hours_start": hours_start,
                "operating_hours_end": hours_end,
                "hourly_rate_cents": hourly_cents,
                "minimum_charge_cents": min_cents,
                "peak_hourly_rate_cents": peak_cents,
                "peak_hours_start": peak_start,
                "peak_hours_end": peak_end,
                "location": location or None,
                "description": description or None,
            }

        # ---------------------------------------------------------------------
        # 3. Inventory Validation
        # ---------------------------------------------------------------------
        elif clean_type == "inventory":
            sku = raw_row.get("sku", "").strip().upper()
            name = raw_row.get("name", "").strip()
            category = (raw_row.get("category") or "general").strip().lower()
            unit = (raw_row.get("unit") or "pcs").strip().lower()
            cost_raw = raw_row.get("unit_cost", "0").strip()
            min_stock_raw = raw_row.get("minimum_stock", "0").strip()
            init_qty_raw = raw_row.get("initial_quantity", "0").strip()
            location = raw_row.get("location", "").strip()
            description = raw_row.get("description", "").strip()

            if not sku:
                row_errors.append("Field 'sku' is required.")
            elif sku in existing_skus:
                row_errors.append(f"Inventory SKU '{sku}' already exists in database.")
            elif sku in seen_skus_in_batch:
                row_errors.append(f"Duplicate SKU '{sku}' in current CSV batch.")
            else:
                seen_skus_in_batch.add(sku)

            if not name:
                row_errors.append("Field 'name' is required.")

            unit_cost_cents = 0
            try:
                unit_cost_cents = parse_cents(cost_raw)
            except ValueError as e:
                row_errors.append(str(e))

            min_stock = 0
            try:
                min_stock = int(min_stock_raw)
                if min_stock < 0:
                    row_errors.append("minimum_stock cannot be negative.")
            except ValueError:
                row_errors.append(f"Invalid integer minimum_stock '{min_stock_raw}'.")

            init_qty = 0
            try:
                init_qty = int(init_qty_raw)
                if init_qty < 0:
                    row_errors.append("initial_quantity cannot be negative.")
            except ValueError:
                row_errors.append(f"Invalid integer initial_quantity '{init_qty_raw}'.")

            parsed_data = {
                "sku": sku,
                "name": name,
                "category": category,
                "unit": unit,
                "unit_cost_cents": unit_cost_cents,
                "minimum_stock": min_stock,
                "initial_quantity": init_qty,
                "location": location or None,
                "description": description or None,
            }

        is_valid = len(row_errors) == 0
        if is_valid:
            valid_count += 1
        else:
            invalid_count += 1

        rows_preview.append({
            "row_index": idx,
            "raw": dict(raw_row),
            "parsed": parsed_data,
            "is_valid": is_valid,
            "errors": row_errors,
        })

    return {
        "entity_type": clean_type,
        "total_rows": len(rows_preview),
        "valid_rows_count": valid_count,
        "invalid_rows_count": invalid_count,
        "can_import": (invalid_count == 0 and valid_count > 0),
        "headers": headers,
        "rows": rows_preview,
    }


# =============================================================================
# Atomic CSV Import Execution with Complete Rollback on Failure
# =============================================================================

def execute_csv_import(
    entity_type: str,
    csv_text: str,
    actor_user: Dict[str, Any],
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically execute CSV import. If ANY validation or database constraint fails,
    the entire transaction is rolled back and ZERO records are inserted.
    """
    preview = preview_csv_import(entity_type=entity_type, csv_text=csv_text, actor_user=actor_user)
    if not preview["can_import"]:
        error_details = []
        for r in preview["rows"]:
            if not r["is_valid"]:
                error_details.append(f"Row {r['row_index']}: {'; '.join(r['errors'])}")
        raise ValueError(
            f"Cannot execute import: {preview['invalid_rows_count']} rows failed validation. "
            f"Fix errors and retry. Details: {' | '.join(error_details[:5])}"
        )

    clean_type = preview["entity_type"]
    created_ids: List[int] = []
    now_str = now_rome_iso()
    actor_id = actor_user.get("id")
    actor_name = actor_user.get("full_name") or actor_user.get("username", "system")

    conn = get_connection()
    try:
        with transaction(conn) as tx:
            # -----------------------------------------------------------------
            # 1. Import Members
            # -----------------------------------------------------------------
            if clean_type == "members":
                # Find the maximum sequence from existing member_number values (FD-MEM-XXXX)
                rows = tx.execute("SELECT member_number FROM members WHERE member_number LIKE 'FD-MEM-%';").fetchall()
                cur_seq = 1
                for r in rows:
                    if r["member_number"]:
                        match = re.search(r"FD-MEM-(\d+)", r["member_number"])
                        if match:
                            try:
                                val = int(match.group(1))
                                if val >= cur_seq:
                                    cur_seq = val + 1
                            except ValueError:
                                pass

                for row_info in preview["rows"]:
                    p = row_info["parsed"]
                    while True:
                        member_code = f"FD-MEM-{cur_seq:04d}"
                        exists = tx.execute("SELECT id FROM members WHERE member_number = ?;", (member_code,)).fetchone()
                        cur_seq += 1
                        if not exists:
                            break

                    cur = tx.execute(
                        """
                        INSERT INTO members (
                            member_number, full_name, email, phone,
                            membership_status, membership_expiry, notes,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            member_code,
                            p["full_name"],
                            p["email"],
                            p["phone"],
                            p["membership_status"],
                            p["membership_expiry"],
                            p["notes"],
                            now_str,
                            now_str,
                        ),
                    )
                    member_id = cur.lastrowid
                    created_ids.append(member_id)

                    for q in p.get("qualifications", []):
                        tx.execute(
                            """
                            INSERT INTO qualifications (
                                member_id, category_id, qualification_name,
                                issue_date, expiry_date, verified_by_user_id,
                                notes, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                member_id,
                                q["category_id"],
                                f"{q['category_name']} Qualification",
                                now_str[:10],
                                q["expiry_date"],
                                actor_id,
                                "Imported via CSV batch",
                                now_str,
                            ),
                        )

                    record_audit_event(
                        action="member.imported",
                        object_type="member",
                        object_id=member_id,
                        actor=actor_user,
                        details={"member_number": member_code, "email": p["email"], "qualifications_count": len(p.get("qualifications", []))},
                        after={"id": member_id, "member_number": member_code, "full_name": p["full_name"], "status": p["membership_status"]},
                        ip_address=ip_address,
                        conn=tx,
                    )

            # -----------------------------------------------------------------
            # 2. Import Machines
            # -----------------------------------------------------------------
            elif clean_type == "machines":
                for row_info in preview["rows"]:
                    p = row_info["parsed"]
                    cur = tx.execute(
                        """
                        INSERT INTO machines (
                            code, name, category_id, required_qualification_category_id,
                            capacity, state, operating_hours_start, operating_hours_end,
                            hourly_rate_cents, minimum_charge_cents, peak_hourly_rate_cents,
                            peak_hours_start, peak_hours_end, location, description,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            p["code"],
                            p["name"],
                            p["category_id"],
                            p["category_id"],
                            p["capacity"],
                            p["state"],
                            p["operating_hours_start"],
                            p["operating_hours_end"],
                            p["hourly_rate_cents"],
                            p["minimum_charge_cents"],
                            p["peak_hourly_rate_cents"],
                            p["peak_hours_start"],
                            p["peak_hours_end"],
                            p["location"],
                            p["description"],
                            now_str,
                            now_str,
                        ),
                    )
                    machine_id = cur.lastrowid
                    created_ids.append(machine_id)

                    record_audit_event(
                        action="machine.imported",
                        object_type="machine",
                        object_id=machine_id,
                        actor=actor_user,
                        details={"code": p["code"], "name": p["name"], "category_code": p["category_code"]},
                        after={"id": machine_id, "code": p["code"], "state": p["state"]},
                        ip_address=ip_address,
                        conn=tx,
                    )

            # -----------------------------------------------------------------
            # 3. Import Inventory Items
            # -----------------------------------------------------------------
            elif clean_type == "inventory":
                for row_info in preview["rows"]:
                    p = row_info["parsed"]
                    cur = tx.execute(
                        """
                        INSERT INTO inventory_items (
                            sku, name, category, unit, unit_cost_cents,
                            minimum_stock, location, description,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            p["sku"],
                            p["name"],
                            p["category"],
                            p["unit"],
                            p["unit_cost_cents"],
                            p["minimum_stock"],
                            p["location"],
                            p["description"],
                            now_str,
                            now_str,
                        ),
                    )
                    item_id = cur.lastrowid
                    created_ids.append(item_id)

                    init_qty = p.get("initial_quantity", 0)
                    if init_qty > 0:
                        tx.execute(
                            """
                            INSERT INTO inventory_ledger (
                                item_id, movement_type, quantity, unit_cost_cents,
                                reference_type, reference_id, reason, actor_id, actor_name,
                                created_at
                            ) VALUES (?, 'receipt', ?, ?, 'initial_stock', ?, 'Initial stock receipt from CSV import', ?, ?, ?);
                            """,
                            (
                                item_id,
                                init_qty,
                                p["unit_cost_cents"],
                                str(item_id),
                                actor_id,
                                actor_name,
                                now_str,
                            ),
                        )

                    record_audit_event(
                        action="inventory.imported",
                        object_type="inventory_item",
                        object_id=item_id,
                        actor=actor_user,
                        details={"sku": p["sku"], "name": p["name"], "initial_stock": init_qty},
                        after={"id": item_id, "sku": p["sku"], "stock": init_qty},
                        ip_address=ip_address,
                        conn=tx,
                    )

            # Batch Summary Audit Event
            record_audit_event(
                action="system.import_completed",
                object_type="system",
                object_id=clean_type,
                actor=actor_user,
                details={
                    "entity_type": clean_type,
                    "imported_count": len(created_ids),
                    "created_ids": created_ids,
                },
                ip_address=ip_address,
                conn=tx,
            )

        logger.info("CSV import executed successfully: %d %s items created by %s.", len(created_ids), clean_type, actor_name)
        return {
            "status": "success",
            "entity_type": clean_type,
            "imported_count": len(created_ids),
            "created_ids": created_ids,
        }
    finally:
        conn.close()


# =============================================================================
# CSV / JSON Export Engine
# =============================================================================

def export_members_csv() -> str:
    """Generate RFC 4180 CSV export of all members and their qualifications."""
    query = """
        SELECT m.id, m.member_number, m.full_name, m.email, m.phone,
               m.membership_status, m.membership_expiry, m.notes, m.created_at,
               GROUP_CONCAT(c.code || ':' || COALESCE(q.expiry_date, 'permanent'), ';') AS qualifications_list
        FROM members m
        LEFT JOIN qualifications q ON q.member_id = m.id
        LEFT JOIN machine_categories c ON c.id = q.category_id
        GROUP BY m.id
        ORDER BY m.id ASC;
    """
    rows = query_all(query)
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "ID", "Member Number", "Full Name", "Email", "Phone",
        "Membership Status", "Membership Expiry", "Notes", "Qualifications", "Created At"
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["member_number"], r["full_name"], r["email"], r["phone"] or "",
            r["membership_status"], r["membership_expiry"] or "", r["notes"] or "",
            r["qualifications_list"] or "", r["created_at"]
        ])
    return output.getvalue()


def export_members_json() -> str:
    """Generate structured JSON export of all members and qualifications."""
    members = query_all("SELECT * FROM members ORDER BY id ASC;")
    quals = query_all("""
        SELECT q.member_id, q.qualification_name, q.issue_date, q.expiry_date, c.code AS category_code, c.name AS category_name
        FROM qualifications q
        JOIN machine_categories c ON c.id = q.category_id
        ORDER BY q.member_id, q.id ASC;
    """)
    quals_by_member: Dict[int, List[Dict[str, Any]]] = {}
    for q in quals:
        quals_by_member.setdefault(q["member_id"], []).append(dict(q))

    results = []
    for m in members:
        rec = dict(m)
        rec["qualifications"] = quals_by_member.get(m["id"], [])
        results.append(rec)

    payload = {
        "app": "ForgeDesk",
        "entity": "members",
        "exported_at": now_rome_iso(),
        "total_count": len(results),
        "data": results,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def export_machines_csv() -> str:
    """Generate RFC 4180 CSV export of all machines and configuration."""
    query = """
        SELECT m.id, m.code, m.name, c.code AS category_code, c.name AS category_name,
               m.capacity, m.state, m.operating_hours_start, m.operating_hours_end,
               m.hourly_rate_cents, m.minimum_charge_cents, m.peak_hourly_rate_cents,
               m.peak_hours_start, m.peak_hours_end, m.location, m.description, m.created_at
        FROM machines m
        JOIN machine_categories c ON c.id = m.category_id
        ORDER BY m.id ASC;
    """
    rows = query_all(query)
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "ID", "Code", "Name", "Category Code", "Category Name",
        "Capacity", "State", "Operating Hours Start", "Operating Hours End",
        "Hourly Rate (EUR)", "Minimum Charge (EUR)", "Peak Hourly Rate (EUR)",
        "Peak Hours Start", "Peak Hours End", "Location", "Description", "Created At"
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["code"], r["name"], r["category_code"], r["category_name"],
            r["capacity"], r["state"], r["operating_hours_start"], r["operating_hours_end"],
            f"{r['hourly_rate_cents'] / 100:.2f}", f"{r['minimum_charge_cents'] / 100:.2f}",
            f"{r['peak_hourly_rate_cents'] / 100:.2f}", r["peak_hours_start"] or "",
            r["peak_hours_end"] or "", r["location"] or "", r["description"] or "", r["created_at"]
        ])
    return output.getvalue()


def export_machines_json() -> str:
    """Generate structured JSON export of all machines."""
    query = """
        SELECT m.*, c.code AS category_code, c.name AS category_name
        FROM machines m
        JOIN machine_categories c ON c.id = m.category_id
        ORDER BY m.id ASC;
    """
    rows = query_all(query)
    payload = {
        "app": "ForgeDesk",
        "entity": "machines",
        "exported_at": now_rome_iso(),
        "total_count": len(rows),
        "data": [dict(r) for r in rows],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def export_reservations_csv() -> str:
    """Generate RFC 4180 CSV export of reservations."""
    query = """
        SELECT r.id, r.start_time, r.end_time, r.status,
               r.actual_check_in, r.actual_check_out, r.recurrence_group_id,
               m.code AS machine_code, m.name AS machine_name,
               mem.member_number, mem.full_name AS member_name,
               u.final_charge_cents AS charge_cents, r.created_at
        FROM reservations r
        JOIN machines m ON m.id = r.machine_id
        JOIN members mem ON mem.id = r.member_id
        LEFT JOIN usage_charges u ON u.reservation_id = r.id
        ORDER BY r.start_time DESC;
    """
    rows = query_all(query)
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "ID", "Start Time (Europe/Rome)", "End Time (Europe/Rome)", "Status",
        "Actual Check-In", "Actual Check-Out", "Recurrence Group",
        "Machine Code", "Machine Name", "Member Number", "Member Name",
        "Charge (EUR)", "Created At"
    ])
    for r in rows:
        charge_str = f"{r['charge_cents'] / 100:.2f}" if r["charge_cents"] is not None else ""
        writer.writerow([
            r["id"], r["start_time"], r["end_time"], r["status"],
            r["actual_check_in"] or "", r["actual_check_out"] or "",
            r["recurrence_group_id"] or "", r["machine_code"], r["machine_name"],
            r["member_number"], r["member_name"], charge_str, r["created_at"]
        ])
    return output.getvalue()


def export_reservations_json() -> str:
    """Generate structured JSON export of all reservations."""
    query = """
        SELECT r.*, m.code AS machine_code, m.name AS machine_name,
               mem.member_number, mem.full_name AS member_name,
               u.final_charge_cents AS charge_cents, u.status AS charge_status
        FROM reservations r
        JOIN machines m ON m.id = r.machine_id
        JOIN members mem ON mem.id = r.member_id
        LEFT JOIN usage_charges u ON u.reservation_id = r.id
        ORDER BY r.start_time DESC;
    """
    rows = query_all(query)
    payload = {
        "app": "ForgeDesk",
        "entity": "reservations",
        "exported_at": now_rome_iso(),
        "total_count": len(rows),
        "data": [dict(r) for r in rows],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def export_inventory_csv() -> str:
    """Generate RFC 4180 CSV export of inventory stock items."""
    query = """
        SELECT i.id, i.sku, i.name, i.category, i.unit,
               i.unit_cost_cents, i.minimum_stock, i.location, i.description,
               COALESCE((
                   SELECT SUM(CASE WHEN movement_type IN ('receipt', 'release') THEN quantity WHEN movement_type IN ('consumption') THEN -quantity ELSE 0 END)
                   FROM inventory_ledger WHERE item_id = i.id
               ), 0) AS current_stock,
               i.created_at
        FROM inventory_items i
        ORDER BY i.sku ASC;
    """
    rows = query_all(query)
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "ID", "SKU", "Name", "Category", "Unit", "Unit Cost (EUR)",
        "Minimum Stock", "Current Stock", "Location", "Description", "Created At"
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["sku"], r["name"], r["category"], r["unit"],
            f"{r['unit_cost_cents'] / 100:.2f}", r["minimum_stock"],
            r["current_stock"], r["location"] or "", r["description"] or "", r["created_at"]
        ])
    return output.getvalue()


def export_inventory_ledger_csv() -> str:
    """Generate RFC 4180 CSV export of the immutable inventory ledger."""
    query = """
        SELECT l.id, l.created_at, i.sku, i.name AS item_name,
               l.movement_type, l.quantity, l.unit_cost_cents,
               l.reference_type, l.reference_id, l.reason,
               COALESCE(l.actor_name, u.username, 'system') AS actor_username
        FROM inventory_ledger l
        JOIN inventory_items i ON i.id = l.item_id
        LEFT JOIN users u ON u.id = l.actor_id
        ORDER BY l.id DESC;
    """
    rows = query_all(query)
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "ID", "Timestamp (Europe/Rome)", "SKU", "Item Name",
        "Movement Type", "Quantity", "Unit Cost (EUR)",
        "Reference Type", "Reference ID", "Reason", "Actor"
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["created_at"], r["sku"], r["item_name"],
            r["movement_type"], r["quantity"], f"{r['unit_cost_cents'] / 100:.2f}",
            r["reference_type"] or "", r["reference_id"] or "",
            r["reason"] or "", r["actor_username"] or "system"
        ])
    return output.getvalue()


def export_inventory_json() -> str:
    """Generate structured JSON export of inventory items and ledger transactions."""
    items = query_all("SELECT * FROM inventory_items ORDER BY sku ASC;")
    ledger = query_all("SELECT * FROM inventory_ledger ORDER BY id ASC;")
    payload = {
        "app": "ForgeDesk",
        "entity": "inventory",
        "exported_at": now_rome_iso(),
        "total_items": len(items),
        "total_ledger_records": len(ledger),
        "items": [dict(i) for i in items],
        "ledger": [dict(l) for l in ledger],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def export_charges_csv() -> str:
    """Generate RFC 4180 CSV export of usage charges and adjustments."""
    query = """
        SELECT c.id, c.created_at, r.id AS reservation_id,
               m.code AS machine_code, m.name AS machine_name,
               mem.member_number, mem.full_name AS member_name,
               c.duration_minutes, c.base_charge_cents, c.final_charge_cents,
               c.status,
               COALESCE((
                   SELECT SUM(adjustment_cents) FROM charge_adjustments WHERE charge_id = c.id
               ), 0) AS total_adjustments_cents
        FROM usage_charges c
        JOIN reservations r ON r.id = c.reservation_id
        JOIN machines m ON m.id = c.machine_id
        JOIN members mem ON mem.id = c.member_id
        ORDER BY c.id DESC;
    """
    rows = query_all(query)
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "Charge ID", "Finalized At", "Reservation ID", "Machine Code", "Machine Name",
        "Member Number", "Member Name", "Duration (Minutes)",
        "Base Charge (EUR)", "Final Charge (EUR)", "Total Adjustments (EUR)", "Status"
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["created_at"], r["reservation_id"], r["machine_code"], r["machine_name"],
            r["member_number"], r["member_name"], r["duration_minutes"],
            f"{r['base_charge_cents'] / 100:.2f}", f"{r['final_charge_cents'] / 100:.2f}",
            f"{r['total_adjustments_cents'] / 100:.2f}", r["status"]
        ])
    return output.getvalue()


def export_charges_json() -> str:
    """Generate structured JSON export of charges and adjustment history."""
    charges = query_all("SELECT * FROM usage_charges ORDER BY id ASC;")
    adjustments = query_all("SELECT * FROM charge_adjustments ORDER BY id ASC;")
    adjustments_by_charge: Dict[int, List[Dict[str, Any]]] = {}
    for a in adjustments:
        adjustments_by_charge.setdefault(a["charge_id"], []).append(dict(a))

    results = []
    for c in charges:
        rec = dict(c)
        rec["adjustments"] = adjustments_by_charge.get(c["id"], [])
        results.append(rec)

    payload = {
        "app": "ForgeDesk",
        "entity": "charges",
        "exported_at": now_rome_iso(),
        "total_charges": len(results),
        "data": results,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def export_full_system_json() -> str:
    """Generate a complete structured JSON database dump of all application tables."""
    tables = [
        "users", "sessions", "members", "machine_categories", "qualifications",
        "machines", "maintenance_windows", "reservations", "waiting_list",
        "inventory_items", "inventory_ledger", "usage_charges", "charge_adjustments",
        "maintenance_jobs", "incidents", "incident_attachments", "audit_log", "schema_migrations"
    ]
    dump: Dict[str, Any] = {
        "app": "ForgeDesk",
        "export_type": "full_system_database_dump",
        "exported_at": now_rome_iso(),
        "database_file": str(DB_PATH.name),
        "tables": {},
    }

    conn = get_connection()
    try:
        for t in tables:
            try:
                rows = conn.execute(f"SELECT * FROM {t};").fetchall()
                sanitized_rows = []
                for r in rows:
                    row_dict = dict(r)
                    if t == "users":
                        row_dict["password_hash"] = "[REDACTED]"
                        row_dict["password_salt"] = "[REDACTED]"
                    elif t == "sessions":
                        row_dict["session_token"] = "[REDACTED]"
                    sanitized_rows.append(row_dict)
                dump["tables"][t] = sanitized_rows
            except Exception as e:
                logger.warning("Could not dump table '%s': %s", t, e)
    finally:
        conn.close()

    return json.dumps(dump, indent=2, ensure_ascii=False)


# =============================================================================
# Online Database Backup & Restore Engine
# =============================================================================

def create_database_backup(
    destination_path: Optional[Union[str, Path]] = None,
    actor_user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Path:
    """Perform a live, non-blocking online SQLite database backup using the SQLite backup API.
    Guarantees page-level consistency with zero downtime and zero table locking issues.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    if destination_path is None:
        timestamp_str = now_rome().strftime("%Y%m%d_%H%M%S")
        dest_file = BACKUP_DIR / f"forgedesk_backup_{timestamp_str}.db"
    else:
        dest_file = Path(destination_path)
        dest_file.parent.mkdir(parents=True, exist_ok=True)

    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Source database file '{DB_PATH}' does not exist.")

    source_conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        source_conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    except Exception:
        pass
    dest_conn = sqlite3.connect(str(dest_file), timeout=30.0)

    try:
        with dest_conn:
            source_conn.backup(dest_conn, pages=100, sleep=0.01)
        dest_conn.close()
    finally:
        source_conn.close()

    backup_size = dest_file.stat().st_size
    record_audit_event(
        action="system.backup_created",
        object_type="system",
        object_id=str(dest_file.name),
        actor=actor_user,
        details={
            "backup_filename": dest_file.name,
            "backup_path": str(dest_file),
            "size_bytes": backup_size,
            "size_formatted": f"{backup_size / (1024 * 1024):.2f} MB",
        },
        ip_address=ip_address,
    )

    logger.info("Online database backup created at '%s' (%d bytes).", dest_file, backup_size)
    return dest_file


def validate_backup_file(backup_path: Union[str, Path]) -> Dict[str, Any]:
    """Validate that a file is a valid, uncorrupted ForgeDesk SQLite backup."""
    bpath = Path(backup_path)
    if not bpath.is_file():
        raise FileNotFoundError(f"Backup file '{bpath}' does not exist.")

    if bpath.stat().st_size < 512:
        raise ValueError("Backup file is too small to be a valid SQLite database.")

    with open(bpath, "rb") as f:
        header = f.read(16)
        if not header.startswith(b"SQLite format 3\x00"):
            raise ValueError("File is not a valid SQLite database (invalid header).")

    conn = sqlite3.connect(f"file:{bpath}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {r[0] for r in cur.fetchall()}

        required_tables = {"users", "members", "machines", "reservations", "audit_log", "schema_migrations"}
        missing_tables = required_tables - tables
        if missing_tables:
            raise ValueError(f"Backup database is missing required ForgeDesk tables: {', '.join(sorted(missing_tables))}")

        user_count = conn.execute("SELECT COUNT(*) AS c FROM users;").fetchone()[0]
        reservation_count = conn.execute("SELECT COUNT(*) AS c FROM reservations;").fetchone()[0]
        audit_count = conn.execute("SELECT COUNT(*) AS c FROM audit_log;").fetchone()[0]

        return {
            "is_valid": True,
            "filename": bpath.name,
            "size_bytes": bpath.stat().st_size,
            "table_count": len(tables),
            "tables": sorted(list(tables)),
            "user_count": user_count,
            "reservation_count": reservation_count,
            "audit_count": audit_count,
        }
    finally:
        conn.close()


def restore_database_backup(
    backup_path: Union[str, Path],
    actor_user: Dict[str, Any],
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Restore database from a validated backup file atomically.
    Creates a pre-restore safety snapshot before replacing the active database.
    """
    bpath = Path(backup_path)
    validation = validate_backup_file(bpath)

    safety_snapshot = None
    if DB_PATH.is_file():
        try:
            chk_conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
            chk_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            chk_conn.close()
        except Exception as e:
            logger.warning("Could not execute pre-restore wal_checkpoint on '%s': %s", DB_PATH, e)

        timestamp_str = now_rome().strftime("%Y%m%d_%H%M%S")
        safety_snapshot = BACKUP_DIR / f"pre_restore_safety_{timestamp_str}.db"
        safety_snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DB_PATH, safety_snapshot)
        logger.info("Pre-restore safety snapshot created at '%s'", safety_snapshot)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_target = DB_PATH.with_suffix(".restoring.tmp")
    shutil.copy2(bpath, temp_target)
    os.replace(temp_target, DB_PATH)

    # Clean up any leftover WAL/SHM auxiliary files from the previous database state
    for aux_suffix in ("-wal", "-shm"):
        aux_file = DB_PATH.parent / f"{DB_PATH.name}{aux_suffix}"
        if aux_file.exists():
            try:
                aux_file.unlink()
                logger.debug("Removed stale WAL auxiliary file '%s' during restore.", aux_file)
            except Exception as e:
                logger.warning("Could not remove auxiliary file '%s': %s", aux_file, e)

    applied = apply_migrations()

    record_audit_event(
        action="system.database_restored",
        object_type="system",
        object_id=str(bpath.name),
        actor=actor_user,
        details={
            "source_backup_file": bpath.name,
            "source_backup_path": str(bpath),
            "pre_restore_safety_snapshot": str(safety_snapshot) if safety_snapshot else None,
            "validation_summary": validation,
            "migrations_applied": applied,
        },
        ip_address=ip_address,
    )

    logger.info("Database successfully restored from '%s' by %s.", bpath.name, actor_user.get("username"))
    return {
        "status": "success",
        "restored_from": bpath.name,
        "validation": validation,
        "migrations_applied": applied,
        "safety_snapshot": str(safety_snapshot) if safety_snapshot else None,
    }
