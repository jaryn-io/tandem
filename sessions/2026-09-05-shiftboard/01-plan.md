# Plan · ShiftBoard: a visual production planner, seven roles, two correction cycles

## Revision 1 (message 005) — approved

Plan revision 1

ShiftBoard is a substantial self-contained application, so the plan uses five meaningful steps with production first, targeted independent checks during integration, and a final claim-to-evidence audit. All work remains under `./`; no previous sessions or deliverables are consulted.

S01 — Producer: create the complete application in `./`. Implement the self-contained local startup path, realistic workshop data, job and operation models, calendar and outage handling, initial scheduling proposal, interactive machine-organized schedule, manual movement and priority/availability changes, conflict explanations, visible rescheduling with undo, unscheduled work, KPI calculations, save/reopen, readable export, restore-demo behavior, and the required walkthrough, assumptions, limitations, licensing, and startup documentation.

S02 — Reviewer: independently check the produced application against the full brief. Verify that the visible schedule, controls, data relationships, metrics, exports, documentation, and reproducible outage/urgent-order demonstration are complete and internally consistent. Report concrete deviations for Producer correction if needed.

S03 — Security: assess the local self-contained delivery for proportionate risks, including unsafe external calls, account or credential requirements, file access, import/export handling, browser/runtime assumptions, and unsafe interaction patterns. Report required mitigations without expanding the product scope.

S04 — Challenger: perform an independent usability and edge-case challenge focused on schedule comprehension, operation precedence, machine capacity, working-calendar boundaries, setup time, outage conflicts, urgent orders, undo/restore behavior, unscheduled work, and non-color status communication. Identify ambiguous or fragile behavior for targeted correction.

S05 — Auditor: perform the final independent audit after all accepted corrections. Trace each major claim in the brief to the delivered artifact and available evidence, confirm that the final application is self-contained and publication-quality within the declared scope, and record any remaining limitations or unresolved issues.

Dependencies are: S01 has none; S02 depends on S01; S03 depends on S01; S04 depends on S01; S05 depends on S02, S03, and S04. Reviewer, Security, and Challenger findings return to the Producer through the Orchestrator for controlled correction before S05. No human decision is required in the implementation plan itself; the existing Plan Strict approval remains the activation decision.

| Step | Owner | Depends on |
|---|---|---|
| S01 | producer | — |
| S02 | reviewer | S01 |
| S03 | security | S01 |
| S04 | challenger | S01 |
| S05 | auditor | S02, S03, S04 |
