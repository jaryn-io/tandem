# Summary · API Contract Explorer: two views that broke on first use, three security holes, one reviewer swapped mid-session

**In one line.** A developer tool from a ten-requirement brief: import an OpenAPI document, browse, inspect, search, compare two operations, export Markdown. The first version passed its own checks; the Reviewer broke the Compare and Export views on the first ordinary click, Security found three holes, all six findings were fixed and re-verified. When the first Reviewer attempt failed on its browser tool, Tandem swapped in another model within seconds. One word from the person. 54 messages, 0h50, 1.65M tokens.

**Language.** Everything is in English as written. Role outputs are the roles' own text.

## The brief

Import or paste an OpenAPI JSON document; browse endpoints grouped by tag; inspect parameters, request bodies, response schemas and examples; search and filter; compare two operations side by side; export a concise Markdown reference for selected endpoints; a realistic built-in sample; changes made in the interface persist locally between browser sessions; a README with the exact command and URL; responsive, accessible, fully interactive rather than a static mock-up.

## What happened

1. **Plan.** Four steps: Producer, Reviewer, Security, Auditor. Approved as submitted.
2. **First version (S01, Producer, Gemini 3.7 Flash).** Eight files, two sample APIs, 22 operations, import, navigation, comparison, export, persistence, tests, README. Its own browser scenario passed, but it had only looked at the Compare view's first render.
3. **Review (S02).** The first Reviewer attempt (GLM 5.3 Flash) hit three consecutive browser-tool errors while it was surfacing a defect; Tandem stopped that attempt, discarded its partial output and handed the same step to Claude Sonnet 5 two seconds later. That Reviewer drove the real interface and found two high-severity defects: every change of the Compare selectors or swap button stacked a new copy of the whole panel under the old one, and Select All or Deselect All did the same in the Export view. Two of the six workflows the brief names were interactive only on their first render. Corrected by the Producer; the Reviewer (GLM 5.3 Flash again) re-drove every interaction and confirmed one panel throughout.
4. **Security (S03, GPT-5.6).** Three findings: imported specification values reached the page as HTML; generated cURL commands interpolated imported URLs and headers without shell quoting; imported specifications, which often contain secrets, were saved in plaintext browser storage by default. Corrected with systematic escaping, POSIX quoting and a session-only default with explicit opt-in; the Reviewer verified with a hostile specification imported through the real dialog, twelve payloads round-tripped through a real shell, and live inspection of browser storage. One leftover README sentence still promised automatic persistence of imported contracts; corrected and verified.
5. **Audit (S04, Claude Haiku 4.5).** Ten brief requirements traced to the delivered files; six findings traced from discovery to correction to verification; artefacts identical across rounds. Session completed and sealed. Positive Memory validated.

## What the record does not prove

Responsiveness at mobile widths and accessibility were checked in the markup and stylesheet, not measured with a mobile viewport or an accessibility audit. The Reviewer says so in every round. The persistence requirement was deliberately narrowed after the security finding: interface state persists automatically, imported contracts only on opt-in, and the README says so.

## Why it is published

A tool a developer understands at a glance, and a first version that would have shipped with its two main interactive views broken. The record also shows a role's attempt failing on its tooling and the session continuing with another model, without the person noticing until the end.

## Files

`00-brief.md` · `01-plan.md` · `02-record.md` · `03-findings.md` · `04-closeout.md` · `05-positive-memory.md`
