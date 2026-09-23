# Tandem examples — the applications the sessions produced

Each folder here is an application accepted at the closeout of a real Tandem session, published exactly as delivered: no edits after the session, tests included, one command to run. The session that produced it, every message, is in [`sessions/`](../sessions). Read the record, then run the code and check one against the other.

They are internal test deliverables, not client work and not products: the briefs were written to be demanding on purpose.

| Application | What it is | Session record | Delivered | Size |
|---|---|---|---|---|
| [`forgedesk-2`](forgedesk-2) | Management system for a makerspace: members and qualifications, machines and maintenance, single and recurring bookings with waiting list, consumables ledger, per-minute charges, append-only audit. Python standard library and SQLite | [`2026-08-30-forgedesk-2`](../sessions/2026-08-30-forgedesk-2/00-summary.md) | 30 Aug 2026 | 27,800 lines, 174 tests |
| [`shiftboard`](shiftboard) | Visual production planner for a small workshop: jobs with due dates and ordered operations, machines with shifts and downtime, a drag-and-drop schedule with explained conflicts, automatic rescheduling with undo, utilisation and lateness KPIs. HTML and JavaScript, no dependencies | [`2026-09-05-shiftboard`](../sessions/2026-09-05-shiftboard/00-summary.md) | 5 Sep 2026 | 11 files, 19 tests |
| [`queuelens`](queuelens) | Support-backlog explorer: import tickets from CSV or JSON, search and filter by priority, status and assignee, find duplicates, export the view, and see malformed records explained rather than dropped. Single page plus a small Python server | [`2026-09-06-queuelens`](../sessions/2026-09-06-queuelens/00-summary.md) | 6 Sep 2026 | 12 files, 35 tests |

Sessions whose application is not here yet: [API Contract Explorer](../sessions/2026-09-07-api-contract-explorer/00-summary.md), [ShipGate](../sessions/2026-09-07-shipgate/00-summary.md).

Each application carries its own MIT licence. Jaryn, Tandem and AWOS names and logos are the property of Jaryn, all rights reserved: see [TRADEMARKS.md](../TRADEMARKS.md) and [NOTICE.md](../NOTICE.md).

Questions: tandem@jaryn.io
