# Closeout · ShipGate: fifteen findings, one reviewer timeout, no one asked

# Final report

## Objective

Build a complete, usable web application for managing a small software team’s release
readiness. Users must be able to create releases, add acceptance criteria and risks,
record supporting evidence, track unresolved blockers, and understand whether each
release is ready. All records must be editable and persistent. Include a useful
populated example and clear setup and run instructions. The result must behave as a
real working tool rather than a static mockup.

## Outcome

Status: Completed.

### Orchestrator conclusion

The Reviewer’s independent review verification is accepted. It positively verifies both AUD-01 and AUD-02 using matching artifact hashes, static inspection, 16/16 isolated tests, and a browser regression pass. No open findings or missing proof remain; all approved plan obligations are complete.

## Accepted deliverables

- `./README.md`
- `./data/releases.json`
- `./data/sample-releases.json`
- `./package.json`
- `./public/app.js`
- `./public/index.html`
- `./public/styles.css`
- `./server.js`
- `./server.py`
- `./test.js`

## Plan

See `01-plan.md`.

## Findings

- AUD-01 · Severity: low
  Status: Verified and closed.
- AUD-02 · Severity: low
  Status: Verified and closed.
- F01 · Severity: medium
  Status: Verified and closed.
- F02 · Severity: medium
  Status: Verified and closed.
- F03 · Severity: medium
  Status: Verified and closed.
- F04 · Severity: medium
  Status: Verified and closed.
- F05 · Severity: medium
  Status: Verified and closed.
- F06 · Severity: low
  Status: Verified and closed.
- F07 · Severity: low
  Status: Verified and closed.
- F08 · Severity: low
  Status: Verified and closed.
- F09 · Severity: low
  Status: Verified and closed.
- SEC-01 · Severity: high
  Status: Verified and closed.
- SEC-02 · Severity: medium
  Status: Verified and closed.
- SEC-03 · Severity: medium
  Status: Verified and closed.
- SEC-04 · Severity: low
  Status: Verified and closed.

## Positive Memory

# Positive Memory Candidate — Session 311 (Release Readiness Web Application)

## 1. Test Suite Persistence Isolation
- **Context & Risk**: In file-backed applications, test harnesses frequently need baseline or sample data to execute assertions. If tests operate directly on default storage files (such as `data/releases.json`) or call reset routines during execution, running a routine test command (`npm test`) silently wipes out user data accumulated in development or production.
- **Reusable Practice**: Parameterize data and storage locations through environment variables (`DATA_DIR`, `RELEASES_FILE`, `SAMPLE_FILE`). Implement test runners to allocate an isolated temporary directory (e.g. `fs.mkdtempSync`) on startup, point the environment overrides to that scratch path, and purge the temporary workspace upon test exit.
- **When to Apply**: Any file-backed or local-persistence application shipping an automated test suite.
- **What to Avoid**: Never point automated test runners at production or default data paths, and do not rely on end-of-suite reset functions to restore overwritten state.

## 2. Dual-Target Modules for Dependency-Free Vanilla Applications
- **Context & Risk**: In zero-build web applications (vanilla HTML/JS without bundlers), domain logic such as validation, scoring engines, or policy calculations resides in browser scripts. If these functions are not exported, test harnesses are tempted to reimplement duplicate shadow logic in unit test files. This creates a false sense of coverage while leaving the real shipped implementation unverified.
- **Reusable Practice**: Structure vanilla JavaScript modules to support both browser script-tag loading and CommonJS/Node testing environments. Guard DOM references behind runtime checks (`typeof window !== 'undefined'`) and conditionally export the core domain functions:
  ```javascript
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { computeReadiness, validateReleasesPayload, isSafeHttpUrl };
  }
  ```
- **When to Apply**: When building client-side or zero-dependency applications that require automated unit testing of core business logic without introducing build or bundling toolchains.
- **What to Avoid**: Never maintain parallel duplicate calculation logic in test files; test the exact functions that execute in production.

## 3. Defensive Batch Ingestion and Rendering Fallbacks
- **Context & Risk**: Bulk data import features (e.g., JSON file uploads or clipboard paste) often assume well-formed structures. Committing unvalidated records directly into application state or persistent storage can cause runtime crashes across the entire UI (such as property access on undefined values), permanently locking users out of their data.
- **Reusable Practice**: Implement strict structural schema validation upfront on both client and server before persisting changes or updating in-memory state. If any item in a batch payload fails required field or type constraints, reject the operation atomically without modifying the store. Additionally, equip UI render loops with defensive fallbacks (e.g. `item.status || 'pending'`) so that malformed or legacy records fail gracefully rather than crashing the interface.
- **When to Apply**: Any bulk ingestion, import, or synchronization boundary in web applications.
- **What to Avoid**: Never assign raw, unvalidated external payloads directly to persistent state; do not write partial batch updates when any element fails validation.
