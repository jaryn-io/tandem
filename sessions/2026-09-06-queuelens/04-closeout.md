# Closeout · QueueLens: a first version that looked complete, and what independent checks found

# Final report

## Objective

Build QueueLens, a self-contained application for exploring a local support-request
backlog. Import CSV or JSON, search and filter requests by priority, status and
assignee, identify duplicates, and export the selected view. Explain malformed or
excluded records without silently losing usable data. Include realistic sample data,
a polished interface, clear startup instructions and documented limitations. Process
everything locally. Create a uniquely named directory under the project folder/ without
consulting or reusing previous sessions or their artifacts.

## Outcome

Status: Completed.

### Orchestrator conclusion

# Orchestrator Judgment — S04 Auditor Final Audit

The Auditor has completed a thorough, source-based claim-to-evidence audit of the complete QueueLens session. The submission is adequate and the verdict is correct. All material session obligations have been met with credible evidence.

## Evidence Review

**Auditor performed:**
- Complete brief-to-deliverable mapping across all 11 substantive obligations
- Full trace of six findings (three security, three review) from source through correction to verification
- Code inspection of security and data-preservation critical paths
- Automated test execution: 35 tests, all pass
- Live browser verification: comprehensive scenarios including the core obligation (malformed-record preservation visibility to user)
- Contradiction and coherence check: zero contradictions found

**Core obligation status:**
The brief's foundational requirement—"explain malformed or excluded records without silently losing usable data"—was initially violated in S01 (R03-F02: values dropped despite claims of preservation). Producer corrected it by implementing actual preservation in `custom_fields`; Reviewer verified the correction live in browser with user-visible preservation messaging and CSV export preservation. This is now verified closed.

**All six findings verified closed:**
- **SEC-001** (stored DOM XSS): HTML escaping applied across all views, CSP hardened to `script-src 'self'`, verified via automated XSS injection tests and browser execution
- **SEC-002** (CSV formula injection): Formula-triggering prefixes neutralized in export, verified via automated string-level checks and unit assertions  
- **SEC-003** (non-loopback server binding): Binding restricted to loopback only, non-loopback addresses rejected at startup with explicit security error
- **R03-F01** (documentation accuracy): README rewritten to match implemented duplicate-detection algorithm and explicit scale limitations with empirical latency data
- **R03-F02** (preservation accuracy): Values now both preserved and explained correctly; live browser test confirmed user sees the preservation wording in modal and diagnostics
- **R03-F03** (test robustness): Assertions hardened to exit code 1 on failure, Python wrapper correctly interprets exit code; negative test confirms visibility

**No residual material gaps:**
Auditor noted two minor unexercised proofs (performance latencies not re-benchmarked, formula behavior not tested in live spreadsheet), but both fall outside the three remediation targets and do not block closure. Code structure supports the documented latency model; formula-neutralization logic is verified at the string level.

## Disposition

S04 is the final gate. All dependencies satisfied, all findings verified closed, all obligations met. The session is ready for closure.

## Accepted deliverables

- `./README.md`
- `./app.js`
- `./index.html`
- `./run.sh`
- `./sample-data.js`
- `./sample-data/support_tickets_standard.csv`
- `./sample-data/support_tickets_standard.json`
- `./sample-data/support_tickets_with_issues.csv`
- `./sample-data/support_tickets_with_issues.json`
- `./server.py`
- `./styles.css`
- `./test_queuelens.py`

## Plan

See `01-plan.md`.

## Findings

- R03-F01 · Severity: low
  Status: Verified and closed.
- R03-F02 · Severity: low
  Status: Verified and closed.
- R03-F03 · Severity: low
  Status: Verified and closed.
- SEC-001 · Severity: high
  Status: Verified and closed.
- SEC-002 · Severity: medium
  Status: Verified and closed.
- SEC-003 · Severity: medium
  Status: Verified and closed.

## Positive Memory

# Positive Memory: Patterns & Lessons from QueueLens

## 1. Eliminating Explanatory Illusions in Data Preservation & Normalization

### Context & Problem
When designing data ingestion pipelines with a "fault-tolerant, no-silent-data-loss" requirement, applications often fall back to default values when standard fields are missing, invalid, or malformed. In this session, the normalizer emitted diagnostic warnings asserting that unrecognized priority/status values were defaulted and *"(preserved in custom_fields)"*. In reality, because the custom-fields extractor stripped recognized standard column keys, the unmapped raw values were dropped. The user interface presented a reassuring explanation of data preservation while silently discarding the user's raw data.

