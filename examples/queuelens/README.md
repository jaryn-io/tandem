> **About this folder.** This is the application produced by a Tandem session on 6 September 2026, published as delivered. The complete record of that session, every message, is at [`sessions/2026-09-06-queuelens`](../../sessions/2026-09-06-queuelens/00-summary.md). It is an internal test deliverable, not client work and not a product. Changes made for publication, and nothing else: four paths in this README made relative and one sentence about the verification tooling reworded; an MIT licence file added, since the session delivered none. Tests: all passing.
>
> Questions: tandem@jaryn.io

# QueueLens — Support Request Backlog Explorer

**QueueLens** is a self-contained, local-first web application designed for support leads, triage engineers, and customer operations teams to inspect, search, filter, deduplicate, and export local support ticket backlogs.

---

## Key Highlights

- **100% Local & Offline**: Operates completely in the user's browser without making any external network requests, loading remote CDNs, or executing third-party scripts.
- **Resilient Ingestion**: Parses both CSV (RFC 4180 compliant) and JSON ticket collections. It recovers partially incomplete records using safe defaults instead of silently discarding usable data, while clearly explaining any unparseable or excluded records.
- **Smart Duplicate Detection**: Identifies exact and high-similarity duplicate support tickets, groups them into clusters, designates the earliest ticket as primary, and highlights duplicates side-by-side.
- **Multi-faceted Search & Filters**: Search full text across subjects, descriptions, IDs, customers, and tags; filter by Priority (Urgent, High, Medium, Low), Status, Assignee (including Unassigned), or Duplicate status.
- **Flexible Export**: Export the current filtered backlog or checked records to RFC 4180 CSV or formatted JSON.
- **Dual Visualizations**: Switch seamlessly between a high-density interactive Table View and an interactive Kanban Board View.
- **Zero External Dependencies**: Implemented in clean semantic HTML5, modern vanilla CSS with light and dark mode, and robust ES6 JavaScript.

---

## Directory Structure

```
examples/queuelens/
├── index.html                           # Single-page web application entrypoint
├── styles.css                           # Polished styling with responsive layout & dark/light themes
├── app.js                               # Core application logic (parsers, filters, duplicate detector, UI)
├── sample-data.js                       # Pre-bundled sample datasets for instant 1-click exploration
├── server.py                            # Zero-dependency Python 3 HTTP server with security headers
├── run.sh                               # Executable launcher script
├── test_queuelens.py                    # Automated test suite (Python + Node.js unit tests)
├── README.md                            # Comprehensive documentation & user guide
└── sample-data/
    ├── support_tickets_standard.csv     # 20 realistic support tickets with duplicates & varied priorities
    ├── support_tickets_standard.json    # JSON equivalent of standard support backlog
    ├── support_tickets_with_issues.csv  # Edge-case CSV with malformed rows, missing fields & duplicates
    └── support_tickets_with_issues.json # Edge-case JSON with empty objects, missing fields & duplicates
```

---

## Quick Start & Startup Instructions

QueueLens offers two flexible, zero-installation ways to run:

### Option 1: Direct Browser Launch (No server required)
Because QueueLens has zero external dependencies, you can open `index.html` directly in any standard browser:
```bash
# Double click index.html or open via terminal:
xdg-open examples/queuelens/index.html
# or on macOS:
open examples/queuelens/index.html
```

### Option 2: Local HTTP Server (Recommended)
QueueLens includes a built-in Python 3 static HTTP server that binds strictly to `127.0.0.1` and enforces strict security headers (`Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`).

```bash
cd examples/queuelens

# Run via launcher script:
./run.sh

# Or run directly with Python 3:
python3 server.py --port 8080 --open
```

Once started, open `http://127.0.0.1:8080/index.html` in your web browser.

---

## Features & Operational Guide

### 1. Ingestion & Data Quality Diagnostics
QueueLens supports importing support backlogs in two formats:
- **CSV**: RFC 4180 compliant parser featuring auto-detection of delimiters (`,` `;` `\t` `|`), support for quoted fields containing commas or line breaks, and escaped quotes (`""`).
- **JSON**: Accepts root arrays (`[{...}]`) or wrapped objects (`{"tickets": [...]}`).

#### Explaining Malformed & Excluded Records
Unlike traditional tools that silently discard records or crash on unexpected lines, QueueLens implements a **preservation-first policy**:
- **Clean Records**: Fully valid records with standard fields.
- **Recovered Records (with Warnings)**:
  - *Missing ID*: Auto-assigns a synthetic ID (`AUTO-0001`) so the ticket can be tracked and explored.
  - *Missing Title*: Synthesizes a subject from the description preview rather than losing the record.
  - *Missing Priority*: Sets safe default (`Medium`) and logs a warning note.
  - *Missing Status*: Sets safe default (`Open`) and logs a warning note.
  - *Extra Columns*: Unnamed extra fields are preserved in `custom_fields`.
