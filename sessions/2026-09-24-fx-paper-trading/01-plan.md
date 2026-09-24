# Plan · FX Paper Trading and Options Risk Terminal

## Revision 1 (message 005) — approved

Plan revision 1

I’ll return an ordered, assignable plan that covers the FX feed, paper trading and persistence, options risk, analytics, and the dealing terminal, with the required strict-role sequence.
## Integrated plan

**S01 — Establish the market-data and valuation foundation (Producer).** Connect to biquote’s public feed for EUR/USD, GBP/USD, and USD/JPY. Build the quote lifecycle for bid/ask, source, timestamps, age, connection and market state; retain the last display while blocking simulated fills whenever the market is closed, the feed is unavailable, or an active quote is older than the configurable 30-second limit. Load the feed’s historical candles for charts. Define explicit currency units, reporting-currency conversion inputs, and the market data used for subsequent execution and valuation.

**S02 — Build portfolios, spot execution, and durable records (Producer).** Add separate locally persisted paper portfolios with starting equity, positions, orders, and history. Implement market and limit buy/sell tickets for long and short spot positions, using ask for market buys and bid for market sells; limit fills use the available bid or ask when an incoming quote reaches the order price. Record each fill’s quote and timestamp in an enduring blotter, and represent pending, filled, cancelled, and rejected orders. Keep the first-launch trading record empty.

**S03 — Add European options, Greeks, and spot hedging (Producer; depends on S01 and S02).** Implement simulated vanilla calls and puts on the displayed pairs, with explicit notional, strike, expiry, volatility, and domestic and foreign rate assumptions. Calculate premiums and Greeks with Garman–Kohlhagen. Label option marks and fills as model-derived, show the assumed execution spread, and never present theoretical premiums as observed quotes. Add position and aggregate signed delta with clear units, and a risk-view spot hedge whose effect on delta and exposure is visible.

**S04 — Deliver the terminal’s analysis and demonstration workflow (Producer; depends on S01–S03).** Present market watch, charts, order ticket, blotter, positions, risk, and P&L in a usable live-demo flow. Add selectable chart timeframes and SMA, EMA, RSI, and MACD from feed candles, keeping indicators separate from order execution. Show realized and unrealized P&L, equity over time, currency exposure, spread cost, and results by pair and position. Distinguish bid/ask-based spot P&L from model-marked option P&L; show reporting-currency conversion rate and timestamp. Provide final analysis of trade sequence, cumulative P&L, and spot, option, and hedge contributions.

**S05 — Review the integrated product against the brief (Reviewer; depends on S04).** Check the completed application and its evidence against all requested behavior, especially stale/closed-market fill suspension, execution-side pricing, immutable fill records, units and conversion disclosures, option labeling and model assumptions, delta hedging, persistence, empty first-launch history, and indicator non-execution. Report findings with evidence to the Orchestrator.

**S06 — Assess security and external-boundary behavior (Security; depends on S01 and S04).** Examine feed integration, local persistence, order execution boundaries, and configuration for the stated constraint that no trade reaches a broker or external trading account. Report security findings and evidence to the Orchestrator.

**S07 — Audit the complete claim-to-evidence chain (Auditor; depends on S05 and S06).** Independently audit the delivered result and review reports against the original brief, checking that material claims are supported by observed evidence and that gaps or limitations remain explicit. Return the audit to the Orchestrator.

| Step | Owner | Depends on |
|---|---|---|
| S01 | producer | — |
| S02 | producer | S01 |
| S03 | producer | S01, S02 |
| S04 | producer | S01, S02, S03 |
| S05 | reviewer | S04 |
| S06 | security | S01, S04 |
| S07 | auditor | S05, S06 |

Correction steps added during the session are numbered G-1, G-2… in order of creation; `-verify` marks the independent re-check of a correction.
