> **About this folder.** This is the application produced by a Tandem session on 5 September 2026, published as delivered. The complete record of that session, every message, is at [`sessions/2026-09-05-shiftboard`](../../sessions/2026-09-05-shiftboard/00-summary.md). It is an internal test deliverable, not client work and not a product. Changes made for publication, and nothing else: four absolute paths in this README made relative; the copyright line of the licence set to Jaryn. Tests: 19/19.
>
> Questions: tandem@jaryn.io

# ShiftBoard — Visual Workshop Job Scheduling & Capacity Planning

A self-contained, publication-quality visual planning application designed for small manufacturers scheduling discrete customer jobs across shared machine centers.

---

## 1. Overview & Purpose

In a custom manufacturing environment (machine shops, fabrication facilities, tool & die makers), production planners must juggle competing customer jobs with diverse operations, variable batch sizes, and strict delivery deadlines across constrained, shared equipment.

**ShiftBoard** puts the interactive machine schedule at the visual center of planning operations. It empowers planners to:
- Visually organize, track, and dispatch jobs across machines across a 5-day operating week.
- Understand capacity constraints, machine utilization, and customer due date feasibility in real-time.
- Simulate and absorb disruptions—such as emergency rush orders or machine breakdowns—with transparent conflict explanations and one-click auto-scheduling with full undo/redo.
- Run 100% locally in any modern browser with **zero dependencies, no external network calls, and no accounts or logins**.

---

## 2. Quick Start (Local Execution)

ShiftBoard requires no build pipeline, npm packages, or database servers.

### Option A: Direct Browser File Opening (Zero Install)
Simply open `index.html` directly in your favorite web browser:
```bash
# On Linux / desktop:
xdg-open examples/shiftboard/index.html
# Or open the file URL:
file://examples/shiftboard/index.html
```

### Option B: Local Startup Script (Recommended)
Use the included startup script, which starts a loopback-bound server or launches your browser:
```bash
cd examples/shiftboard
./run.sh
```

### Option C: Local Static HTTP Server (Loopback Bound)
If you run Python's HTTP server directly, explicitly bind to the loopback address (`127.0.0.1`):
```bash
cd examples/shiftboard
python3 -m http.server 8080 --bind 127.0.0.1
```
Then visit: `http://127.0.0.1:8080` in your web browser.

### Network Exposure & Origin Isolation Model
ShiftBoard is engineered to operate strictly on the local host:
- **Loopback Binding (`127.0.0.1`)**: Binding explicitly to `127.0.0.1` (used by `run.sh` and documented above) ensures the server listens only on the local machine loopback interface. External network connections from adjacent machines on the local area network (LAN/Wi-Fi) are refused.
- **Origin Isolation**: Restricting access to loopback ensures that browser origin storage (`localStorage`) and active planning snapshots remain protected from other network devices.
- **Input Hardening & DOM XSS Protection**: All imported JSON plan files are strictly validated against a closed schema (rejecting unexpected fields, verifying type bounds, and checking color/identifier regexes). User-visible strings are rendered into the DOM using safe DOM APIs (`textContent`, `createElement`, `setAttribute`) rather than raw string interpolation into `innerHTML`, rendering script or event-handler payloads completely inert.

---

## 3. Fictional Workshop Walkthrough (AeroPrecision Machining Lab)

ShiftBoard comes preloaded with **AeroPrecision Machining Lab**, a realistic high-precision CNC manufacturing facility operating a 5-day standard work week (Monday through Friday, 08:00 to 17:00, 45 operating hours per workstation).

### Machine Workstations
1. **M1 — Haas UMC-750 (5-Axis CNC Mill)**: Precision simultaneous multi-axis machining center for aerospace brackets and complex compound angles. Hourly rate: $145/hr. *(Contains a pre-scheduled preventive maintenance outage on Tuesday afternoon, 13:00–17:00).*
2. **M2 — Haas VF-2SS (3-Axis High-Speed Vertical Mill)**: High-speed prismatic milling, drilling patterns, pocketing, and face milling. Hourly rate: $95/hr.
3. **M3 — Doosan Lynx 2100 (CNC Turning Center / Lathe)**: Precision lathe for cylindrical turning, boring, threading, and grooving. Hourly rate: $85/hr.
4. **M4 — Amada Ensis 3015 (3kW Fiber Laser Cutter)**: Flat sheet nesting, profile blanking, and prep cutting. Hourly rate: $120/hr.
5. **M5 — Zeiss DuraMax (Shop-Floor CMM & Metrology)**: Coordinate measurement inspection, surface roughness validation, and quality release certification. Hourly rate: $75/hr.