- **Excluded Records**:
  - Entirely blank lines or empty objects (`{}`).
  - Records lacking ID, Title, and Description simultaneously (insufficient content to identify).
  - Unparseable JSON syntax errors.

To inspect data quality:
1. Click the **"Data Quality Notes"** badge on the top right.
2. Review the **Excluded Records** tab (shows exact row numbers, reasons, and raw content snippets).
3. Review the **Recovered with Warnings** tab (shows applied defaults and affected fields).
4. Click **Download Diagnostics JSON** to save the diagnostic audit report.

### 2. Built-in Sample Datasets
The application comes pre-equipped with 1-click sample loaders in the top navigation bar:
- **📂 Load Clean Sample**: Loads 20 realistic IT, billing, and infrastructure tickets featuring urgent issues, unassigned tickets, and known duplicate reports.
- **⚡ Load Sample w/ Edge Cases**: Ingests a raw dataset containing empty lines, missing identifiers, unclosed quotes, and extra columns, immediately activating the Ingestion Explainer.

### 3. Duplicate Detection Engine
Support queues frequently suffer from duplicate tickets created across email, web portals, and chat:
- **Detection Algorithm**: Normalizes text (case, whitespace, punctuation, and common ticket prefixes like `Re:`, `Fwd:`, `Ticket:`, `Issue:`) and performs multi-pass clustering:
  1. Exact match on normalized subject/title (length > 5 characters).
  2. High Jaccard token similarity (>= 80%) on normalized subject/title tokens (> 2 characters).
  3. Exact description snippet match (> 20 characters) between tickets submitted by the same customer.
- **Cluster Hierarchy**: Related tickets are grouped using Disjoint Set Union (DSU) clustering. For each cluster, the earliest ticket by timestamp/order is designated as **Primary**, and subsequent tickets are tagged as **Duplicates**.
- **Duplicate Badge**: Click **"⚡ Duplicates"** in the top bar to open the **Duplicate Inspector Modal** for side-by-side comparisons of duplicate reports and 1-click filtering to any cluster.

### 4. Search & Multi-Faceted Filters
- **Full-Text Search**: Live search across ID, subject, description, requester/company, tags, and assignee.
- **Priority Filter**: Filter by `Urgent`, `High`, `Medium`, `Low`.
- **Status Filter**: Filter by `Open`, `In Progress`, `Pending Customer`, `Resolved`, `Closed`.
- **Assignee Filter**: Dynamically populated from dataset, including a dedicated filter for `⚠️ Unassigned` tickets.
- **Duplicate Filter**: Toggle between `All Tickets`, `⚡ Duplicates Only`, `★ Primary Records Only`, or `✓ Non-Duplicate Unique`.
- **Sorting**: Sort by Created Date (newest/oldest), Priority (severity hierarchy), Subject (A-Z), ID, or Status.
- **Active Filter Chips**: Click the `×` on any active filter chip to remove it individually, or click `↺ Reset` to clear all filters.

### 5. Exporting Views
QueueLens supports exporting backlogs directly from the browser:
- **Export Filtered View as CSV**: Exports all tickets currently visible under active filters.
- **Export Filtered View as JSON**: Exports formatted JSON of visible records.
- **Export Selected Records**: Use checkboxes in the table to select individual records, then export only the selected subset.
- *Preservation Guarantee*: Custom and non-standard fields are preserved in exports.

### 6. Dual View: Table & Kanban Board
- **Table View**: High-density view displaying ID, priority pill, status pill, title with description preview, tag chips, assignee, customer, duplicate group button, date, and detail action.
- **Kanban Board**: Groups tickets into workflow columns (`Open`, `In Progress`, `Pending Customer`, `Resolved / Closed`) with click-to-inspect modal details. (Note: Drag-and-drop card movement is not supported; file drag-and-drop is supported on the import drop zone.)

### 7. Ticket Detail Drawer / Modal
Click any ticket ID or title to open the modal:
- Full subject and formatted description.
- Customer, channel, source line number, and creation timestamp.
- **Duplicate Cluster Box**: Direct links to sibling tickets in the cluster.
- **Custom Fields Table**: Displays any extra metadata fields present in the source file.
- **Raw JSON View**: Complete record payload.

---

## Data Format & Field Specifications

QueueLens maps column names flexibly and case-insensitively:

