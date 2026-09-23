# Supplier price-list import tool

Public record `2026-09-23-pricelist-import` · session of 2026-09-23 · 91 messages · duration 1h34 · total tokens 1125086

## Team

| Role | Model | Turns |
|---|---|---|
| Auditor | Claude Opus 5.5 | 1 |
| Orchestrator | Claude Sonnet 5 | 22 |
| Planner | GLM 5.3 Flash | 1 |
| Producer | Kimi K3 | 10 |
| Reviewer | GPT-6 Luna | 6 |
| Security | GPT-6 Sol | 4 |

## Brief

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
