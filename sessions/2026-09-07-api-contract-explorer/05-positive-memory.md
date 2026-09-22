# Positive Memory · API Contract Explorer: two views that broke on first use, three security holes, one reviewer swapped mid-session

# Positive Memory — Secure, Zero-Dependency Local Web Applications for Developer Tools

## 1. Reusable Pattern: Decoupled Core Logic with Idempotent UI Mount Lifecycle

When building zero-dependency local web applications (runnable via loopback HTTP or direct `file://` access):

- **Decouple computation from DOM manipulation**: Maintain pure parsing, filtering, diffing, and export formatting in a standalone module (`core.js`) and UI controllers in a separate module (`app.js`). This enables fast, automated testing directly in Node.js without requiring mock DOM environments or browser test runners.
- **Enforce centralized, idempotent stage rendering**: In vanilla JavaScript event-driven architectures, interactive controls (such as dropdown selectors, tab switchers, and bulk toggle buttons) must not call sub-render functions directly if those functions assume an empty container. Directly re-invoking sub-renderers without clearing the mount stage (`stage.innerHTML = ''`) causes cumulative DOM duplication on repeated user interactions.
- **Guideline**: Route view updates through a single top-level rendering dispatcher that owns container teardown and rebuild, or update existing DOM nodes in-place rather than appending new components.

## 2. Security Boundaries for Untrusted Specifications & Code Generation

Developer utilities that parse external specifications (like OpenAPI / Swagger JSON) must treat all document content as untrusted input:

- **Neutralize DOM XSS across all metadata sinks**: OpenAPI operation summaries, parameter names, default values, enums, schema models, and example payloads can carry malicious payloads. Systematically sanitize all text before insertion into HTML templates (`escapeHtml()`) or assign values via `textContent`. In addition, sanitize parsed JSON objects to strip `__proto__`, `constructor`, and `prototype` keys to prevent prototype pollution.
- **Enforce POSIX single-quote escaping for generated shell snippets**: When generating cURL or CLI invocation commands from user-supplied endpoints and parameters, naive string interpolation allows command injection (`$(...)`, backticks, semicolons) if a user copies snippets into their terminal. Wrap all dynamic values (paths, query strings, headers, request bodies) in single quotes and escape embedded single quotes as `'\''`.
- **Reconcile persistence requirements with data confidentiality**: When a brief requests session persistence, consider that unencrypted `localStorage` can expose proprietary endpoints, authorization tokens, or internal schemas. Implement a safe default (in-memory, session-only storage) with explicit user opt-in for `localStorage` persistence, paired with a one-click purge control.
- **Maintain documentation consistency during security refactoring**: When a security remediation alters application behavior (such as moving from automatic persistence to opt-in storage), update user documentation and architectural descriptions immediately. Stale claims in user documentation erode trust and cause verification failures during review and audit.