| Standard Field | Accepted Aliases / Column Names | Fallback / Recovery Behavior |
| :--- | :--- | :--- |
| `id` | `ticket_id`, `request_id`, `issue_id`, `number`, `ref`, `key` | Synthesizes `AUTO-<row>` with warning |
| `title` | `subject`, `summary`, `headline`, `name`, `topic` | Synthesizes from description preview with warning |
| `description` | `details`, `body`, `message`, `notes`, `content`, `text` | Defaults to empty string |
| `priority` | `urgency`, `severity`, `level`, `prio` | Maps `P1/Urgent`, `P2/High`, etc.; defaults to `Medium` with warning |
| `status` | `state`, `stage`, `phase` | Maps `Open`, `In Progress`, `Pending`, etc.; defaults to `Open` with warning |
| `assignee` | `assigned_to`, `owner`, `agent`, `handler` | Preserved as empty (Unassigned) |
| `customer` | `requester`, `client`, `company`, `user`, `email` | Defaults to `Unknown Customer` |
| `created_at` | `date`, `timestamp`, `opened_at`, `created_date` | Preserves raw date string if unparseable, assigns current ISO timestamp |
| `tags` | `labels`, `categories`, `keywords` | Parsed from comma/semicolon-separated string or array |
| *Other columns* | Any other column header | **Preserved intact** in `custom_fields` |

---

## Documented Limitations

1. **Quadratic Duplicate Detection Scale**: Duplicate detection performs an O(n²) pairwise comparison across records using text normalization and token similarity. For typical queue sizes (up to 1,000–2,000 tickets), analysis finishes in under half a second (~385 ms for 1,000 tickets). For larger backlogs (e.g. 4,000+ tickets ~6s, 8,000+ tickets ~24s, 15,000+ tickets ~90s), the pairwise comparison runs synchronously in the main thread and causes noticeable browser delays before the view renders. While search, filtering, and table rendering remain fast once loaded, duplicate analysis is the primary computational bottleneck at scale.
2. **Session Persistence**: When opening via `file://` or static HTTP, uploaded files are processed in-memory. Refreshing the browser resets the view back to the default sample dataset unless exported. Theme preferences (`light`/`dark`) are stored persistently in `localStorage`.
3. **Fuzzy Matching Heuristic**: Duplicate detection uses token Jaccard similarity and string normalization. Highly technical error codes or acronyms might occasionally score near the threshold; users can inspect and filter duplicate groups in the dedicated Duplicate Inspector modal before taking triage action.
4. **Kanban Board Interaction**: The Kanban board provides column grouping and click-to-inspect modal detail views; card drag-and-drop between columns is not supported. Drag-and-drop interaction is dedicated to the file import drop zone.

---

## Security & Privacy Assurance

- **Zero External Telemetry**: No third-party scripts, tracking pixels, analytics, or external font stylesheets are loaded. 100% offline and local execution.
- **Robust DOM XSS Defense (SEC-001)**:
  - Dynamic interfaces are constructed using DOM APIs (`textContent`, `createElement`, `replaceChildren`) and strict HTML entity encoding (`escapeHtml`) across Table View, Kanban Board, Ticket Details Drawer, Ingestion Diagnostics, and Duplicate Cluster Inspector.
  - Zero inline event handlers (e.g. `onclick`) in markup; all events are bound via standard DOM event listeners.
  - Content Security Policy strictly excludes `'unsafe-inline'` from script execution: `default-src 'self' data:; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'none'; object-src 'none';`.
- **CSV Formula Injection Neutralization (SEC-002)**:
  - CSV export neutralizes formula-initiating characters (`=`, `+`, `-`, `@`, `\t`, `\r`) by prefixing cell values with a single quote (`'`), preventing formula execution / DDE attacks in Microsoft Excel, LibreOffice Calc, and Google Sheets.
  - Formatted JSON export preserves raw, unmodified values.
- **Strict Loopback-Only Isolation (SEC-003)**:
  - `server.py` enforces local-only loopback binding (`127.0.0.1`, `localhost`, `::1`). Any attempt to supply non-loopback hosts (such as `0.0.0.0` or LAN interfaces) is rejected with an explicit security violation error, ensuring zero accidental local-network exposure.

---

## Automated Verification & Test Suite

QueueLens includes a comprehensive verification test suite covering file integrity, network isolation, sample validity, and JS core engines:

```bash
# Run the automated test suite:
python3 test_queuelens.py
```

### Verification Coverage
1. **File Structure**: Verifies existence and accessibility of all components.
2. **Network Isolation**: Scans all HTML, CSS, and JS files to prove zero external URLs.
3. **Data Integrity**: Verifies standard and edge-case CSV/JSON sample files.
4. **Unit Engine Verification**: Executes `app.js` logic in Node.js, verifying:
   - RFC 4180 CSV parsing (quoted strings, embedded newlines, custom delimiters).
   - JSON parsing (nested and wrapped payloads).
   - Malformed row detection and explanation.
   - Synthetic ID assignment and non-lossy data preservation.
   - Exact and fuzzy duplicate cluster identification.
   - Filter, search, and sorting algorithms.
   - CSV and JSON export formatting.
5. **Headless Browser Scenario**: Verified in a headless browser across interactive controls, modals, filters, and theme toggling.
