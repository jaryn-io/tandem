# Positive Memory · QueueLens: a first version that looked complete, and what independent checks found

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
