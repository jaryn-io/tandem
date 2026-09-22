# Plan · ShipGate: fifteen findings, one reviewer timeout, no one asked

## Revision 1 (message 005) — approved

Plan revision 1

S01 — Producer: build the complete application in `./`. Implement the release, acceptance-criteria, risk, evidence, blocker, readiness-status, editing, and persistence workflows; include populated example data and clear setup/run instructions. The result must be a working tool rather than a static mockup.

S02 — Reviewer: independently examine the produced application against the brief and verify end-to-end usability, editability, persistence, readiness calculations, populated examples, setup instructions, and strict conformity. This step depends on S01.

S03 — Security: assess the implemented application and its configuration for applicable risks, including persistence boundaries, input handling, unsafe rendering or links, data exposure, and setup/runtime concerns. Report findings and required corrections. This step depends on S01 and may run alongside S02.

S04 — Auditor: perform the final independent audit of the complete claim-to-evidence chain, incorporating the producer result, reviewer report, and security report. Confirm that the final deliverable satisfies the brief or identify unresolved gaps. This is the final canonical step and depends on S02 and S03.

The plan deliberately keeps production as the only modifying work, places conformity and security checks after an executable result exists, and reserves the final decision-quality audit for the end. Any material rework identified by review or security returns to the Orchestrator for disposition before the audit is accepted.

| Step | Owner | Depends on |
|---|---|---|
| S01 | producer | — |
| S02 | reviewer | S01 |
| S03 | security | S01 |
| S04 | auditor | S02, S03 |

Correction steps added during the session are numbered G-1, G-2… in order of creation; `-verify` marks the independent re-check of a correction.
