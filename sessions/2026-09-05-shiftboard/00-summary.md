# Summary · ShiftBoard: a visual production planner, seven roles, two correction cycles

**In one line.** The most demanding brief of the set: an interactive scheduler for a small workshop, with constraints, conflicts, undo, KPIs, scenarios and documentation. Seven roles, seven findings in two correction cycles, one word from the person after the brief. 48 messages, 0h50, 2.49M tokens, no flagship model.

**Language.** Everything is in English as written. Role outputs are the roles' own text.

## The brief

Jobs with quantities, due dates, priorities and ordered operations; machines with working hours and planned outages; an interactive schedule by machine; moving operations, changing priority and availability; an initial proposal; conflicts explained; automatic rescheduling visible and undoable; unplaceable work kept visible; utilisation, completion and lateness indicators whose numbers agree with the displayed schedule; a realistic demo with urgent order, extended outage and restore; readable labels and status not relying on colour alone; startup, walkthrough, assumptions, limitations, licence; local, no accounts, no external services.

## What happened

1. **Plan.** Five steps: Producer, then Reviewer, Security and Challenger independently, then Auditor. Approved as submitted.
2. **First version (S01, Producer, Gemini 3.7 Flash).** A full application: 30-minute slot planner over a five-day week, five machines, eight jobs, twenty-one operations, scheduling engine, drag-and-drop, conflicts, undo/redo, KPIs, two disruption scenarios, save, open, export, tests, README with licence.
3. **Review (S02, Reviewer, GLM 5.3 Flash).** Drove the application in a browser. Five findings, two of them on the brief's core: the button labelled "Undo Action" after a disruption did not undo, it rescheduled forward; an operation dragged to the Friday edge was silently shortened on screen while utilisation still counted its full length, so the numbers no longer agreed with the schedule. Three documentation mismatches. Corrected by the Producer, re-verified by the Reviewer in the browser.
4. **Security (S03, GPT-5.6).** Two findings: a saved plan file was rendered as HTML, so a shared plan could run script; the convenience server listened on every interface. Corrected with a closed schema, DOM-only rendering and loopback binding; verified with hostile fixtures and a socket check.
5. **Challenge (S04, Gemini 3.6 Flash).** Eight probes on precedence, capacity, shift spanning, setup time, outages, undo, unscheduled work and non-colour cues. No new finding; the earlier fixes hold.
6. **Audit (S05, Claude Haiku 4.5).** Thirteen brief requirements traced to the delivered files and to evidence; seven findings closed; session completed and sealed. Positive Memory validated.

## Why it is published

A brief this size is where a first version is most likely to look right and be wrong in the details a planner would hit on day one. Two of the seven findings were exactly that, and the record shows who found them, how they were fixed, and who checked.

## Files

`00-brief.md` · `01-plan.md` · `02-record.md` · `03-findings.md` · `04-closeout.md` · `05-positive-memory.md`
