# Closeout · Supplier price-list import tool

# Final report

## Objective

Build a locally runnable tool that imports supplier price lists into a company article
catalog. Each supplier delivers files in its own layout (CSV and Excel with different
columns, separators, decimal formats and article codes), so the tool uses a mapping
profile per supplier. An import validates every row, rejects invalid rows with a clear
error report instead of failing the whole file, detects duplicates inside a file and
against the catalog, and shows what would change (new articles, price changes, articles
no longer listed) before applying. Re-importing the same file must not change anything;
importing an updated file applies only the differences and keeps the price history.
Include three sample suppliers with realistic files, including a deliberately dirty one,
and a regression suite. Deliver a usable command-line tool with clear instructions for
adding a new supplier profile.

## Outcome

Status: Completed.

### Orchestrator conclusion

The Reviewer's verification of S06-XLSX-PRICE-PRECISION-001 is sound and independently conclusive. It ran a fresh import against the current file hashes, reproduced both examples the Producer had disclosed: the high-precision decimal (`1.23456789123`) round-trips exactly through parsing, `current_price`, and price history; the tiny value (`1e-11`) is also preserved exactly, just rendered as `1E-11` by Decimal's canonical string form rather than fixed-point. The Reviewer correctly identified that this is not a precision-loss regression — the reader's fixed-point text is what gets parsed, but `str(Decimal)` re-normalizes very small/large magnitudes to exponent notation on the way to storage. The stored value is numerically identical to the input either way, and the finding was about lost precision, not about display format. It also checked an ordinary price and ran the full 78-test suite. This is adequate independent verification of the correction; no residual defect.

This closes the last pending step. All prior findings (S03-DIFF-DESCRIPTION-001, S04-PRICE-AMBIGUITY-002, S04-RESOURCE-BOUNDS-004, S04-SUPPLIER-ISOLATION-001, S04-TERMINAL-OUTPUT-003, S06-REVIEW-LINEAGE-001) are already verified_closed. No open steps remain in the plan projection. Closing the session.

## Accepted deliverables

- `the project folderREADME.md`
- `the project folderpricelist_import/__init__.py`
- `the project folderpricelist_import/__main__.py`
- `the project folderpricelist_import/cli.py`
- `the project folderpricelist_import/diffapply.py`
- `the project folderpricelist_import/duplicates.py`
- `the project folderpricelist_import/engine.py`
- `the project folderpricelist_import/models.py`
- `the project folderpricelist_import/profiles.py`
- `the project folderpricelist_import/readers.py`
- `the project folderpricelist_import/store.py`
- `the project folderpricelist_import/validation.py`
- `the project folderrequirements.txt`
- `the project foldersamples/acme/listino_2026-09.csv`
- `the project foldersamples/acme/listino_2026-10.csv`
- `the project foldersamples/bricomania/listino_2026-09.csv`
- `the project foldersamples/nordwind/preise_2026-09.xlsx`
- `the project foldersamples/nordwind/preise_2026-10.xlsx`
- `the project foldersamples/profiles/acme.json`
- `the project foldersamples/profiles/bricomania.json`
- `the project foldersamples/profiles/nordwind.json`
- `the project foldersamples/regenerate_xlsx.py`
- `the project foldertests/test_cli.py`
- `the project foldertests/test_diffapply.py`
- `the project foldertests/test_engine.py`
- `the project foldertests/test_samples.py`

## Plan

See `01-plan.md`.

## Findings

- S03-DIFF-DESCRIPTION-001 · Severity: medium
  Status: Verified and closed.
- S04-PRICE-AMBIGUITY-002 · Severity: medium
  Status: Verified and closed.
- S04-RESOURCE-BOUNDS-004 · Severity: medium
  Status: Verified and closed.
- S04-SUPPLIER-ISOLATION-001 · Severity: medium
  Status: Verified and closed.
- S04-TERMINAL-OUTPUT-003 · Severity: medium
  Status: Verified and closed.
- S06-REVIEW-LINEAGE-001 · Severity: low
  Status: Verified and closed.
- S06-XLSX-PRICE-PRECISION-001 · Severity: medium
  Status: Verified and closed.

## Positive Memory

# Positive memory candidate — supplier price-list import tool

## Reusable lessons from this session

**Escalate environment-changing actions instead of working around them.** When the Excel path could not be exercised because `openpyxl` was missing, the Producer did not install it on its own authority and did not silently skip the verification: it ran everything else (29 tests passing, 2 Excel tests explicitly skipped) and the Orchestrator asked the human a concrete question with two clear options, authorize the install now or defer with the gap tracked. The human authorized it in one short reply and the Excel path got real execution evidence before later steps built on it. Apply this whenever a step needs something outside the deliverable's authority (package installs, system changes, credentials): state exactly what is missing, what works without it, and what each choice unblocks. Avoid both failure modes: unilateral installs, and quiet skipping that lets untested code become load-bearing.

**A finding closes only when the role that raised it verifies the fix — never on the Producer's own claim.** Across five consecutive rounds the Orchestrator explicitly declined to close findings based on Producer reports alone, and each `verified_closed` came only after Reviewer or Security re-ran the check. This discipline caught real residual problems and kept the finding ledger trustworthy. The Auditor later confirmed no finding was closed early.

**Review verdicts bind to specific file states — fixes after a review invalidate its coverage.** The Auditor's sharpest catch (S06-REVIEW-LINEAGE-001) was procedural, not a product defect: the Reviewer's PASS applied to particular file hashes, and subsequent Security-driven fixes changed those same files, so the final delivered code had never passed a functional review. The correction was a focused Reviewer pass on the final hashes. Apply whenever corrections land after a review or audit: check whether the reviewed artifacts still match what will ship, and re-review the final state rather than assuming the old verdict carries over.

**Security checklist for tools that ingest third-party data files.** The four findings from the Security pass generalize well beyond this importer:
- Shared business keys need an ownership model: an article code from supplier B's file could silently overwrite supplier A's record because the catalog keyed on code alone.
- Locale-aware number parsing must distinguish typed numeric cells from ambiguous text: `1.234` under a profile declaring `.` as thousands separator parsed as one-and-a-bit instead of 1234, because the parser accepted a plain-looking number before applying the profile.
- Anything supplier-controlled that reaches a terminal must be escaped: an unescaped `\x1b[2J` in a description survived into CLI output.
- Untrusted files need size, row-count and decompression-expansion bounds *before* parsing, or a hostile archive exhausts resources before any error report exists.

**Money and prices: build `Decimal` from fixed-point text, never from binary float.** The last finding was a precision case where high-precision decimals (`1.23456789123`) and tiny values (`1e-11`) had to round-trip exactly through parsing, storage and price history. The fix kept fixed-point text as the parse source. A useful corollary the Reviewer established: `str(Decimal)` re-normalizes very small magnitudes to exponent form (`1E-11`) — that is a display rendering, not precision loss, so verify numeric equality rather than string format before declaring a regression.

## Verdict

verdict: pass
