# Summary · ShipGate: fifteen findings, one reviewer timeout, no one asked

**In one line.** A release-readiness tool for a small software team, from a six-sentence brief. Fifteen findings across four correction cycles, all closed; one Reviewer attempt hit the thirty-minute limit and Tandem reassigned the work by itself; the person typed one word after the brief. 69 messages, 1h50, 4.67M tokens, no flagship model.

**Language.** Everything is in English as written. Role outputs are the roles' own text.

## The brief

Create releases; add acceptance criteria and risks; record supporting evidence; track unresolved blockers; understand whether each release is ready. All records editable and persistent. A useful populated example and clear setup and run instructions. A real working tool, not a static mockup.

## What happened

1. **Plan.** Four steps: Producer, Reviewer, Security, Auditor. Approved as submitted (the approval was typed with a typo; it was still an approval).
2. **First version (S01, Producer, Gemini 3.7 Flash).** A working application in one turn: Node and Python servers, browser client with a standalone mode, readiness engine, three populated releases, tests, README.
3. **Review (S02, Reviewer, GLM 5.3 Flash).** Drove the tool in a browser and against both servers. Eight findings, five of them medium and on the brief's own words: a malformed import wiped the whole store and broke every later render; a release with no evidence was declared "ready, evidence verified"; an empty release scored 100 percent; the README's sample figure was wrong; running the tests deleted the user's data. Corrected by the Producer.
4. **Timeout.** The Reviewer's verification attempt ran past thirty minutes after a sequence of browser errors and was stopped. Tandem kept the corrected files, left the verification open, and moved Security forward in the meantime. No question reached the person.
5. **Security (S03, GPT-5.6).** Four findings: an API open to any client and any website, evidence links that would run script, unbounded request bodies, a health endpoint disclosing server paths. Corrected and re-verified.
6. **Verification (Reviewer, second attempt).** All eight review findings confirmed closed with fresh execution; one new cosmetic finding on imported records without timestamps, corrected and re-verified.
7. **Audit (S04, Claude Haiku 4.5).** Chain consistent, artefacts identical to the verified set, all thirteen product findings closed. Two documentation discrepancies raised, corrected, and verified by the Reviewer with 16 of 16 tests and a browser pass. Session completed and sealed. Positive Memory validated: three lessons.

## Why it is published

The first version worked. Five of the fifteen findings were on exactly what the brief asked for: knowing whether a release is ready, and not losing records. The record also shows what happens when a role's attempt fails mid-session: the work already accepted stays, the open step stays open, and the session continues without asking the person to intervene.

## Files

`00-brief.md` · `01-plan.md` · `02-record.md` · `03-findings.md` · `04-closeout.md` · `05-positive-memory.md`
