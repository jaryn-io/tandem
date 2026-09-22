# Tandem sessions — real records

This repository holds complete records of real Tandem sessions, in the same shape you get on your own machine: the brief, the plan and its approval, every message between the roles, the findings, what stayed open, and the closeout. They are internal test sessions run while building the product, not client work. They are published as proof you can read, not as claims.

## What a session is

A person writes a brief. A planning role turns it into steps with owners and dependencies; nothing runs until the person approves the plan. Each step runs as a bounded call. The role that produces is never the role that judges: review, security and audit roles check the work independently, and can be bound to a different model family from the one that produced it. The decisions that are actually the person's come back to the person. The session ends with a readable record.

## How to read a record

| File | What it holds |
|---|---|
| `00-summary.md` | The story in one page, in English |
| `00-brief.md` | The team (role, model, turns) and the brief as written |
| `01-plan.md` | Every plan revision: rejected ones with the person's reasons, the approved one in full |
| `02-record.md` | Every message, in order, with time and author. Role outputs are the roles' own text. Tandem's own messages to the person are summarised in one line |
| `03-findings.md` | Every finding raised, by whom, and its status at closeout |
| `04-closeout.md` | The final report: outcome, accepted deliverables, findings, and the lessons proposed |
| `05-positive-memory.md` | The validated lessons, when the session produced them |

## Redaction rule

What you see is the work and the outcome. Internal paths, identifiers, hashes and transport are removed, and so is every sentence in which a role describes the system's own mechanics rather than the work; database files are listed by count, not by name. Nothing else is rewritten. Correction steps added during a session are numbered G-1, G-2… in order of creation. Where a message was written in Italian, it is kept in Italian and collapsed; briefs and human messages in Italian carry a translation with the original underneath. Model names stay: they are part of the proof.

## Sessions

| Record | Date | In one line | Language |
|---|---|---|---|
| [`2026-08-30-forgedesk-2`](2026-08-30-forgedesk-2/00-summary.md) | 30 Aug 2026 | ForgeDesk inherited, verified and completed: two HIGH authorisation findings found by a second model family, fixed, re-verified by a third; final audit passed; 27,800 lines, 174 tests | IT, translated where human |
| [`2026-09-05-shiftboard`](2026-09-05-shiftboard/00-summary.md) | 5 Sep 2026 | A visual production planner from a thirteen-requirement brief: seven roles, seven findings in two correction cycles, one word from the person after the brief; 50 minutes, no flagship model | EN |
| [`2026-09-07-api-contract-explorer`](2026-09-07-api-contract-explorer/00-summary.md) | 7 Sep 2026 | An OpenAPI explorer whose Compare and Export views broke on the first click: two high findings from a Reviewer that replaced a failed one mid-session, three security findings, all fixed and re-verified; 50 minutes | EN |
| [`2026-09-07-shipgate`](2026-09-07-shipgate/00-summary.md) | 7 Sep 2026 | A release-readiness tool from a six-sentence brief: fifteen findings in four correction cycles, one reviewer attempt timed out and the session re-routed itself, one word from the person; 1h50, no flagship model | EN |
| [`2026-09-06-queuelens`](2026-09-06-queuelens/00-summary.md) | 6 Sep 2026 | A support-backlog explorer whose first version looked complete: six findings from two independent roles, one of them on the brief's central requirement, all corrected and re-verified; 40 minutes, no flagship model | EN |

## What is not here

The models' prompts and the mechanics of the system are not part of the record, on purpose. The deliverables (the applications themselves) are not in this repository.

Questions and design-partner requests: tandem@jaryn.io

---

Jaryn, Tandem and AWOS names and logos are the property of Jaryn, all rights reserved. See [TRADEMARKS.md](https://github.com/jaryn-io/.github/blob/main/TRADEMARKS.md) and [NOTICE.md](https://github.com/jaryn-io/.github/blob/main/NOTICE.md).
