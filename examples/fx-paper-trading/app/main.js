// Application bootstrap for the terminal: wires the biquote feed into the
// quote store, renders market watch, chart, tickets, options, risk and the
// P&L analysis, and records equity samples for the equity-over-time curve.

import { CONFIG } from "./config.js";
import { BiquoteFeed } from "./marketdata/feed.js";
import { QuoteStore } from "./marketdata/quoteStore.js";
import { CandleStore } from "./marketdata/candles.js";
import { MarketWatch, renderConnectionState } from "./ui/marketWatch.js";
import { CURRENCIES, money, convertMoney, conversionDisclosure, formatMoney } from "./domain/units.js";
import { PortfolioStore } from "./domain/portfolio.js";
import { ExecutionEngine } from "./domain/execution.js";
import { OptionBook } from "./domain/optionBook.js";
import { maybeRecordEquity, computeEquity } from "./domain/analytics.js";
import { OrderTicket } from "./ui/orderTicket.js";
import { PortfolioPanel } from "./ui/portfolioPanel.js";
import { OptionsPanel } from "./ui/optionsPanel.js";
import { RiskView } from "./ui/riskView.js";
import { ChartView } from "./ui/chartView.js";
import { AnalyticsPanel } from "./ui/analyticsPanel.js";

const quoteStore = new QuoteStore({ maxQuoteAgeSeconds: CONFIG.maxQuoteAgeSeconds });
const candleStore = new CandleStore(CONFIG);

// S02: portfolios, execution and the blotter, persisted in localStorage.
const portfolioStore = new PortfolioStore({ storage: globalThis.localStorage ?? null });
// Buying power is measured against full equity (cash + model value of open
// option lots); the closures are resolved at fill time, after optionBook
// exists. netDeltaOf lets the shared rule admit delta-reducing fills (hedges,
// partial closes) even when they borrow.
let optionBook = null;
const equityOf = (pf, nowMs) => computeEquity(pf, quoteStore, optionBook, nowMs);
const netDeltaOf = (pf, symbol) => {
  const agg = optionBook?.aggregateDelta(pf)[symbol];
  return agg && agg.unpricedLots === 0 ? { ok: true, netDeltaBase: agg.netDeltaBase } : { ok: false };
};
const engine = new ExecutionEngine({
  quoteStore, portfolioStore, equityOf,
  optionMarginFraction: CONFIG.options.marginFraction, netDeltaOf,
});
// S03: simulated European options (Garman–Kohlhagen, model-derived prices).
optionBook = new OptionBook({
  quoteStore, portfolioStore, optionSpreadFraction: CONFIG.options.spreadFraction,
  optionMarginFraction: CONFIG.options.marginFraction, equityOf,
});

const connEl = document.getElementById("connection-state");
const watch = new MarketWatch(document.getElementById("watch-grid"), quoteStore, CONFIG.symbols);
const chart = new ChartView(document.getElementById("chart-view"), {
  candleStore, symbols: CONFIG.symbols,
  intervals: CONFIG.candleIntervals, defaultInterval: CONFIG.defaultCandleInterval,
});

let panel = null;
let ticket = null;
let optionsPanel = null;
let riskView = null;
let analytics = null;
const renderTrading = () => {
  panel?.render();
  ticket?.renderGate();
  optionsPanel?.render();
  riskView?.render();
  analytics?.render();
};
panel = new PortfolioPanel(
  {
    bar: document.getElementById("portfolio-bar"),
    positions: document.getElementById("positions-table"),
    orders: document.getElementById("orders-table"),
    blotter: document.getElementById("blotter-table"),
  },
  {
    portfolioStore, engine, quoteStore,
    defaultReportingCurrency: CONFIG.defaultReportingCurrency,
    defaultStartingEquity: CONFIG.defaultStartingEquity,
    onChanged: renderTrading,
  },
);
ticket = new OrderTicket(document.getElementById("order-ticket"), {
  engine, quoteStore, symbols: CONFIG.symbols, onChanged: renderTrading,
});
optionsPanel = new OptionsPanel(
  {
    ticket: document.getElementById("option-ticket"),
    lots: document.getElementById("option-lots-table"),
    orders: document.getElementById("option-orders-table"),
  },
  {
    book: optionBook, quoteStore, symbols: CONFIG.symbols,
    optionDefaults: CONFIG.options, onChanged: renderTrading,
  },
);
riskView = new RiskView(document.getElementById("risk-view"), {
  book: optionBook, engine, portfolioStore, quoteStore, onChanged: renderTrading,
});
analytics = new AnalyticsPanel(document.getElementById("analysis-panel"), {
  portfolioStore, quoteStore, book: optionBook,
});

