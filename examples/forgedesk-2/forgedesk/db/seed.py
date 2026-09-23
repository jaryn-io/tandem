"""Representative and comprehensive seed dataset for ForgeDesk Makerspace."""

import datetime
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional

from forgedesk.db.connection import get_connection, transaction
from forgedesk.utils.crypto import hash_password
from forgedesk.utils.datetime_tz import now_rome, now_rome_iso, parse_datetime

logger = logging.getLogger("forgedesk.seed")


DEMO_USERS = [
    {
        "username": "admin",
        "email": "admin@forgedesk.local",
        "password": "admin123",
        "full_name": "Valerio Galli (Admin)",
        "role": "admin",
    },
    {
        "username": "operator",
        "email": "operator@forgedesk.local",
        "password": "operator123",
        "full_name": "Elena Ferri (Operator)",
        "role": "operator",
    },
    {
        "username": "member_alice",
        "email": "alice@makers.local",
        "password": "member123",
        "full_name": "Alice Moretti",
        "role": "member",
    },
    {
        "username": "member_bob",
        "email": "bob@makers.local",
        "password": "member123",
        "full_name": "Bob Rossi",
        "role": "member",
    },
    {
        "username": "member_clara",
        "email": "clara@makers.local",
        "password": "member123",
        "full_name": "Clara Bianchi",
        "role": "member",
    },
    {
        "username": "viewer",
        "email": "viewer@forgedesk.local",
        "password": "viewer123",
        "full_name": "Gianni Conti (Auditor/Viewer)",
        "role": "viewer",
    },
]

DEMO_CATEGORIES = [
    {
        "code": "3d_printers",
        "name": "3D Printers (FDM / SLA)",
        "description": "Rapid prototyping polymer additive manufacturing machines.",
    },
    {
        "code": "laser_cutters",
        "name": "Laser Cutters & Engravers",
        "description": "CO2 and fiber precision laser sheet cutting and engraving.",
    },
    {
        "code": "cnc_mills",
        "name": "CNC Milling & Machining",
        "description": "Subtractive multi-axis machining for metals and composites.",
    },
    {
        "code": "woodworking",
        "name": "Woodworking & Joinery",
        "description": "Heavy machinery for precision timber processing and joining.",
    },
    {
        "code": "electronics",
        "name": "Electronics & Soldering Lab",
        "description": "SMD soldering, oscilloscopes, power supplies and test gear.",
    },
]


