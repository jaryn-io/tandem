# Positive Memory · ShiftBoard: a visual production planner, seven roles, two correction cycles

# Positive Memory Snippet: Local-First Scheduling Tools, Ingestion Hardening, and Discrete Horizon Integrity

## 1. Local-First Ingestion Defense: Closed Schemas and Safe DOM Pipelines

In client-side applications that run offline without server backends, user accounts, or external APIs, developers often perceive the system as having no significant attack surface. However, local file import controls (e.g., loading saved plans or backups via `FileReader`) constitute an external, untrusted input boundary.

### What to Avoid:
- Interpolating properties from imported JSON records (such as job names, customer identifiers, machine descriptions, outage reasons, operation codes, or conflict messages) directly into `innerHTML`. A crafted plan file shared among users can achieve Stored DOM XSS in the local origin, granting execution capability within the browser session, exposing `localStorage`, and permitting state tampering.
- Relying solely on loose object-key checks (e.g., `if (data.jobs)`) that accept unknown properties or arbitrary types without schema constraints.

### What to Apply:
- **Strict Closed-Schema Validation**: Implement a schema validator (`validatePlanSchema`) that rejects unknown top-level and entity properties through explicit allowlists. Validate primitive types, regex patterns (for alphanumeric IDs and hex color codes), discrete slot ranges, and referential integrity (ensuring machine IDs, job IDs, and dependency references map to declared entities).
- **Native DOM Construction**: Build dynamic interface elements (Gantt timeline bars, card headers, drawer lists, dropdowns, and modal tables) exclusively using native DOM APIs (`document.createElement`, `textContent`, `setAttribute`). Ensure validation error reporting itself uses `textContent` so invalid payloads cannot trigger script execution during error display.
- **Adversarial Fixtures in the Verification Suite**: Include explicit test fixtures for malicious inputs (e.g., valid schema structures containing `<script>`, `<img>` error handlers, and SVG attack vectors, alongside fixtures with unknown fields or corrupted types). Verify both programmatic rejection and live browser inertness (e.g., verifying `window.__xss_detected === false` in end-to-end browser tests).

---

## 2. Local Convenience Server Isolation: Explicit Loopback Binding

Standalone applications often include convenience scripts (such as `run.sh`) or startup documentation leveraging Python's built-in HTTP server (`python3 -m http.server $PORT`).

### What to Avoid:
- Invoking `python3 -m http.server` without an explicit bind address. By default, Python binds to `0.0.0.0` (`INADDR_ANY`), exposing the local application and its origin-scoped `localStorage` to all network interfaces, including local LANs and shared Wi-Fi networks.

### What to Apply:
- Explicitly pass `--bind 127.0.0.1` in all launch scripts and documentation to restrict the HTTP socket exclusively to the local loopback interface.
- Clearly document the single-tenant local workstation security model and local origin boundaries in user-facing documentation.
- Include automated checks (such as CLI flag assertions or socket inspection) to guarantee the server rejects non-loopback connections.

---

## 3. Discrete Horizon Integrity: Conflict Reporting vs. Silent Clamping

In timeline- and slot-based planning tools (e.g., a 5-day manufacturing week divided into discrete 30-minute intervals), dragging or scheduling operations near the schedule boundary presents boundary-condition risks.

### What to Avoid:
- Silently clamping `endSlot = Math.min(startSlot + durationSlots - 1, MAX_SLOT)`. Truncating the operation's span to fit within the visible grid distorts the visual block (e.g., rendering a 2.0-hour block for a 2.5-hour task) while metrics like machine utilization continue counting the full 2.5 hours. Furthermore, clamping hides the boundary overflow from conflict detection engines.

### What to Apply:
- **Preserve Declared Duration**: Keep the operation's true duration intact. Evaluate whether `startSlot + durationSlots - 1` exceeds the grid boundary (`TOTAL_SLOTS - 1`).
- **Explicit Conflict Flagging**: When an operation exceeds the horizon, flag an explicit `out_of_bounds` conflict. Display actionable remediation guidance (e.g., suggest moving to an earlier slot or returning the task to the unscheduled queue) so that visual blocks, conflict lists, and utilization metrics remain strictly consistent.

---

## 4. Semantic Clarity in Disruption Notification Banners

Interactive simulations (such as injecting urgent rush orders or unexpected machine breakdowns) alter schedule state and typically present in-app notification toasts offering one-click resolution.

### What to Avoid:
- Binding forward heuristic auto-scheduling (e.g., auto-resolving conflicts by rearranging lower-priority jobs) to a notification button labeled "Undo" or "Undo Action". This conflates automated forward mutation with historical rollback, misleading the user into believing the disruption was canceled rather than the schedule reshuffled.

### What to Apply:
- Clearly differentiate forward resolution from backward rollback. Label heuristic actions descriptively (e.g., "Auto-Resolve" or "Optimize Schedule").
- Reserve "Undo" exclusively for state restoration via the application's history stack (`undoStack` / `handleUndo()`), ensuring users retain unambiguous control over disruptions and automatic modifications.

---

## 5. When to Apply & Context

Apply these patterns to any client-side planning, scheduling, resource-allocation, or dispatch tool featuring discrete time models, file-based state import/export, and local execution. They safeguard against client-side injection, eliminate unintended local network exposure, preserve mathematical and visual consistency across planning boundaries, and ensure intuitive user control during disruption management.