const feed = new BiquoteFeed({
  symbols: CONFIG.symbols,
  callbacks: {
    onTick: (raw) => {
      let symbol = null;
      try {
        symbol = quoteStore.ingest(raw).symbol;
      } catch (err) {
        console.warn("dropped malformed tick", err);
      }
      // Incoming quotes work resting limit orders across all portfolios.
      // Guards: a single bad state must never abort the render chain.
      try {
        if (symbol && engine.processSymbol(symbol).length) renderTrading();
      } catch (err) {
        console.warn("order processing failed on tick", err);
      }
      // Expired option lots settle at intrinsic on the last known quote.
      try {
        if (optionBook.settleExpired().length) renderTrading();
      } catch (err) {
        console.warn("expiry settlement failed on tick", err);
      }
      watch.render();
      renderConversion();
      renderTrading();
    },
    onConnection: (state, detail) => {
      quoteStore.setConnectionState(state);
      renderConnectionState(connEl, state, detail);
      watch.render();
      renderTrading();
    },
  },
});

async function boot() {
  // Immediate snapshot so the first paint shows last known prices (including
  // marketState: closed on weekends) before the stream connects.
  try { await feed.snapshot(); } catch (err) {
    console.warn("initial snapshot failed", err);
  }
  watch.render();
  await feed.start();
  await chart.load();
}

// Quote age must tick even when no new quote arrives (that is exactly when it
// matters), so re-render on a 1s cadence independent of feed events. Expiry
// settlement is also checked here so a lot expiring between ticks still
// settles. Equity sampling rides the same cadence: a sample is written on
// every blotter change and at most once a minute otherwise.
setInterval(() => {
  try {
    optionBook.settleExpired();
  } catch (err) {
    console.warn("expiry settlement failed on cadence", err);
  }
  maybeRecordEquity(portfolioStore, quoteStore, optionBook);
  watch.render();
  renderConversion();
  renderTrading();
}, 1000);

// Refresh the visible chart's candles once a minute so the chart stays live
// without hammering the feed's OHLC endpoint. Forced: the candle cache also
// expires after candleCacheMaxAgeMs, so neither path can freeze on stale bars.
setInterval(() => { chart.load(true); }, 60_000);

addEventListener("resize", () => { chart.draw(); analytics.render(); });

// --- conversion input --------------------------------------------------------

const fromSel = document.getElementById("conv-from");
const toSel = document.getElementById("conv-to");
for (const c of CURRENCIES) {
  fromSel.appendChild(new Option(c, c));
  toSel.appendChild(new Option(c, c, c === CONFIG.defaultReportingCurrency, c === CONFIG.defaultReportingCurrency));
}
document.getElementById("conv-amount").addEventListener("input", renderConversion);
fromSel.addEventListener("change", renderConversion);
toSel.addEventListener("change", renderConversion);

function renderConversion() {
  const resultEl = document.getElementById("conv-result");
  const disclosureEl = document.getElementById("conv-disclosure");
  const amount = Number(document.getElementById("conv-amount").value);
  if (!Number.isFinite(amount)) { resultEl.textContent = "—"; return; }
  const conv = convertMoney(money(amount, fromSel.value), toSel.value, quoteStore);
  if (!conv.ok) {
    resultEl.textContent = "conversion unavailable";
    disclosureEl.textContent = conv.neededSymbol
      ? `No quote received yet for ${conv.neededSymbol}; no rate will be assumed.`
      : `No conversion path (${conv.reason}); no rate will be assumed.`;
    return;
  }
  resultEl.textContent = `${formatMoney(money(amount, fromSel.value))} → ${formatMoney(conv.result)}`;
  disclosureEl.textContent = conversionDisclosure(conv, quoteStore);
}

boot();
