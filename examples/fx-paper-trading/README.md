> **About this folder.** This is the application produced by a Tandem session on 24 September 2026, published as delivered. The complete record of that session, every message, is at [`sessions/2026-09-24-fx-paper-trading`](../../sessions/2026-09-24-fx-paper-trading/00-summary.md). It is an internal test deliverable, not client work and not a product: trading is simulated, prices come from a public feed, nothing is ever sent to a broker. Changes made for publication, and nothing else: an MIT licence file added, since the session delivered none; the run path below made relative; three headings that named the session's internal step numbers reworded. Checks: 255/255.
>
> Questions: tandem@jaryn.io

# FX Paper Trading and Options Risk Terminal

A locally runnable FX trading workstation for **simulated** trading against live
market data. No trade is ever sent to a broker or external trading account —
every fill is computed in the browser from feed quotes and stored locally.

## Run it

```bash
cd fx-paper-trading
python3 server.py            # prints the URL, default http://127.0.0.1:8317/
```

If the default port is occupied the next free port is used automatically; open
the URL the server prints.

Open the URL in a browser. Internet access is required for the biquote public
feed (no API key, no signup). The SignalR client is vendored in `vendor/`, so no
CDN is needed at runtime.

## Current state — terminal, chart and analysis

The review corrections, on top of the options layer:

- **Buying power** (`app/domain/buyingPower.js`): one admission rule for both
  sides of every fill, spot and options. A fill may leave cash negative in a
  currency — buying USDJPY from a USD portfolio borrows JPY, a short borrows
  the base currency. The account's requirement is the total borrowed plus a
  margin of `options.marginFraction` (default 10%) of every open short option
  lot's notional, all valued in the reporting currency at observed feed mids
  — so option sales are bounded even though selling only receives premium. A
  fill is admitted when it does not increase the requirement (partial closes
  and buy-backs stay possible over the limit), when it flattens the pair's
  net delta without flipping its sign — crossing flat only within a small
  tolerance, so the exact-flat risk-view hedge is admitted even when it
  borrows against negative equity while an overshooting "hedge" is not — or
  when the post-fill requirement fits within current equity (cash plus the
  model value of open option lots). A fill that opens a new short option lot
  never qualifies for the risk-off or delta-flattening admissions: short
  sales always face the margin/equity test, whatever their effect on delta,
  and the premium a sale receives is never credited against the requirement —
  the account's pre-fill requirement plus the new lot's margin must fit
  within equity, so a deep-in-the-money sale cannot leverage the account by
  repaying borrowing with its own premium. If equity
  or a requirement component cannot be valued from observed quotes, the fill
  is rejected as unprovable rather than allowed on an assumed rate.
- **USD cross-conversion** (`app/domain/units.js`): when no direct pair exists
  (e.g. EUR reporting with GBPUSD trades), conversion crosses through USD on
  two observed legs; both legs, rates and timestamps are disclosed. A missing
  leg refuses the conversion — nothing is estimated.
- **Live chart**: the candle cache expires after `candleCacheMaxAgeMs`
  (default 60s) and the minute timer forces a refetch, so the chart refreshes
  without Reload and symbol switches never serve bars older than the expiry.
- **Polling fallback recovery** (`app/marketdata/feed.js`): a failed poll
  marks the feed "disconnected"; the next successful poll restores "polling",
  so one network blip no longer blocks fills for good.
- **P&L reconciliation** (`app/domain/analytics.js`): the analysis panel shows
  a marking-basis adjustment tying the equity change to the P&L cards —
  equity values cash at the observed mid while unrealized spot P&L is marked
  at the exit side of the bid/ask, so the difference is exactly the
  half-spread still carried on open spot positions.
- **Expiry settlement** (`app/domain/optionBook.js`): settlement writes a
  blotter fill, so it obeys the same fill-suspension rule as every simulated
  fill. An expired lot whose quote is stale, whose market is closed or whose
  feed is down stays open past expiry and settles at intrinsic on the first
  fresh, tradable quote after expiry; the settlement fill records the quote
  it used.

The terminal, chart and analysis layer, on top of the options layer:

- **Chart** (`app/ui/chartView.js`): candlestick chart per pair with selectable
  timeframes (1m–1d) from the feed's OHLC candles, redrawn on selection and
  refreshed once a minute. SMA 20 and EMA 50 overlays, RSI 14 and MACD
  12·26·9 subpanels — all computed by `app/domain/indicators.js` (pure
  functions pinned to an independent Python reference in the checks) from the
  same closes the chart draws. Indicators are display-only: the chart module
  holds no reference to the execution engine, and a displayed signal never
  places or modifies an order.
