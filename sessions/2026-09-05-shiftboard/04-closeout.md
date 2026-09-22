# Closeout · ShiftBoard: a visual production planner, seven roles, two correction cycles

# Final report

## Objective

Build ShiftBoard, a self-contained visual planning application for a small manufacturer
scheduling jobs across shared machines.

Create the project in a new, uniquely named directory under the project folder This session must
be isolated: do not consult or reuse previous sessions, reports, code, or deliverables.

User and purpose

A production planner needs to arrange a finite set of customer jobs over a working week,
understand capacity constraints, and respond to a machine outage or an urgent order.

Core experience

- Represent jobs with quantities, due dates, priorities, and an ordered sequence of
manufacturing operations.

- Represent machines with working hours and planned unavailability.
- Show operations on an interactive schedule organized by machine, with a clear connection
between each operation and its customer job.

- Let the planner move operations, change job priority, adjust availability, and inspect the
consequences.

- Provide a useful initial scheduling proposal and allow manual changes.
- Support saving and reopening a plan, plus a readable schedule export.

Scheduling behavior

Respect operation order, machine capacity, and working calendars. Make the treatment of
setup time and operations spanning shift boundaries explicit.

When a change creates a conflict, explain which jobs or operations are affected. Any
automatic rescheduling should make its effects visible and allow the user to undo it. Work
that cannot be placed must remain visible as unscheduled.

Show understandable indicators for machine utilization, planned job completion, and late
orders. Define their calculation so the numbers agree with the displayed schedule.

Reproducible demonstration

Include a realistic fictional workshop with several machines, multiple jobs competing for
capacity, a multi-operation job, and a planned outage. Let users explore how an urgent order
or extended outage changes the plan, then restore the original example.

Publication quality

Make the schedule the visual center of the application. Use readable labels, clear time
scales, and status indicators that remain understandable without relying only on color.

Include straightforward startup instructions, a short walkthrough of the example, scheduling
assumptions, limitations, and licensing information. The application should work locally
without accounts or external services.

## Outcome

Status: Completed.

### Orchestrator conclusion

# Orchestrator Judgment: Final Audit Accepted — Session Complete

The Auditor's comprehensive claim-to-evidence audit is accepted. The report demonstrates methodical tracing of all 13 major brief requirements through delivered artifacts, verified corrections, independent testing results, and specialist assessments. Every material claim in the original brief has supporting evidence; no unresolved defects or missing proofs remain.

## Evidence summary

**Brief coverage verified:**
- Self-contained local delivery confirmed: zero external network calls, no accounts required, works locally from HTML/JS/CSS.
- Job and operation models implemented with correct data relationships, sequencing constraints, and priority handling.
- Conflict detection verified across five categories (machine overlap, outage collision, precedence violation, out-of-bounds, tardiness).
- Interactive schedule rendering proven in live Chromium testing with machine rows, operation blocks, setup indication, outage overlays, and job linking.
- Initial scheduling proposal implemented with documented heuristic (Priority Rank → EDD → Job ID).
- Undo/redo lifecycle verified with real history (40-level stack, state snapshots, restore function).
- Manual drag-and-drop and priority/availability adjustment controls confirmed working.
- Unscheduled work queue implementation verified (operations marked -1 remain visible).
- Save/open/export with schema validation, Markdown dispatch tables, and CSV operation lists confirmed.
- KPI calculations (utilization, on-time rate, tardiness) matched to displayed schedule with published formulas.
- Reproducible demonstration verified: AeroPrecision baseline (5 machines, 8 jobs, 1 outage), urgent-order scenario, extended-outage scenario, restore to baseline all execute correctly.
- Publication quality confirmed: startup (3 paths documented), walkthrough, assumptions, limitations (1-week horizon, single-spindle, sandbox), MIT licensing.
- Non-color accessibility verified: priority glyphs, status chips, cross-hatched setup, hazard-striped outages, continuation badges, sister highlighting.

**Finding disposition:**
All seven prior findings (S02-F1…F5, S03-F1…F2) are independently verified as closed:
- **S02-F1 & F2 (interaction defects):** Corrected and verified in G-1/G-2 with live button/conflict test; Challenger independently confirmed correct behavior under real operation.
- **S02-F3–F5 (documentation consistency):** Corrected and verified with fresh artifact inspection; README and code now aligned.
- **S03-F1 & F2 (security/binding):** Corrected with strict schema validation and malicious JSON testing in G-3/G-3-verify; loopback binding confirmed.

**Edge-case and state safety:**
Challenger executed 19 automated tests (all pass), 8 critical boundary probes (all pass), confirmed no regressions and robust conflict handling under realistic and pathological scenarios.

**No unresolved matters.** Zero open findings. All design limitations are transparent in documentation. The application is complete within declared scope: no undeclared features, no scope creep, no internal inconsistencies.

## Closeout authority

The approved plan has been executed in full. All specialist findings have been resolved through bounded correction and independent verification. The final audit confirms that the delivered artifact is publication-ready, self-contained, and wholly compliant with the original brief.

**Next action:** Route to terminal completion. The session has achieved its objective.

## Accepted deliverables

- `./LICENSE`
- `./README.md`
- `./app.js`
- `./engine.js`
- `./fixtures/invalid_schema_bad_types.json`
- `./fixtures/invalid_schema_unknown_fields.json`
- `./fixtures/malicious_xss_plan.json`
- `./index.html`
- `./run.sh`
- `./style.css`
- `./test_shiftboard.js`

## Plan

See `01-plan.md`.

## Findings

- S02-F1 · Severity: medium
  Status: Verified and closed.
- S02-F2 · Severity: medium
  Status: Verified and closed.
- S02-F3 · Severity: low
  Status: Verified and closed.
- S02-F4 · Severity: low
  Status: Verified and closed.
- S02-F5 · Severity: low
  Status: Verified and closed.
- S03-F1 · Severity: medium
  Status: Verified and closed.
- S03-F2 · Severity: medium
  Status: Verified and closed.

## Positive Memory

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
