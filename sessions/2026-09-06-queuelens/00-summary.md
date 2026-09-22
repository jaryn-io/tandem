# Summary · QueueLens: a first version that looked complete, and what independent checks found

**In one line.** A person writes a nine-requirement brief, approves the plan with one word, and comes back forty minutes later to a finished application: six problems found by two independent roles, corrected, re-verified, and traced back to the brief by a final audit. Six roles on four models, none of them a flagship: 44 messages, 0h40, 1.65M tokens.

**Language.** Everything is in English as written. Role outputs are the roles' own text.

## The brief

A self-contained application for exploring a local support-request backlog: import CSV or JSON; search and filter by priority, status and assignee; identify duplicates; export the selected view; explain malformed or excluded records without silently losing usable data; realistic sample data, a polished interface, clear startup instructions, documented limitations; everything processed locally.

## What happened

1. **Plan.** Four steps: Producer builds, Security inspects, Reviewer checks against the brief, Auditor traces every claim to evidence. Approved as submitted.
2. **First version (S01, Producer, Gemini 3.7 Flash).** A complete application in one turn: parser, duplicate detection, filters, export, two views, sample data, tests, README. Its own browser scenario passed.
3. **Security (S02, GPT-5.6).** Three findings from source inspection: imported values reached the page as HTML, so a crafted ticket could run script; exported CSV cells starting with `=`, `+`, `-` or `@` were not neutralised; the server accepted any bind address although the documentation promised loopback only. The last one touches the brief directly: "process everything locally". Corrected by the Producer, re-verified by Security in a real browser.
4. **Review (S03, Reviewer, GLM 5.3 Flash).** The functional surface is complete, and every earlier fix holds. Three new findings, all against the brief's own words. The README promised a drag-and-drop Kanban that did not exist and "15,000 tickets at 60 FPS" while the duplicate detector took 24 seconds at 8,000. A warning told the user an unrecognised value was "preserved in custom_fields" when it was in fact dropped: the one requirement the brief calls out, explaining malformed records without silent loss, was met in appearance and not in fact. The shipped test suite could not fail. Corrected by the Producer; the Reviewer re-ran the suite and drove the fixed page in a browser.
5. **Audit (S04, Claude Haiku 4.5).** Every brief obligation mapped to a file and a piece of evidence; all six findings traced from source to correction to verification; no contradictions. Session completed and sealed. Positive Memory validated: four lessons for the next session.

## Why it is published

The first version would have passed a quick look. What the record shows is the distance between "looks done" and "verified against the brief", and that the distance was covered without the person doing anything after approving the plan.

## Files

`00-brief.md` · `01-plan.md` · `02-record.md` · `03-findings.md` · `04-closeout.md` · `05-positive-memory.md`
