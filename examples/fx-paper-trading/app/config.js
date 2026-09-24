// Central configuration for the FX paper trading terminal.
// Every tunable that affects market-data freshness or trading behaviour lives here.

export const CONFIG = {
  // Instruments streamed from the biquote public feed.
  symbols: ["EURUSD", "GBPUSD", "USDJPY"],

  // Fill-suspension rule: an active-market quote older than this many seconds
  // blocks simulated fills. Configurable per the brief; default 30s.
  maxQuoteAgeSeconds: 30,

  // biquote endpoints (public, no API key).
  apiBase: "https://biquote.io",
  hubUrl: "https://biquote.io/hubs/tick",

  // REST polling fallback when the SignalR stream cannot be established.
  pollIntervalMs: 2000,
  pollBatch: true, // /api/latest batched per docs (repeat ?symbols=)

  // Chart timeframes supported by the feed's OHLC endpoint.
  candleIntervals: ["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
  defaultCandleInterval: "1h",
  defaultCandleLimit: 200,
  // Candle cache expiry: older entries are refetched, so the chart's minute
  // refresh and symbol switches never freeze on first-load bars.
  candleCacheMaxAgeMs: 60_000,

  // Default reporting currency for new portfolios (conversion inputs in
  // app/domain/units.js use live quotes only — never manufactured rates).
  defaultReportingCurrency: "USD",
  defaultStartingEquity: 100000,

  // S03 — simulated European options (Garman–Kohlhagen). All of these are
  // user-editable assumptions on the ticket, never observed market data:
  // there is no options quote feed in this version.
  options: {
    defaultVolatility: 0.10,   // annualized, 10%
    defaultExpiryDays: 30,
    // Illustrative rate assumptions (continuously compounded, per year);
    // domestic = pair quote currency, foreign = pair base currency.
    defaultRates: { EUR: 0.0200, USD: 0.0350, GBP: 0.0375, JPY: 0.0050 },
    // Assumed execution spread: total model bid/ask distance, as a fraction
    // of the theoretical premium. A buy pays mid + half, a sell receives
    // mid - half; the spread is shown on the ticket and on every fill.
    spreadFraction: 0.02,
    // Margin carried against every open SHORT option lot, as a fraction of
    // its notional (valued in the portfolio's reporting currency at observed
    // mids). Selling an option only receives premium, so borrowing alone
    // would never bound it; the margin makes short option sales consume
    // buying power like any other risk.
    marginFraction: 0.10,
  },
};