### Realistic Baseline Customer Jobs
The baseline demo features 8 distinct customer jobs interleaved across the 5 machines:
- **JOB-101 (Apex Aerospace — Titanium Impeller Bracket)**: Priority High (▲). 3 sequential operations (Blanking on M4 $\to$ 5-Axis Milling on M1 $\to$ CMM on M5).
- **JOB-102 (BioVasc Medical — 316L Vascular Valve Manifold)**: Priority Urgent (⚡). Multi-operation flagship part requiring 4 sequential stages (Cut on M4 $\to$ Lathe on M3 $\to$ Micro-Milling on M2 $\to$ Cleanroom Metrology on M5).
- **JOB-103 (Nordic Hydraulics — High-Pressure Pump Cylinder)**: Priority Normal (●). 2 operations (Turning on M3 $\to$ Bolt Pattern on M2).
- **JOB-104 (Turbine Dynamics — Inconel Nozzle Guide Ring)**: Priority Normal (●). Multi-shift 5-axis airfoil finishing spanning cleanly across Monday afternoon into Tuesday morning on M1, concluding before M1's Tuesday afternoon outage.
- **JOB-105 (Vanguard Defense — Armor Mounting Pivot Pin)**: Priority High (▲). Heavy turning on M3 $\to$ pin flats on M2.
- **JOB-106 (Kinetics Robotix — Articulated Joint Housing)**: Priority Normal (●). Pocket milling on M2 $\to$ bore finishing on M1 $\to$ bore micrometry on M5.
- **JOB-107 (Solaris Energy — Concentrator Pivot Bracket)**: Priority Low (▼). Laser cutting on M4 $\to$ batch drilling on M2.
- **JOB-108 (Precision Hydraulics — Valve Spool Assembly)**: Priority Normal (●). Fine grinding on M3 $\to$ spool leak testing on M5.

---

## 4. Scheduling Engine Rules & Assumptions

### 4.1 Working Calendar & Discrete Slot Model
- **Work Week**: Monday to Friday (5 working days).
- **Shift Hours**: 08:00 to 17:00 (9 working hours per day, 45 operating hours per week).
- **Time Slot Granularity**: 30-minute discrete slots (18 slots per day $\times$ 5 days = 90 total slots).
- **Slot Index Mapping**:
  - Monday 08:00 is `Slot 0`; Monday 16:30 is `Slot 17`.
  - Tuesday 08:00 is `Slot 18`; Tuesday 16:30 is `Slot 35`.
  - Friday 16:30 is `Slot 89` (finishes at 17:00).

### 4.2 Shift Boundaries & Non-Working Hours
Manufacturing operations that do not fit into the remainder of a daily shift pause when the shift closes at 17:00, and resume automatically at 08:00 the following morning.
- Non-working overnight hours (17:00 to 08:00 next day) and weekends are **not counted against operation duration**.
- In the visual board, operations spanning shift boundaries display an explicit continuation badge (e.g. `Cont. Tue`).

### 4.3 Setup Time vs. Run Time
- Every operation model separates **Setup Duration** from **Run Duration**:
  $$\text{Total Duration} = \text{Setup Hours} + \text{Run Hours}$$
- Setup occurs at the machine immediately prior to the production run.
- Setup occupies machine capacity and is visually shaded on the schedule block with a distinct cross-hatched pattern and wrench icon (`🔧`).

### 4.4 Hard Constraints & Conflict Detection
The engine evaluates 5 fundamental constraints on every schedule mutation:
1. **Machine Capacity (Single Spindle Limit)**: A machine can process at most one operation at any time slot. Overlaps are flagged as `CRITICAL: machine_overlap`.
2. **Machine Outages**: Operations cannot be scheduled during planned maintenance windows or breakdowns. Violations are flagged as `CRITICAL: outage_overlap`.
3. **Operation Precedence**: For any job, operation $\text{Op}_k$ cannot start until operation $\text{Op}_{k-1}$ has completed:
   $$\text{StartSlot}(\text{Op}_k) > \text{EndSlot}(\text{Op}_{k-1})$$
   Early starts are flagged as `CRITICAL: precedence_violation`.
4. **Schedule Horizon**: Operations must fall within the working week (`Slots 0..89`). Operations extending past the 5-day horizon are flagged as `CRITICAL: out_of_bounds`. Operations that cannot fit during auto-scheduling remain in the **Unscheduled Work Queue**.
5. **Customer Deadlines**: If the final operation of a job completes past the customer due date, the job is flagged with a `WARNING: tardiness` indicator.

### 4.5 Heuristic Forward-Scheduling Engine
When the user clicks **Propose Schedule (⚡)**:
1. Active jobs are sorted by:
   - Priority rank ($\text{Urgent} > \text{High} > \text{Normal} > \text{Low}$).
   - Earliest Due Date (EDD).
   - Deterministic Job ID tie-breaker (`a.id.localeCompare(b.id)`).
2. For each job, operations are sequenced sequentially:
   - The engine searches forward from $\max(0, \text{FinishSlot}(\text{Predecessor}) + 1)$ on the required machine.
   - It identifies the earliest contiguous window of free slots that does not conflict with outages or previously placed higher-priority operations.
3. If an operation cannot fit within the 45-hour week, it is safely marked as **Unscheduled** and kept in the queue.

---

## 5. Performance Indicators (KPI) Mathematical Definitions

