# API Contract Explorer: two views that broke on first use, three security holes, one reviewer swapped mid-session

Public record `2026-09-07-api-contract-explorer` · session of 2026-09-07 · 54 messages · duration 0h50 · total tokens 1654788

## Team

| Role | Model | Turns |
|---|---|---|
| Auditor | Claude Haiku 4.5 | 1 |
| Orchestrator | Claude Haiku 4.5 | 12 |
| Planner | GPT-5.6 | 1 |
| Producer | Gemini 3.7 Flash | 5 |
| Reviewer | Claude Sonnet 5 | 1 |
| Reviewer | GLM 5.3 Flash | 4 |
| Security | GPT-5.6 | 1 |

## Brief

Build a complete local web application called “API Contract Explorer” in
deliverables/api-contract-explorer/. It must let a software engineer import or paste
an OpenAPI JSON document, browse endpoints grouped by tag, inspect parameters,
request bodies, response schemas and examples, search and filter endpoints, compare
two operations side by side, and export a concise Markdown reference for selected
endpoints. Include a realistic built-in sample API so the application is useful
immediately without external services. Changes made in the interface must persist
locally between browser sessions. Provide a clear README with the exact command and
URL needed to run it. The finished application must be responsive, accessible and
fully interactive rather than a static mock-up.
