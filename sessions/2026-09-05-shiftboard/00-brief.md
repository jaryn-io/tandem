# ShiftBoard: a visual production planner, seven roles, two correction cycles

Public record `2026-09-05-shiftboard` · session of 2026-09-05 · 48 messages · duration 0h50 · total tokens 2490905

## Team

| Role | Model | Turns |
|---|---|---|
| Auditor | Claude Haiku 4.5 | 1 |
| Challenger | Gemini 3.6 Flash | 1 |
| Orchestrator | Claude Haiku 4.5 | 11 |
| Planner | GPT-5.6 | 1 |
| Producer | Gemini 3.7 Flash | 4 |
| Reviewer | GLM 5.3 Flash | 3 |
| Security | GPT-5.6 | 2 |

## Brief

Build ShiftBoard, a self-contained visual planning application for a small manufacturer
scheduling jobs across shared machines.

Create the project in a new, uniquely named directory under deliverables/. This session must
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
