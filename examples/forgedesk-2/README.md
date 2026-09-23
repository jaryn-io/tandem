> **About this repository.** This is the application produced by a Tandem session on 30 August 2026, published exactly as delivered. The complete record of that session, every message, is at [`sessions/2026-08-30-forgedesk-2`](../../sessions/2026-08-30-forgedesk-2/00-summary.md). It is an internal test deliverable, not client work and not a product; the brief was written to be demanding on purpose. Nothing below this note has been changed.
>
> The test suite passed 174/174 at closeout. Three lifecycle tests use fixed dates in August 2026 and fail once those dates are more than thirty days in the past, because the application rejects manual check-ins older than thirty days. That is the application's own rule at work, not a regression; the tests were left as written.
>
> Questions: tandem@jaryn.io

# ForgeDesk — Makerspace Management Web Application

ForgeDesk is a complete, production-ready, locally runnable web application for managing a makerspace with members, machines, qualifications, reservations, waiting lists, consumable inventory ledger, maintenance, usage charges, and append-only audit logging.

Built with pure Python 3 standard library and SQLite (WAL mode). Zero external framework dependencies, 100% offline and self-contained on Linux.

---

## Quickstart

Run ForgeDesk with a single command:

```bash
./run.sh
```

Or directly via Python 3:

```bash
python3 app.py
```

The application starts immediately on **`http://127.0.0.1:8080/`**. On first launch, database schema migrations and initial representative seed data are applied automatically.

---

## Demonstration Accounts

ForgeDesk comes pre-seeded with representative user accounts for all four operational roles:

| Username | Password | Role | Description & Primary Capabilities |
|---|---|---|---|
| `admin` | `admin123` | **Administrator** | Full system authority, role management, financial adjustments, CSV import, backup & restore |
| `operator` | `operator123` | **Operator** | Daily operations, machine state, maintenance, inventory movements, reservations, CSV exports |
| `member_alice` | `member123` | **Member** | Active member with 3D Printing & Laser Cutter qualifications; self-service reservations |
| `member_bob` | `member123` | **Member** | Active member with 3D Printing qualification & expired CNC qualification |
| `member_clara` | `member123` | **Member** | Active member on waiting list |
| `viewer` | `viewer123` | **Viewer** | Read-only observer with access to machine schedules and inventory stock status |

---

## Four-Role Authorization Model & Granular RBAC

Authorization is strictly enforced on the server for **every page view, form submission, and REST API request**.

| Role | Operational Scope & Server-Side Security Boundaries |
|---|---|
| **Administrator** (`admin`) | **Full authority**: User role assignments, system data imports, financial adjustments/refunds, database backups and restores, and all workshop operations. Cannot demote or disable the last remaining administrator. |
| **Operator** (`operator`) | **Workshop manager**: Manages machines, maintenance jobs, incident reports, all reservations, waiting lists, check-ins/check-outs, inventory movements, member certifications, and data exports. **Cannot modify user roles, alter system security settings, or perform database restores**. |
| **Member** (`member`) | **Self-service user**: Manages personal profile and own reservations, self check-in/checkout, views own charges and payment ledger, views catalog availability. **Cannot modify other members' records, alter machines, or access administrative tools**. |
| **Viewer** (`viewer`) | **Read-only observer**: Read-only access to calendar, machine catalog, and inventory stock. **Cannot create bookings, modify records, or check in**. |

---

## Key Modules & Capabilities

### 1. Data Management, CSV Bulk Import & Rollback Engine (`/system/data`, `/system/import`)

- **Interactive CSV Import with Row-by-Row Validation**:
  - Supports bulk creation of **Members** (with qualifications), **Machines** (with categories and rates), and **Inventory Items** (with initial ledger receipts).
  - Pre-import preview displays row index, parsed values, validation badges, and specific line diagnostics.
- **Atomic Rollback Guarantee**:
  - The import executes inside a single transactional block (`with transaction(conn):`).
  - If **any single row fails** validation or database constraints, the entire batch is rolled back completely. Zero partial data is ever written to the database.
- **Downloadable Sample CSV Templates**:
  - Download template files via `/system/import/sample?type={members|machines|inventory}` or web UI buttons.

#### CSV Format Specifications

**Members CSV (`sample_members.csv`)**:
```csv
full_name,email,phone,membership_status,membership_expiry,notes,qualifications
"Marco Verdi","marco.verdi@example.com","+39 340 1122334","active","2026-12-31","New member","3d_printers:2026-12-31;laser_cutters:2026-10-31"
"Giulia Romano","giulia.romano@example.com","+39 347 5566778","active","2027-06-30","Electronics specialist","electronics;cnc_mills"
```