def seed_database(conn: Optional[sqlite3.Connection] = None, force: bool = False) -> Dict[str, int]:
    """Populate database with rich representative demonstrator data if empty or forced."""
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        user_count = conn.execute("SELECT COUNT(*) AS c FROM users;").fetchone()["c"]
        if user_count > 0 and not force:
            logger.info("Database already contains data (%d users). Skipping seed.", user_count)
            return {"status": "skipped", "existing_users": user_count}

        counts: Dict[str, int] = {}
        now = now_rome()
        now_str = now_rome_iso()

        with transaction(conn) as tx:
            if force:
                logger.info("Force seeding: clearing existing application data...")
                tx.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_delete;")
                tx.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_update;")
                tx.execute("DROP TRIGGER IF EXISTS trg_usage_charges_no_delete;")
                tx.execute("DROP TRIGGER IF EXISTS trg_usage_charges_protect_snapshot;")
                tx.execute("DROP TRIGGER IF EXISTS trg_charge_adjustments_no_delete;")
                tx.execute("DROP TRIGGER IF EXISTS trg_charge_adjustments_no_update;")
                tx.execute("DROP TRIGGER IF EXISTS trg_inventory_ledger_no_delete;")
                tx.execute("DROP TRIGGER IF EXISTS trg_inventory_ledger_no_update;")
                tables_to_clear = [
                    "audit_log", "incident_attachments", "incidents", "maintenance_jobs",
                    "charge_adjustments", "usage_charges", "inventory_ledger", "inventory_items",
                    "waiting_list", "reservations", "maintenance_windows", "machines",
                    "qualifications", "machine_categories", "members", "sessions", "users"
                ]
                for t in tables_to_clear:
                    tx.execute(f"DELETE FROM {t};")

            # 1. Seed Users and Members
            user_ids: Dict[str, int] = {}
            member_ids: Dict[str, int] = {}

            for u in DEMO_USERS:
                pwd_hash, pwd_salt = hash_password(u["password"])
                cur = tx.execute(
                    """
                    INSERT INTO users (username, email, password_hash, password_salt, full_name, role, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?);
                    """,
                    (u["username"], u["email"], pwd_hash, pwd_salt, u["full_name"], u["role"], now_str, now_str),
                )
                uid = cur.lastrowid
                user_ids[u["username"]] = uid

                # Audit user creation
                tx.execute(
                    """
                    INSERT INTO audit_log (actor_id, actor_name, action, object_type, object_id, details_json, created_at)
                    VALUES (?, ?, 'user.created', 'user', ?, ?, ?);
                    """,
                    (uid, "system", str(uid), json.dumps({"username": u["username"], "role": u["role"]}), now_str),
                )

                # Create associated Member profile for member users
                if u["role"] == "member" or u["username"] in ("member_alice", "member_bob", "member_clara"):
                    seq = len(member_ids) + 1
                    mem_no = f"FD-MEM-{seq:04d}"
                    status = "active"
                    cur_m = tx.execute(
                        """
                        INSERT INTO members (user_id, member_number, full_name, email, phone, membership_status, membership_expiry, notes, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            uid,
                            mem_no,
                            u["full_name"],
                            u["email"],
                            f"+39 340 555 0{seq:03d}",
                            status,
                            (now + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                            f"Demonstration makerspace member {u['full_name']}",
                            now_str,
                            now_str,
                        ),
                    )
                    member_ids[u["username"]] = cur_m.lastrowid

            counts["users"] = len(user_ids)
            counts["members"] = len(member_ids)

            # 2. Seed Machine Categories
            cat_ids: Dict[str, int] = {}
            for cat in DEMO_CATEGORIES:
                cur = tx.execute(
                    "INSERT INTO machine_categories (code, name, description, created_at) VALUES (?, ?, ?, ?);",
                    (cat["code"], cat["name"], cat["description"], now_str),
                )
                cat_ids[cat["code"]] = cur.lastrowid
            counts["categories"] = len(cat_ids)

            # 3. Seed Qualifications
            # Alice: 3D printing & Laser cutting (Valid)
            tx.execute(
                """
                INSERT INTO qualifications (member_id, category_id, qualification_name, issue_date, expiry_date, verified_by_user_id, notes, created_at)
                VALUES (?, ?, 'Certified 3D Printing Operator', '2026-01-10', '2027-01-10', ?, 'Passed hands-on safety test', ?);
                """,
                (member_ids["member_alice"], cat_ids["3d_printers"], user_ids["operator"], now_str),
            )
            tx.execute(
                """
                INSERT INTO qualifications (member_id, category_id, qualification_name, issue_date, expiry_date, verified_by_user_id, notes, created_at)
                VALUES (?, ?, 'High-Power Laser Safety & Operation', '2026-02-01', '2027-02-01', ?, 'Level 2 Optical Safety Certified', ?);
                """,
                (member_ids["member_alice"], cat_ids["laser_cutters"], user_ids["admin"], now_str),
            )

            # Bob: 3D Printing (Valid) & CNC Mills (Expired)
            tx.execute(
                """
                INSERT INTO qualifications (member_id, category_id, qualification_name, issue_date, expiry_date, verified_by_user_id, notes, created_at)
                VALUES (?, ?, 'Standard FDM 3D Printing', '2025-06-01', NULL, ?, 'Lifetime basic qualification', ?);
                """,
                (member_ids["member_bob"], cat_ids["3d_printers"], user_ids["operator"], now_str),
            )
            tx.execute(
                """
                INSERT INTO qualifications (member_id, category_id, qualification_name, issue_date, expiry_date, verified_by_user_id, notes, created_at)
                VALUES (?, ?, 'CNC 3-Axis Mill Fundamentals', '2025-01-15', '2026-07-15', ?, 'Annual recertification required (EXPIRED)', ?);
                """,
                (member_ids["member_bob"], cat_ids["cnc_mills"], user_ids["admin"], now_str),
            )

            # Clara: Laser cutting (Valid)
            tx.execute(
                """
                INSERT INTO qualifications (member_id, category_id, qualification_name, issue_date, expiry_date, verified_by_user_id, notes, created_at)
                VALUES (?, ?, 'Laser Safety Operator Level 1', '2026-03-01', '2027-03-01', ?, 'Trained on Epilog Fusion Pro', ?);
                """,
                (member_ids["member_clara"], cat_ids["laser_cutters"], user_ids["operator"], now_str),
            )
            counts["qualifications"] = 5

            # 4. Seed Machines
            machines_data = [
                {
                    "code": "PRUSA-MK4-01",
                    "name": "Prusa MK4 FDM #1 (3D Printer)",
                    "cat": "3d_printers",
                    "req_cat": "3d_printers",
                    "capacity": 1,
                    "state": "available",
                    "op_start": "08:00",
                    "op_end": "22:00",
                    "hourly": 200,  # €2.00/h
                    "min_charge": 100,  # €1.00
                    "peak_hourly": 300,  # €3.00/h
                    "peak_start": "17:00",
                    "peak_end": "21:00",
                    "location": "3D Print Lab, Bench A1",
                    "desc": "High precision FDM printer with Nextruder and automated bed leveling.",
                },
                {
                    "code": "BAMBU-X1C-01",
                    "name": "Bambu Lab X1-Carbon #1",
                    "cat": "3d_printers",
                    "req_cat": "3d_printers",
                    "capacity": 1,
                    "state": "available",
                    "op_start": "08:00",
                    "op_end": "22:00",
                    "hourly": 300,  # €3.00/h
                    "min_charge": 150,  # €1.50
                    "peak_hourly": 400,  # €4.00/h
                    "peak_start": "17:00",
                    "peak_end": "21:00",
                    "location": "3D Print Lab, Bench A2",
                    "desc": "High speed multi-material enclosed 3D printer with lidar calibration.",
                },
                {
                    "code": "EPILOG-FUSION-PRO",
                    "name": "Epilog Fusion Pro 48 (120W Laser)",
                    "cat": "laser_cutters",
                    "req_cat": "laser_cutters",
                    "capacity": 1,
                    "state": "available",
                    "op_start": "08:00",
                    "op_end": "22:00",
                    "hourly": 1200,  # €12.00/h
                    "min_charge": 600,  # €6.00
                    "peak_hourly": 1600,  # €16.00/h
                    "peak_start": "17:00",
                    "peak_end": "21:00",
                    "location": "Laser Room Lab 2",
                    "desc": "Industrial 120W CO2 laser cutter with 1219 x 914 mm working bed.",
                },
                {
                    "code": "HAAS-MINI-MILL",
                    "name": "Haas Mini Mill (3-Axis CNC)",
                    "cat": "cnc_mills",
                    "req_cat": "cnc_mills",
                    "capacity": 1,
                    "state": "under_maintenance",
                    "op_start": "08:00",
                    "op_end": "20:00",
                    "hourly": 2500,  # €25.00/h
                    "min_charge": 1500,  # €15.00
                    "peak_hourly": 3000,  # €30.00/h
                    "peak_start": "17:00",
                    "peak_end": "20:00",
                    "location": "Heavy Machining Bay",
                    "desc": "Precision vertical machining center with 10-station automatic tool changer.",
                },
                {
                    "code": "ALTENDORF-F45",
                    "name": "Altendorf F45 Sliding Table Saw",
                    "cat": "woodworking",
                    "req_cat": "woodworking",
                    "capacity": 1,
                    "state": "available",
                    "op_start": "09:00",
                    "op_end": "21:00",
                    "hourly": 800,  # €8.00/h
                    "min_charge": 400,  # €4.00
                    "peak_hourly": 1000,  # €10.00/h
                    "peak_start": "17:00",
                    "peak_end": "21:00",
                    "location": "Woodworking Shop",
                    "desc": "Sliding table panel saw with hydraulic tilting arbor.",
                },
                {
                    "code": "ROLAND-MDX-40",
                    "name": "Roland MDX-40 Milling Machine (Legacy)",
                    "cat": "cnc_mills",
                    "req_cat": "cnc_mills",
                    "capacity": 1,
                    "state": "retired",
                    "op_start": "08:00",
                    "op_end": "20:00",
                    "hourly": 500,
                    "min_charge": 250,
                    "peak_hourly": 600,
                    "peak_start": "17:00",
                    "peak_end": "20:00",
                    "location": "Storage Archive",
                    "desc": "Retired desktop milling machine preserved for historical reference.",
                },
            ]

            machine_ids: Dict[str, int] = {}
            for m in machines_data:
                cur = tx.execute(
                    """
                    INSERT INTO machines (code, name, category_id, required_qualification_category_id, capacity, state,
                                          operating_hours_start, operating_hours_end, hourly_rate_cents, minimum_charge_cents,
                                          peak_hourly_rate_cents, peak_hours_start, peak_hours_end, location, description,
                                          created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        m["code"],
                        m["name"],
                        cat_ids[m["cat"]],
                        cat_ids.get(m["req_cat"]),
                        m["capacity"],
                        m["state"],
                        m["op_start"],
                        m["op_end"],
                        m["hourly"],
                        m["min_charge"],
                        m["peak_hourly"],
                        m["peak_start"],
                        m["peak_end"],
                        m["location"],
                        m["desc"],
                        now_str,
                        now_str,
                    ),
                )
                machine_ids[m["code"]] = cur.lastrowid
            counts["machines"] = len(machine_ids)

            # 5. Seed Maintenance Windows
            maint_start = (now + datetime.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            maint_end = maint_start + datetime.timedelta(hours=4)
            tx.execute(
                """
                INSERT INTO maintenance_windows (machine_id, title, start_time, end_time, status, created_by_user_id, notes, created_at)
                VALUES (?, 'Haas Mini Mill Spindle Bearing Lubrication & Calibration', ?, ?, 'scheduled', ?, 'Quarterly OEM service schedule', ?);
                """,
                (machine_ids["HAAS-MINI-MILL"], maint_start.isoformat(), maint_end.isoformat(), user_ids["operator"], now_str),
            )
            counts["maintenance_windows"] = 1

            # 6. Seed Reservations
            # A. Historical finished reservation for Alice on Prusa MK4
            hist_start = (now - datetime.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
            hist_end = hist_start + datetime.timedelta(hours=2)
            cur_r1 = tx.execute(
                """
                INSERT INTO reservations (machine_id, member_id, title, start_time, end_time, status, actual_check_in, actual_check_out, created_by_user_id, created_at, updated_at)
                VALUES (?, ?, '3D Printing Drone Arm Prototype', ?, ?, 'checked_out', ?, ?, ?, ?, ?);
                """,
                (
                    machine_ids["PRUSA-MK4-01"],
                    member_ids["member_alice"],
                    hist_start.isoformat(),
                    hist_end.isoformat(),
                    hist_start.isoformat(),
                    hist_end.isoformat(),
                    user_ids["member_alice"],
                    now_str,
                    now_str,
                ),
            )
            res1_id = cur_r1.lastrowid

            # B. Upcoming confirmed reservation for Alice on Epilog Laser Cutter
            up_start = (now + datetime.timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
            up_end = up_start + datetime.timedelta(hours=2)
            cur_r2 = tx.execute(
                """
                INSERT INTO reservations (machine_id, member_id, title, start_time, end_time, status, created_by_user_id, created_at, updated_at)
                VALUES (?, ?, 'Laser Cutting Architectural Acrylic Model', ?, ?, 'confirmed', ?, ?, ?);
                """,
                (
                    machine_ids["EPILOG-FUSION-PRO"],
                    member_ids["member_alice"],
                    up_start.isoformat(),
                    up_end.isoformat(),
                    user_ids["member_alice"],
                    now_str,
                    now_str,
                ),
            )
            res2_id = cur_r2.lastrowid

            # C. Historical reservation on retired machine
            ret_start = (now - datetime.timedelta(days=90)).replace(hour=11, minute=0, second=0, microsecond=0)
            ret_end = ret_start + datetime.timedelta(hours=3)
            tx.execute(
                """
                INSERT INTO reservations (machine_id, member_id, title, start_time, end_time, status, actual_check_in, actual_check_out, created_by_user_id, created_at, updated_at)
                VALUES (?, ?, 'PCB Milling Isolation Routing', ?, ?, 'checked_out', ?, ?, ?, ?, ?);
                """,
                (
                    machine_ids["ROLAND-MDX-40"],
                    member_ids["member_bob"],
                    ret_start.isoformat(),
                    ret_end.isoformat(),
                    ret_start.isoformat(),
                    ret_end.isoformat(),
                    user_ids["member_bob"],
                    ret_start.isoformat(),
                    ret_end.isoformat(),
                ),
            )
            counts["reservations"] = 3

            # 7. Seed Waiting List
            tx.execute(
                """
                INSERT INTO waiting_list (machine_id, member_id, desired_start_time, desired_end_time, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'waiting', ?, ?);
                """,
                (
                    machine_ids["EPILOG-FUSION-PRO"],
                    member_ids["member_clara"],
                    up_start.isoformat(),
                    up_end.isoformat(),
                    now_str,
                    now_str,
                ),
            )
            counts["waiting_list"] = 1

            # 8. Seed Consumable Inventory Items
            inv_items = [
                {
                    "sku": "FIL-PLA-BLK-1KG",
                    "name": "PLA Filament 1.75mm Black (1kg)",
                    "category": "Consumables",
                    "unit": "spool",
                    "unit_cost": 1800,  # €18.00
                    "min_stock": 3,
                    "location": "Shelf 3D-01",
                    "desc": "Premium PLA filament for reliable FDM printing.",
                },
                {
                    "sku": "FIL-PETG-CLR-1KG",
                    "name": "PETG Filament 1.75mm Clear (1kg)",
                    "category": "Consumables",
                    "unit": "spool",
                    "unit_cost": 2200,  # €22.00
                    "min_stock": 2,
                    "location": "Shelf 3D-02",
                    "desc": "High strength translucent PETG filament.",
                },
                {
                    "sku": "PLY-BIRCH-3MM",
                    "name": "Baltic Birch Plywood 600x400x3mm",
                    "category": "Sheet Materials",
                    "unit": "sheet",
                    "unit_cost": 650,  # €6.50
                    "min_stock": 10,
                    "location": "Rack W-01",
                    "desc": "Grade BB/BB laser-compatible birch plywood sheet.",
                },
                {
                    "sku": "ACR-CLR-5MM",
                    "name": "Cast Acrylic Sheet Clear 600x400x5mm",
                    "category": "Sheet Materials",
                    "unit": "sheet",
                    "unit_cost": 1400,  # €14.00
                    "min_stock": 5,
                    "location": "Rack L-02",
                    "desc": "Optically clear cast PMMA acrylic sheet.",
                },
                {
                    "sku": "NOZ-E3D-04",
                    "name": "Brass Nozzle 0.4mm High Flow",
                    "category": "Replacement Parts",
                    "unit": "pcs",
                    "unit_cost": 450,  # €4.50
                    "min_stock": 4,
                    "location": "Parts Drawer D-03",
                    "desc": "Replacement nozzle for Prusa MK4 Nextruder.",
                },
                {
                    "sku": "ENDMILL-CARB-6MM",
                    "name": "Solid Carbide 2-Flute Endmill 6mm",
                    "category": "Cutting Tools",
                    "unit": "pcs",
                    "unit_cost": 3200,  # €32.00
                    "min_stock": 2,
                    "location": "Tool Cabinet T-01",
                    "desc": "AlTiN coated carbide milling cutter for aluminum and wood.",
                },
            ]

            item_ids: Dict[str, int] = {}
            for item in inv_items:
                cur = tx.execute(
                    """
                    INSERT INTO inventory_items (sku, name, category, unit, unit_cost_cents, minimum_stock, location, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        item["sku"],
                        item["name"],
                        item["category"],
                        item["unit"],
                        item["unit_cost"],
                        item["min_stock"],
                        item["location"],
                        item["desc"],
                        now_str,
                        now_str,
                    ),
                )
                item_ids[item["sku"]] = cur.lastrowid
            counts["inventory_items"] = len(item_ids)

            # 9. Seed Immutable Inventory Ledger
            # Initial receipts
            initial_stock_quantities = {
                "FIL-PLA-BLK-1KG": 10,
                "FIL-PETG-CLR-1KG": 6,
                "PLY-BIRCH-3MM": 25,
                "ACR-CLR-5MM": 12,
                "NOZ-E3D-04": 8,
                "ENDMILL-CARB-6MM": 4,
            }

            for sku, qty in initial_stock_quantities.items():
                tx.execute(
                    """
                    INSERT INTO inventory_ledger (item_id, movement_type, quantity, unit_cost_cents, reference_type, reference_id, reason, actor_id, actor_name, created_at)
                    VALUES (?, 'receipt', ?, ?, 'initial_stock', 'INIT-2026-001', 'Initial warehouse stock receipt', ?, 'Elena Ferri (Operator)', ?);
                    """,
                    (item_ids[sku], qty, 1800, user_ids["operator"], now_str),
                )

            # Record consumption for Alice's completed 3D print
            tx.execute(
                """
                INSERT INTO inventory_ledger (item_id, movement_type, quantity, unit_cost_cents, reference_type, reference_id, reason, actor_id, actor_name, created_at)
                VALUES (?, 'consumption', -1, 1800, 'reservation', ?, 'Consumed 1 spool black PLA for drone prototype', ?, 'Alice Moretti', ?);
                """,
                (item_ids["FIL-PLA-BLK-1KG"], str(res1_id), user_ids["member_alice"], now_str),
            )

            # Record manual correction with mandatory reason
            tx.execute(
                """
                INSERT INTO inventory_ledger (item_id, movement_type, quantity, unit_cost_cents, reference_type, reference_id, reason, actor_id, actor_name, created_at)
                VALUES (?, 'correction', -1, 650, 'manual', 'CORR-2026-01', 'Damaged sheet discarded during monthly safety inspection', ?, 'Valerio Galli (Admin)', ?);
                """,
                (item_ids["PLY-BIRCH-3MM"], user_ids["admin"], now_str),
            )
            counts["ledger_entries"] = len(initial_stock_quantities) + 2

            # 10. Seed Usage Charges and Adjustments
            rate_breakdown = {
                "regular_hours": 2.0,
                "regular_rate_cents": 200,
                "peak_hours": 0.0,
                "peak_rate_cents": 300,
                "minimum_charge_cents": 100,
                "computed_total_cents": 400,
            }
            cur_ch = tx.execute(
                """
                INSERT INTO usage_charges (reservation_id, machine_id, member_id, start_time, end_time, duration_minutes, rate_breakdown_json, base_charge_cents, final_charge_cents, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 120, ?, 400, 400, 'adjusted', ?, ?);
                """,
                (
                    res1_id,
                    machine_ids["PRUSA-MK4-01"],
                    member_ids["member_alice"],
                    hist_start.isoformat(),
                    hist_end.isoformat(),
                    json.dumps(rate_breakdown),
                    now_str,
                    now_str,
                ),
            )
            charge1_id = cur_ch.lastrowid

            # Charge adjustment
            tx.execute(
                """
                INSERT INTO charge_adjustments (charge_id, adjustment_cents, reason, actor_id, created_at)
                VALUES (?, -100, 'Member loyalty promotional discount applied by administrator', ?, ?);
                """,
                (charge1_id, user_ids["admin"], now_str),
            )
            counts["usage_charges"] = 1
            counts["charge_adjustments"] = 1

            # 11. Seed Maintenance Jobs and Incidents
            tx.execute(
                """
                INSERT INTO maintenance_jobs (machine_id, title, description, priority, status, assigned_to_user_id, opened_by_user_id, scheduled_date, notes, created_at, updated_at)
                VALUES (?, 'Quarterly Axis Lubrication & Belt Tensioning', 'Check X/Y belt resonance and apply synthetic PTFE grease to linear rails', 'medium', 'open', ?, ?, '2026-09-05', 'Standard periodic maintenance', ?, ?);
                """,
                (machine_ids["PRUSA-MK4-01"], user_ids["operator"], user_ids["operator"], now_str, now_str),
            )
            counts["maintenance_jobs"] = 1

            tx.execute(
                """
                INSERT INTO incidents (machine_id, title, description, severity, status, reported_by_user_id, takes_machine_out_of_service, created_at, updated_at)
                VALUES (?, 'Spindle Inverter Overcurrent Alarm (E-04)', 'Spindle drive tripped during high-feed roughing pass on 6082 aluminum billet.', 'major', 'investigating', ?, 1, ?, ?);
                """,
                (machine_ids["HAAS-MINI-MILL"], user_ids["operator"], now_str, now_str),
            )
            counts["incidents"] = 1

            # Audit record for database seed completion
            tx.execute(
                """
                INSERT INTO audit_log (actor_id, actor_name, action, object_type, object_id, details_json, created_at)
                VALUES (?, 'system', 'system.database_seeded', 'database', 'main', ?, ?);
                """,
                (user_ids["admin"], json.dumps({"counts": counts}), now_str),
            )

            # Re-ensure immutability triggers for audit_log, usage_charges, charge_adjustments, and inventory_ledger
            tx.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update
                BEFORE UPDATE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'Audit log is append-only: updates are prohibited.');
                END;
            """)
            tx.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete
                BEFORE DELETE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'Audit log is append-only: deletions are prohibited.');
                END;
            """)
            tx.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_usage_charges_no_delete
                BEFORE DELETE ON usage_charges
                BEGIN
                    SELECT RAISE(ABORT, 'Usage charges are immutable financial records: deletions are prohibited.');
                END;
            """)
            tx.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_usage_charges_protect_snapshot
                BEFORE UPDATE ON usage_charges
                BEGIN
                    SELECT CASE
                        WHEN OLD.id != NEW.id
                          OR OLD.reservation_id != NEW.reservation_id
                          OR OLD.machine_id != NEW.machine_id
                          OR OLD.member_id != NEW.member_id
                          OR OLD.start_time != NEW.start_time
                          OR OLD.end_time != NEW.end_time
                          OR OLD.duration_minutes != NEW.duration_minutes
                          OR OLD.rate_breakdown_json != NEW.rate_breakdown_json
                          OR OLD.base_charge_cents != NEW.base_charge_cents
                          OR OLD.created_at != NEW.created_at
                        THEN RAISE(ABORT, 'Usage charge original rate snapshot fields are immutable and cannot be updated.')
                    END;
                END;
            """)
            tx.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_charge_adjustments_no_update
                BEFORE UPDATE ON charge_adjustments
                BEGIN
                    SELECT RAISE(ABORT, 'Charge adjustments ledger is append-only: updates are prohibited.');
                END;
            """)
            tx.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_charge_adjustments_no_delete
                BEFORE DELETE ON charge_adjustments
                BEGIN
                    SELECT RAISE(ABORT, 'Charge adjustments ledger is append-only: deletions are prohibited.');
                END;
            """)
            tx.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_inventory_ledger_no_update
                BEFORE UPDATE ON inventory_ledger
                BEGIN
                    SELECT RAISE(ABORT, 'Inventory ledger is append-only: updates are prohibited.');
                END;
            """)
            tx.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_inventory_ledger_no_delete
                BEFORE DELETE ON inventory_ledger
                BEGIN
                    SELECT RAISE(ABORT, 'Inventory ledger is append-only: deletions are prohibited.');
                END;
            """)

        logger.info("Database successfully seeded with representative dataset: %s", counts)
        return counts
    finally:
        if owns_conn:
            conn.close()
