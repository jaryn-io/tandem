# Plan · Supplier price-list import tool

## Revision 1 (message 005) — approved

Plan revision 1

## Integrated plan — supplier price-list import tool

**Objective (from the original brief, 001):** a locally runnable command-line tool that imports supplier price lists into an article catalog, using per-supplier mapping profiles to absorb layout differences (CSV and Excel, different columns, separators, decimal formats, article-code shapes). Row-level validation with per-row rejection reporting, in-file and against-catalog duplicate detection, a dry-run diff preview (new / changed / no-longer-listed), idempotent re-import of the same file, differential import of updated files preserving price history, three sample suppliers (one deliberately dirty), a regression suite, and documentation for adding a new supplier profile.

**Proposed technology baseline (assumption Producer may refine, not an open question):** a single self-contained CLI in Python 3 with a small dependency set — `openpyxl` for Excel reading, the standard `csv` module for delimited text, `sqlite` (stdlib) as the local catalog/price-history store so the tool stays runnable offline with no server. The catalog, import runs and price history all live in one local database file; mapping profiles are declarative YAML/JSON files, one per supplier.

**Plan structure and rationale:**

- **S01 – Producer: core import engine.** Domain model (catalog article, price entry, price history), mapping-profile schema and loader, CSV and Excel readers driven purely by profiles (column mapping, separator, decimal format, code normalization), row validation with per-row error collection and a structured rejection report, in-file duplicate detection and against-catalog duplicate detection, and catalog storage. This is the foundation everything else exercises.
- Placed here, before security, so functional fixes can still be made cheaply.
- **S04 – Security: ingestion attack-surface review.** Strict Security step focused on the file-parsing surface: CSV injection/formula-formulae in Excel cells, malformed or hostile workbook structures (deeply nested/oversized files, entity-style tricks via `openpyxl`), decimal and encoding edge cases used to corrupt prices, path handling of user-supplied file paths and the local database, and resource-exhaustion-by-large-file behavior. Findings go to a rework step.
- **S05 – Producer: remediation of S03+S04 findings.** Only executed if findings exist; Producer resolves confirmed functional and security findings and re-runs the regression suite.
- **S06 – Auditor: final audit (final gate).** Independent audit of the complete claim-to-evidence chain: every promised behavior demonstrated by the regression suite or direct execution evidence, the clean suppliers import idempotently, the dirty supplier produces a precise rejection report, diff/apply/history behave as documented, and all S03–S05 findings are closed.

**Material assumptions:** single-user local tool (no concurrency, no auth surface beyond the local filesystem); catalog persisted as one local database file inside the deliverable directory; Excel means `.xlsx` readable by `openpyxl` (legacy `.xls` out of scope unless Producer finds it trivially supportable). All three are conservative readings of the brief and do not need a human decision.

**Limits and open questions for Orchestrator:** none blocking. The only real choice embedded is the language/storage baseline above; it is isolated here rather than buried, and Orchestrator may substitute it before S01 without changing step structure.

| Step | Owner | Depends on |
|---|---|---|
| S01 | producer | — |
| S02 | producer | S01 |
| S03 | reviewer | S02 |
| S04 | security | S02 |
| S05 | producer | S03, S04 |
| S06 | auditor | S05 |

Correction steps added during the session are numbered G-1, G-2… in order of creation; `-verify` marks the independent re-check of a correction.
