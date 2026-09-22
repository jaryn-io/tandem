# Plan · API Contract Explorer: two views that broke on first use, three security holes, one reviewer swapped mid-session

## Revision 1 (message 005) — approved

Plan revision 1

The implementation will proceed through four meaningful stages:

1. **S01 — Build the application (Producer)**  
   Implement the complete application in `./`, including the responsive accessible interface, built-in sample OpenAPI document, JSON import/paste flow, endpoint grouping and inspection, search/filtering, side-by-side comparison, Markdown export, browser-local persistence, and README with exact startup commands and URL. The implementation should use a straightforward local web-app structure that runs without external services.

2. **S02 — Conformity review (Reviewer)**  
   Independently inspect the produced application against the original brief and S01 output. Verify that the required workflows are genuinely interactive and connected, that endpoint details and export behavior are complete, that persistence works as intended, and that responsive and accessibility requirements are addressed. Record any concrete gaps for Orchestrator disposition.

3. **S03 — Security assessment (Security)**  
   Assess the application’s local data handling and imported OpenAPI processing, including malformed or unexpected JSON, rendering safety, browser storage behavior, download/export handling, dependency and startup assumptions, and exposure of data beyond the local application. Report actionable findings without modifying the product.

4. **S04 — Final independent audit (Auditor)**  
   Audit the complete claim-to-evidence chain after review and security assessment. Confirm that the final implementation, README, required behavior, scope, and specialist reports collectively support the requested landing condition. This is the final planned gate and should identify unresolved discrepancies or confirm readiness for Orchestrator’s final decision.

Dependencies are intentionally ordered so S01 produces the artifact before independent examinations begin; S02 and S03 both depend on S01 and may be evaluated independently; S04 depends on both specialist assessments. The plan does not assume browser-based verification unless the active review or audit performs it as useful evidence; the core requirement is that the delivered local application be complete and verifiably functional.

| Step | Owner | Depends on |
|---|---|---|
| S01 | producer | — |
| S02 | reviewer | S01 |
| S03 | security | S01 |
| S04 | auditor | S02, S03 |
