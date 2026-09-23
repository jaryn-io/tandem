# Positive Memory · Supplier price-list import tool

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
