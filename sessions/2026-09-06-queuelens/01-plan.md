# Plan · QueueLens: a first version that looked complete, and what independent checks found

## Revision 1 (message 005) — approved

Plan revision 1

QueueLens will be implemented as a self-contained local application inside `./`. The plan preserves usable records during import, makes malformed/excluded records visible, and keeps all processing local.

S01 — Producer: Build the complete application, including the polished interface, local CSV/JSON import, search and filters for priority/status/assignee, duplicate identification, selected-view export, malformed-record reporting, realistic sample data, startup instructions, and documented limitations. The implementation should remain self-contained and avoid dependencies on previous session artifacts.

S02 — Security: Inspect the implementation for local-only processing, unsafe file handling, export/import risks, data leakage, dependency or startup hazards, and any path or browser-related exposure. Report actionable issues for correction before conformity review.

S03 — Reviewer: Review the completed application against the brief and S01/S02 results. Verify functional coverage, user-visible handling of malformed and excluded records, data preservation, export behavior, documentation, and polish. Identify any required rework.

S04 — Auditor: Perform the final independent claim-to-evidence audit across the application, security findings, review results, and documented limitations. Confirm that the deliverable is complete and that each promised capability has credible evidence.

Dependencies: S02 depends on S01; S03 depends on S01 and S02; S04 depends on S01, S02, and S03.

| Step | Owner | Depends on |
|---|---|---|
| S01 | producer | — |
| S02 | security | S01 |
| S03 | reviewer | S01, S02 |
| S04 | auditor | S01, S02, S03 |

Correction steps added during the session are numbered G-1, G-2… in order of creation; `-verify` marks the independent re-check of a correction.