### Reusable Pattern
- **Decouple Raw Preservation from Normalization Fallbacks**: Before mapping or defaulting any standard field, preserve the raw, unmodified value into an unconstrained secondary store under both its original input key and an explicit canonical key (e.g., `raw_<field>` in `custom_fields`).
- **Verify Explanation Truthfulness in Tests**: Assert that the condition described in user-facing warnings matches actual runtime retention. Specifically test that after an invalid field is coerced to a default, the original input value remains accessible in memory, visible in detail views, and preserved in exported data.
- **Export Separation**: In tabular exports (like CSV), ensure that preserving raw fields does not duplicate standard column headers while guaranteeing that raw preservation columns (e.g., `raw_priority`, `raw_status`) are exported cleanly.

### What to Avoid
- Never emit warning messages stating that data has been safely preserved or stashed without an end-to-end assertion confirming the data is present in the record output.
- Avoid using standard field key names directly in an unpartitioned custom attributes bag without collision handling against base schema headers.

---

## 2. Hardening Headless Test Runners Against `console.assert` Silent Passes

### Context & Problem
When creating self-contained applications with lightweight test suites, developers frequently create a wrapper script (e.g., in Python) that invokes a headless JavaScript engine (such as Node.js) via subprocess. In Node.js, `console.assert(condition, message)` logs an error message to `stderr` but **does not throw an exception or set a non-zero exit code** (`process.exitCode = 1`). If the outer wrapper checks only the subprocess exit status and stdout markers, test suites will report a false "ALL TESTS PASSED" even when multiple assertions fail.

### Reusable Pattern
- **Strict Assertion Primitives**: In standalone Node.js test scripts, define an explicit assertion function that writes to `stderr` and immediately terminates execution with `process.exit(1)`:
  ```javascript
  function assert(condition, message) {
    if (!condition) {
      console.error(`[ASSERTION_FAILED] ${message}`);
      process.exit(1);
    }
  }
  console.assert = assert;
  ```
- **Automate Negative Assertion Testing**: Include an explicit test phase within the test runner that intentionally executes a failing assertion in a child process, proving that the outer runner properly catches non-zero exit codes and fails the suite if an assertion fails.

### What to Avoid
- Never assume browser assertion APIs and Node.js assertion APIs share exit semantics.
- Do not rely solely on string matching (e.g., looking for "ALL_TESTS_PASSED") without validating that exit codes are non-zero upon failure.

---

## 3. Grounding Documented Limitations in Empirical Complexity

### Context & Problem
Documentation in early iterations often includes aspirational claims (e.g., "drag-and-drop Kanban view", "runs smoothly at 60 FPS up to 15,000 tickets"). In practice, interactive card movement was not implemented (the Kanban was click-to-inspect only), and the in-browser duplicate detection engine relied on pairwise comparison with an $O(n^2)$ time complexity. At 1,000 records the duplicate check completed in ~385 ms, but at 15,000 records it would freeze the single-threaded browser UI for approximately 90 seconds.

### Reusable Pattern
- **Empirical Complexity Benchmarking**: When an application performs in-memory algorithmic analysis (such as pairwise clustering, Levenshtein/Jaccard similarity, or disjoint-set union), measure execution time across dataset sizes (e.g., 1k, 4k, 8k records) and document the exact bottleneck in a dedicated "Documented Limitations" section.
- **Precise Interaction Documentation**: Clearly distinguish between visual grouping containers (like status columns with click-to-open drawers) and full drag-and-drop state manipulation, ensuring documentation reflects what is implemented rather than expected conventions.

### What to Avoid
- Avoid promising universal 60 FPS performance or linear scalability when core heuristics are quadratic.
- Do not omit the computational bottleneck from documented limitations when client-side processing is an explicit design requirement.

---

## 4. Multi-Layer Local-Only and Export Security

### Context & Problem
Self-contained, local-only applications that process user-supplied CSV/JSON backlogs face specific security risks even without backend databases:
1. **Stored DOM XSS**: Untrusted ticket titles, descriptions, and custom fields rendered into modals, tables, or boards.
2. **Formula Injection (CSV Injection)**: Exported backlog data containing cells starting with `=`, `+`, `-`, or `@` executed by spreadsheet software (Excel, LibreOffice).
3. **Network Boundary Drift**: Local development servers accepting arbitrary host arguments like `0.0.0.0` or external IPs, violating the offline local guarantee.

### Reusable Pattern
- **Strict Content Security Policy & DOM Construction**: Enforce `script-src 'self'` without `'unsafe-inline'`, eliminate inline HTML event handlers (`onclick`), and construct dynamic cards/tables using DOM APIs (`document.createElement`, `textContent`) or strict HTML entity escaping for all interpolated properties.
- **Selective Formula Neutralization**: Neutralize CSV export cells starting with `=`, `+`, `-`, `@`, `\t`, or `\r` by prefixing with a single quote (`'`), while preserving untampered, raw string representations in JSON exports.
- **Loopback Enforcement**: Validate server bind addresses programmatically using loopback IP checks (`127.0.0.1`, `localhost`, `::1`) and reject external interfaces with an explicit exit code and security warning.
