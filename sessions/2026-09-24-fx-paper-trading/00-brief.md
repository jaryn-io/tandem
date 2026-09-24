# FX Paper Trading and Options Risk Terminal

Public record `2026-09-24-fx-paper-trading` · session of 2026-09-24 · 90 messages · duration 3h31 · total tokens 1545540

## Team

| Role | Model | Turns |
|---|---|---|
| Auditor | GPT-6 Sol | 1 |
| Orchestrator | Claude Haiku 4.5 | 21 |
| Planner | GPT-6 Luna | 1 |
| Producer | Kimi K3 | 11 |
| Reviewer | Claude Opus 5.5 | 6 |
| Security | Kimi K3 | 3 |

## Brief

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