**Machines CSV (`sample_machines.csv`)**:
```csv
code,name,category_code,capacity,state,operating_hours_start,operating_hours_end,hourly_rate,minimum_charge,peak_hourly_rate,peak_hours_start,peak_hours_end,location,description
"ULTI-S5-01","Ultimaker S5 Pro","3d_printers",1,"available","08:00","22:00","4.50","2.00","6.00","17:00","21:00","Lab 3D - Bench 2","Dual extrusion FDM 3D printer"
"TROTEC-SP500","Trotec Speedy 500 Laser","laser_cutters",1,"available","09:00","21:00","25.00","10.00","30.00","17:00","21:00","Laser Room 1","120W CO2 laser cutter"
```

**Inventory CSV (`sample_inventory.csv`)**:
```csv
sku,name,category,unit,unit_cost,minimum_stock,initial_quantity,location,description
"MAT-PLA-BLK-1KG","PLA Filament 1.75mm Black 1kg","filament","spool","22.00",3,10,"Shelf B2","Premium black PLA spool"
"MAT-ACRYL-3MM-CLR","Cast Acrylic Sheet 3mm Clear 600x400","sheets","pcs","14.50",5,20,"Rack Laser-1","Optically clear cast acrylic"
```

---

### 2. Domain Data Exports (RFC 4180 CSV & Structured JSON)

Direct download links and API endpoints for all core entities:

- **Members & Qualifications**: `GET /members/export?format=csv` or `?format=json`
- **Machines & Configurations**: `GET /machines/export?format=csv` or `?format=json`
- **Reservations & Usage**: `GET /reservations/export?format=csv` or `?format=json`
- **Inventory Items & Stock**: `GET /inventory/export?format=csv` or `?format=json`
- **Immutable Inventory Ledger**: `GET /inventory/ledger/export?format=csv`
- **Usage Charges & Adjustments**: `GET /charges/export?format=csv` or `?format=json`
- **Audit Activity Trail**: `GET /audit/export?format=csv` or `?format=json`
- **Full Database JSON Dump**: `GET /system/export/all` (sanitizes password hashes and session tokens)

---

### 3. Online Database Backup & Disaster Recovery (`/system/backup`)

- **Live Non-Blocking SQLite Online Backup**:
  - Uses SQLite's online backup API to copy database pages consistently with zero server downtime, no table locks, and no corrupted snapshots.
  - Generates dated `.db` backup archives in `data/backups/`.
- **Verified Atomic Database Restoration**:
  - Validates SQLite header signature and required table schema before proceeding.
  - Automatically takes a pre-restore safety snapshot of the active database before replacing it.
  - Automatically applies any pending migrations to guarantee schema compatibility.
  - Restricted strictly to Administrators (`PERM_SYSTEM_RESTORE`).

---

### 4. Searchable Append-Only Audit Trail (`/audit`)

- **Append-Only Immutability**: Hard trigger-level protection (`trg_audit_log_no_update`, `trg_audit_log_no_delete`).
- **Comprehensive Search & Filters**: Filter by action category (`auth.*`, `member.*`, `machine.*`, `reservation.*`, `inventory.*`, `charge.*`, `system.*`), object type, actor, date range (Europe/Rome), or free-text keywords.
- **Visual Before/After Diffs**: Side-by-side JSON comparison with color-coded additions, deletions, and modifications.

---

## CLI Reference & Database Management

`app.py` provides a full CLI for server binding, database migrations, seeding, backups, and restores:

```bash
# Start server on custom host and port
python3 app.py --host 0.0.0.0 --port 9000

# Apply pending schema migrations
python3 app.py --init-db

# Force seed database with demo dataset
python3 app.py --seed

# Reset database (drops all tables and reapplies schema)
python3 app.py --reset-db

# Perform live online SQLite database backup to data/backups/
python3 app.py --backup

# Perform online database backup to custom file path
python3 app.py --backup /var/backups/forgedesk_custom.db

# Restore database from validated backup file
python3 app.py --restore /var/backups/forgedesk_custom.db
```

---

## Running the Complete Test Suite

Execute the full test suite (170+ unit and integration tests covering RBAC, security, reservations lifecycle, billing, waiting lists, audit trail, CSV import/export, and backup/restore):

```bash
python3 -m unittest discover -s tests -v
```

---

## Configuration & Environment Variables

| Variable | Default | Description |
|---|---|---|
| `FORGEDESK_HOST` | `127.0.0.1` | Network host interface to bind |
| `FORGEDESK_PORT` | `8080` | TCP port number |
| `FORGEDESK_DATA_DIR` | `./data` | Base directory for database and file storage |
| `FORGEDESK_DB_PATH` | `./data/forgedesk.db` | Explicit SQLite database file path |
| `FORGEDESK_BACKUP_DIR` | `./data/backups` | Directory for automated and manual backup files |
| `FORGEDESK_UPLOAD_DIR` | `./data/uploads` | Directory for incident attachments |
| `FORGEDESK_SECRET_KEY` | `(default secret)` | Cryptographic secret for CSRF and tokens |
| `FORGEDESK_MAX_REQUEST_BODY_SIZE` | `15728640` (15 MB) | Maximum incoming HTTP request body size |
| `FORGEDESK_COOKIE_SECURE` | `0` (False) | Auto-enforced True when bound to non-loopback hosts |