- **P&L and analysis** (`app/domain/analytics.js`, `app/ui/analyticsPanel.js`):
  equity, realized P&L, unrealized spot P&L (marked at the exit side of the
  observed bid/ask) and unrealized option P&L (Garman–Kohlhagen model marks,
  always labelled model-derived) as separate figures — never merged
  unlabelled. Spread cost aggregates what every fill paid away from the mid
  (half the observed spread for spot, the assumed model spread for options).
  Results are broken down by pair and by contribution group (spot trades,
  delta hedges, options), with currency exposure per cash balance. All
  reporting-currency conversion uses live feed mid quotes with the rate, pair
  and quote timestamp disclosed (crossing through USD on two disclosed
  observed legs when no direct pair exists); a currency whose conversion
  quotes are missing is reported unavailable, never estimated.
- **Equity over time**: equity samples (converted cash plus the model value of
  open option lots — spot positions already live in the multi-currency cash
  ledger) are recorded on every fill and once a minute while the terminal
  runs, persisted with the portfolio (schema v3; v1/v2 documents migrate in
  place) and drawn as a curve against the starting-equity reference line.
- **Trade sequence**: the chronological blotter with per-fill realized P&L,
  the price basis of each execution (observed bid/ask vs model-derived GK) and
  running cumulative P&L in the reporting currency.
- **Terminal layout**: market watch on top; chart; then a two-column grid with
  portfolio/positions/orders/blotter on the left and the order ticket, options
  and risk view on the right; the analysis section spans the full width.

## Checks

Deterministic lifecycle/conversion checks (synthetic ticks, no network):

```bash
node checks/quote-lifecycle.check.mjs
```

Deterministic portfolio/execution checks (fake storage, synthetic ticks):

```bash
node checks/portfolio-execution.check.mjs
```

Deterministic options/Greeks/hedging checks (fake storage, synthetic ticks,
model values pinned to an independent erf-based reference):

```bash
node checks/options.check.mjs
```

Deterministic indicator/analytics checks (SMA/EMA/RSI/MACD pinned to an
independent Python reference; P&L split, spread cost, equity–P&L
reconciliation, USD cross-conversion, equity sampling; fake storage, synthetic
ticks):

```bash
node checks/indicators-analytics.check.mjs
```

Deterministic candle-cache and polling-recovery checks (cache expiry, forced
refresh, poll fail-then-recover; stubbed fetch, no network or timers):

```bash
node checks/feed-cache-recovery.check.mjs
```

Deterministic buying-power admission checks (short-option margin bound,
delta-reducing short sales and overshooting hedges refused beyond equity,
opening short sales refused when the pre-fill requirement plus the new lot's
margin exceeds equity — the incoming premium never counts as buying power,
the sale-then-hedge ratchet stops at the second sale, margin accumulation
bounding repeated sales, partial close while over the limit, risk-view hedge
and short buy-back after an adverse move; fake storage, synthetic ticks):

```bash
node checks/buying-power-margin.check.mjs
```

## Layout

```
index.html            terminal surface (watch, chart, grid: portfolio/positions/orders/blotter | tickets/options/risk, analysis)
server.py             local static server (stdlib only)
app/config.js         symbols, freshness limit, endpoints, portfolio defaults, option assumptions, candle cache expiry
app/domain/units.js   currencies, pairs, Money, conversion (direct, inverse, USD cross with per-leg disclosure)
app/domain/portfolio.js  portfolios, cash ledger, positions, persistence (schema v3, migrates v1/v2)
app/domain/buyingPower.js one admission rule for both sides: borrowed cash + short-option margin bounded by equity; opening short lots always face the equity test with their premium not credited; risk-reducing fills always admitted
app/domain/execution.js  spot order tickets, fill rules, append-only blotter
app/domain/options.js    Garman–Kohlhagen premium and Greeks (pure model)
app/domain/optionBook.js option orders, lots, marking, delta aggregation, hedging, expiry
app/domain/indicators.js SMA, EMA, RSI (Wilder), MACD over candle closes (pure)
app/domain/analytics.js  P&L split, spread cost, exposure, equity, equity sampling (pure)
app/marketdata/       feed client, quote store/lifecycle, candle store
app/ui/               market watch, chart, portfolio panel, order ticket, options panel, risk view, analysis panel
checks/               deterministic node checks
vendor/signalr.min.js vendored SignalR 8.0.0 client
```
