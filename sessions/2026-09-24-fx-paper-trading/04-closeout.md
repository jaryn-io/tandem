# Closeout · FX Paper Trading and Options Risk Terminal

# Final report

## Objective

Brief — FX Paper Trading and Options Risk Terminal

Build a locally runnable FX trading workstation for simulated trading against live market data. It should feel like a
usable dealing and risk terminal: a user can watch prices move, place paper trades, manage distinct portfolios, hedge
an option position, and understand the resulting profit or loss. No trade may be sent to a broker or external trading
account.

### Market data

Use biquote’s public feed for at least EUR/USD, GBP/USD and USD/JPY. Display streaming bid and ask prices, the source,
quote timestamp, quote age, connection state and market state. Use the feed’s historical candles for charts. Never
manufacture or silently extrapolate a market quote. When the connection fails, the market is closed, or an active-
market quote exceeds a configurable age limit of 30 seconds, retain the last displayed price but suspend simulated
fills until a fresh quote arrives.

### Portfolios and spot trading

Allow the user to create and switch between separate paper portfolios, each with its own starting equity, positions,
orders and history. Support long and short spot positions through market and limit buy/sell tickets. Quantities must
have an explicit currency unit. A market buy fills at the current ask and a market sell at the current bid; a limit
order fills only when an incoming quote reaches its price, using the available bid or ask. Show pending, filled,
cancelled and rejected orders, with the quote and timestamp used for every fill. Keep an enduring blotter so a later
price update never rewrites an earlier execution.

### FX options and delta

Support simulated European vanilla calls and puts on the displayed currency pairs. Each position has an explicit
notional, strike, expiry, volatility assumption and domestic and foreign interest-rate assumptions. Calculate
theoretical premium and Greeks with the Garman–Kohlhagen model. Show position delta and aggregate signed delta by
currency pair with clearly stated units. Let the user place a simulated spot hedge from the risk view and see the
resulting delta and portfolio exposure change.

There is no live options quote feed in this version. Option prices and simulated option fills must be labelled as
model-derived, with any assumed execution spread visible. The interface must never present a theoretical premium as an
observed market bid or ask.

### Analysis and results

Provide live price charts with selectable timeframes and clearly identified SMA, EMA, RSI and MACD indicators
calculated from the feed’s candles. Keep indicators separate from actual orders: a displayed signal must not silently
execute a trade.

Show realized and unrealized P&L, equity over time, currency exposure, spread cost and results by pair and position.
Distinguish spot P&L based on received bid/ask from option P&L based on model marks. Convert results into the
portfolio’s reporting currency with a visible conversion rate and timestamp. A final analysis view should let the user
inspect the trade sequence, cumulative P&L and the contribution of spot positions, options and hedges.

### Usability and continuity

The principal views should keep the market watch, chart, order ticket, blotter, positions, risk and P&L easy to follow
during a live demonstration. Persist paper portfolios locally across restarts. On first launch, show the instruments
and live market state while leaving the trading record empty, so the user can build and explain a portfolio from the
first simulated trade.

## Outcome

Status: Completed.

## Judgment

The Security report is **accepted**. The methodology is rigorous and the conclusion is credible. Security verified the mid-bounds invariant through code inspection (`quoteStore.js:45-50`), full check-suite execution (255 checks, all passing), and fresh independent adversarial probes (17/17 passing). The evidence covers the exact degenerate cases the Auditor identified: zero mid, negative mid, mid outside [bid, ask], and the specific EUR→USD → USD→EUR case that produced Infinity. Security confirmed that each hostile quote is dropped at the single ingest chokepoint and that the last-known-good quote persists, leaving the fill gate responsive.

**Finding Dispositions**

All in-scope findings are now verified closed:

- **S05-F1 (cash-only funding blocks longs):** verified_closed by Reviewer multiple rounds + Auditor
- **S05-F2 through S05-F6:** verified_closed by Reviewer
- **G-verify-F7 (risk-reducing trades rejected):** verified_closed by Reviewer
- **S06-F1 (feed prices not validated):** verified_closed by Security
- **S07-F1 (quote mid not validated):** verified_closed by Security (this turn)

**Completion Status**

The approved plan is fully executed. All 18 planned steps are completed. All correction and verification cycles have closed their findings. The brief's requirements are now satisfied:

- Market data feed validated and resilient to degenerate quotes ✓
- Paper portfolios with spot and options trading ✓
- Delta hedging and risk views ✓
- Charts, indicators, P&L and analysis ✓
- No external trading account access ✓
- All findings closed ✓

The work is ready for closeout.

## Accepted deliverables

- `the project folderREADME.md`
- `the project folderapp/config.js`
- `the project folderapp/domain/analytics.js`
- `the project folderapp/domain/buyingPower.js`
- `the project folderapp/domain/execution.js`
- `the project folderapp/domain/indicators.js`
- `the project folderapp/domain/optionBook.js`
- `the project folderapp/domain/options.js`
- `the project folderapp/domain/portfolio.js`
- `the project folderapp/domain/units.js`
- `the project folderapp/main.js`
- `the project folderapp/marketdata/candles.js`
- `the project folderapp/marketdata/feed.js`
- `the project folderapp/marketdata/quoteStore.js`
- `the project folderapp/ui/analyticsPanel.js`
- `the project folderapp/ui/chartView.js`
- `the project folderapp/ui/marketWatch.js`
- `the project folderapp/ui/optionsPanel.js`
- `the project folderapp/ui/orderTicket.js`
- `the project folderapp/ui/portfolioPanel.js`
- `the project folderapp/ui/riskView.js`
- `the project folderchecks/buying-power-margin.check.mjs`
- `the project folderchecks/feed-cache-recovery.check.mjs`
- `the project folderchecks/indicators-analytics.check.mjs`
- `the project folderchecks/options.check.mjs`
- `the project folderchecks/portfolio-execution.check.mjs`
- `the project folderchecks/quote-lifecycle.check.mjs`
- `the project folderindex.html`
- `the project folderpackage.json`
- `the project folderserver.py`
- `the project folderstyles.css`
- `the project foldervendor/signalr.min.js`

## Plan

See `01-plan.md`.

## Findings

- G-verify-F7-risk-reducing-trades-and-hedges-rejected · Severity: high
  Status: Verified and closed.
- S05-F1-cash-only-funding-blocks-longs · Severity: high
  Status: Verified and closed.
- S05-F2-no-usd-cross-conversion · Severity: medium
  Status: Verified and closed.
- S05-F3-chart-refresh-hits-cache · Severity: medium
  Status: Verified and closed.
- S05-F4-polling-state-stuck-disconnected · Severity: medium
  Status: Verified and closed.
- S05-F5-pnl-not-reconciled-with-equity · Severity: low
  Status: Verified and closed.
- S05-F6-expiry-settlement-bypasses-gate · Severity: low
  Status: Verified and closed.
- S06-F1-feed-prices-not-validated · Severity: medium
  Status: Verified and closed.
- S07-F1-quote-mid-not-validated · Severity: medium
  Status: Verified and closed.