ShiftBoard calculates and displays all metrics dynamically so they transparently agree with the visual board:

### 1. Machine Utilization (%)
$$\text{Utilization}_m = \left( \frac{\sum_{\text{op} \in \text{Scheduled}_m} \text{Duration}_{\text{op}}}{\text{AvailableHours}_m} \right) \times 100\%$$
- $\text{AvailableHours}_m = 45.0\text{h} - \text{OutageHours}_m$.
- Duration includes both setup and run hours.

### 2. Workshop Overall Utilization (%)
$$\text{Overall Utilization} = \left( \frac{\sum_m \text{ScheduledHours}_m}{\sum_m \text{AvailableHours}_m} \right) \times 100\%$$

### 3. On-Time Delivery Rate (%)
$$\text{On-Time Rate} = \left( \frac{\text{Count of Jobs with FinishSlot} \le \text{DueDateSlot}}{\text{Total Jobs in System}} \right) \times 100\%$$

### 4. Order Tardiness
$$\text{Tardiness}_j = \max\left(0, (\text{FinishSlot}_j - \text{DueDateSlot}_j) \times 0.5\text{ hours}\right)$$
$$\text{Total Tardiness} = \sum_{j} \text{Tardiness}_j$$

---

## 6. Reproducible Scenario Demonstrations

Use the dedicated action buttons in the top toolbar to explore shop floor dynamics:

### Scenario 1: Simulate Urgent Order (⚡)
- Injects `JOB-901: AeroTech Emergency (A320 Flap Actuator Mount)`, an Aircraft-On-Ground (AOG) rush order due Wednesday noon.
- It immediately demands turning capacity on M3, 5-axis milling on M1, and QA certification on M5.
- It creates instant capacity conflicts against lower-priority jobs on M3 and directly collides with M1's planned outage.
- **Action**: Click **Propose Schedule** to watch the priority-driven heuristic reschedule lower-priority jobs and re-establish a conflict-free production plan.
- **Undo**: Click **Undo (Ctrl+Z)** to step back and inspect the diff.

### Scenario 2: Simulate Extended Outage (🔧)
- Simulates an emergency spindle drive bearing failure on Haas UMC-750 (M1) extending maintenance through Wednesday afternoon.
- The Conflict Diagnosis drawer instantly flags the colliding operations and outlines them in high-contrast red warning borders.
- Planners can manually drag operations to other times or click **Auto-Resolve All**.

### Scenario 3: Restore Original Demo (⟲)
- Resets the entire workshop state back to the pristine baseline with a single click.

---

## 7. Accessibility & Non-Color Indicators

To guarantee readability under industrial lighting and for users with color vision differences:
- **Priority Icons**: ⚡ Urgent, ▲ High, ● Normal, ▼ Low.
- **Status Chips**: Clear text badges: `[ON TIME]`, `[LATE +3.5h]`, `[⚠ CONFLICT]`, `[UNSCHEDULED]`.
- **Setup Stripes**: Distinct 45-degree cross-hatching with wrench indicator.
- **Outage Blocks**: High-contrast diagonal red/black hazard stripes.
- **Sister Highlighting**: Hovering over any operation highlights all sister operations belonging to the same job across the board with a glowing border and dims unrelated jobs.

---

## 8. Save, Open & Export

- **Save Plan**: Downloads a standalone, readable JSON file (`shiftboard-plan-<timestamp>.json`) and stores an active snapshot in browser `localStorage`.
- **Open Plan**: Uploads and restores any previously exported ShiftBoard JSON plan.
- **Export Timetable**:
  - **Markdown**: Formatted dispatch tables for daily stand-up meetings and production logs.
  - **CSV**: Full tabular operation export for spreadsheets and ERP integration.

---

## 9. Verification & Automated Test Suite

ShiftBoard includes an automated Node.js test suite verifying all math, algorithms, scenarios, and constraints:
```bash
node test_shiftboard.js
```
The test suite covers:
- Time slot conversion and multi-day range formatting.
- Default workshop baseline integrity (0 conflicts, 100% on-time).
- Machine overlap, outage collision, precedence violation, and tardiness detection.
- Heuristic priority scheduling and conflict resolution.
- Urgent order and extended outage simulation transitions.
- Markdown and CSV report generation.

---

## 10. Limitations & Design Decisions

- **Deterministic 1-Week Horizon**: Current planning scope covers a 5-day manufacturing week (45 working hours per workstation). Jobs spanning multi-week horizons are placed in the unscheduled backlog once week capacity is exhausted.
- **Single-Machine Operator Assumption**: Machines are modeled as independent parallel workstations with dedicated or floating operators. Worker skill-matrix constraints can be layered as an extension.
- **Local Browser Sandbox**: No telemetry, analytics, or external API endpoints are invoked. All computations occur client-side in sub-millisecond execution times.

---

## 11. License

ShiftBoard is licensed under the **MIT License**. See [LICENSE](LICENSE) for full legal text.
Copyright &copy; 2026 ShiftBoard Open Project.
