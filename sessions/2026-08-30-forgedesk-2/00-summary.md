# Summary · ForgeDesk, part 2: inherit a partial product, verify it, complete it

**In one line.** A session that inherits the partial ForgeDesk from part 1, refuses to trust its documentation, verifies the code, finds and fixes two high-severity authorisation holes, and closes with a final audit that traces every claim to the code: 123 messages, 1h31, 27,800 lines of Python, 174 tests in 13 files, no third-party packages.

**Language.** The brief and the person's messages are in Italian, translated in place with the original kept. Almost every role wrote in Italian; the full text is kept as written and collapsed in `02-record.md`. Finding identifiers, code and test names are as in the code.

## What happened

1. **Brief.** Take the partially built ForgeDesk and make it real. The README is not proof of completeness; the code is the baseline. Keep what works, migrate data deterministically, propose a complete plan first.
2. **Plan, twice.** The first plan was logically sound but ignored the current state, and its Producer steps were too large. The person rejected it and asked for smaller steps with intermediate reviews and repeated use of the Challenger. The second plan, 22 steps plus an addendum, was approved.
3. **The chain that matters (S04 → S22).** The Producer (Gemini 3.7 Flash) rebuilt authentication and authorisation. Security (GPT-5.6) found two HIGH findings: a Member could read another member's reservations, contacts and qualifications by changing an id, and the same on the waiting list. The Producer enforced server-side ownership; the Reviewer (GLM 4.7) re-ran the suite from scratch, 18 of 18. The Challenger (Gemini 3.7 Flash) raised medium and low findings on state consistency at three points; they were left open and declared. Security later found three more issues (client-controllable check-in timestamps, unrestricted rate overrides, no database-level immutability of charges); the Producer corrected them. The Auditor (Claude Opus 4.6, Thinking) ran the final gate and traced every claim to the code.
4. **Close.** Session completed and sealed. 95 accepted files including database backups. Findings at closeout: two HIGH accepted after remediation, three security fixes corrected but not re-verified, four medium and five low findings open and declared. Positive Memory validated: three lessons for the next session.

## Why it is published

It is the clearest example of the point: the role that produces never certifies its own work, a different model family finds the error, the fix is verified by a third, and what stays open is written down rather than forgotten.

## The application

The ForgeDesk application accepted at closeout is published as delivered, with its 174 tests, at [`examples/forgedesk-2`](../../examples/forgedesk-2). Run it with one command and check the record against the code.

## Files

`00-brief.md` · `01-plan.md` · `02-record.md` · `03-findings.md` · `04-closeout.md` · `05-positive-memory.md`
