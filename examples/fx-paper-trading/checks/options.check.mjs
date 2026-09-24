// Deterministic checks for S03: Garman–Kohlhagen pricing and Greeks, model-
// derived option fills with the assumed execution spread, delta aggregation,
// risk-view spot hedging, expiry settlement and persistence. Synthetic ticks
// stand in for the feed; storage is a fake with the localStorage interface.
// Reference premiums/Greeks were computed with an independent erf-based
// implementation (Python math.erf). No network. Run:
//   node checks/options.check.mjs

import { QuoteStore } from "../app/marketdata/quoteStore.js";
import { PortfolioStore, PORTFOLIO_SCHEMA_VERSION } from "../app/domain/portfolio.js";
import { ExecutionEngine } from "../app/domain/execution.js";
import { OptionBook, YEAR_MS } from "../app/domain/optionBook.js";
import { garmanKohlhagen, normCdf } from "../app/domain/options.js";

const NOW = Date.parse("2026-09-24T12:00:00Z");
const SPREAD = 0.02;
let failures = 0;

function check(name, cond) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
  if (!cond) failures++;
}

function near(name, got, want, tol) {
  check(`${name} (got ${got}, want ${want})`, Math.abs(got - want) <= tol);
}

function fakeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
}

function tick(overrides = {}) {
  return {
    symbol: "EURUSD",
    bid: 1.13916, ask: 1.13922, mid: 1.13919, spread: 0.00006,
    timestamp: "2026-09-24T11:59:59Z",
    source: "MetaTrader 5 (Broker 1)",
    marketState: "open", stale: false, quoteAgeSeconds: 1,
    direction: "FLAT", high: 1.14, low: 1.13,
    ...overrides,
  };
}

const ASSUME = { volatility: 0.10, rateDomestic: 0.035, rateForeign: 0.02 };
const EXPIRY = NOW + 30 * 86400_000;

function rig({ storage = null, equity = 100_000, currency = "USD" } = {}) {
  const quotes = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  quotes.setConnectionState("streaming");
  quotes.ingest(tick(), NOW);
  const portfolios = new PortfolioStore({ storage, now: () => NOW });
  const engine = new ExecutionEngine({ quoteStore: quotes, portfolioStore: portfolios, now: () => NOW });
  const book = new OptionBook({
    quoteStore: quotes, portfolioStore: portfolios, optionSpreadFraction: SPREAD, now: () => NOW,
  });
  const pf = portfolios.create({ name: "Alpha", startingEquityAmount: equity, reportingCurrency: currency });
  return { quotes, portfolios, engine, book, pf };
}

function callTicket(overrides = {}) {
  return {
    symbol: "EURUSD", cp: "call", side: "buy", notionalAmount: 100_000,
    strike: 1.14, expiryMs: EXPIRY, ...ASSUME, ...overrides,
  };
}

// --- model -----------------------------------------------------------------

// 1. normCdf landmarks and symmetry.
{
  near("normCdf(0) = 0.5", normCdf(0), 0.5, 1e-7);
  near("normCdf(1.96) ≈ 0.9750021", normCdf(1.96), 0.9750021048517795, 1e-6);
  near("normCdf(-x) = 1 - normCdf(x)", normCdf(-1.2345), 1 - normCdf(1.2345), 1e-12);
}

// 2. GK premium and Greeks against the independent erf-based reference:
//    EURUSD call, S=1.13919, K=1.14, T=30/365, σ=0.10, rd=0.035, rf=0.02.
{
  const gk = garmanKohlhagen({
    cp: "call", spot: 1.13919, strike: 1.14, yearsToExpiry: 30 / 365, ...ASSUME,
  });
  near("reference call premium", gk.price, 0.013303014231, 1e-6);
  near("reference call delta", gk.delta, 0.512139006819, 1e-6);
  near("reference call gamma", gk.gamma, 12.188655165969, 1e-4);
  near("reference call vega (per 1.00 vol)", gk.vega, 0.130009925279, 1e-5);
  near("reference call theta (per year)", gk.theta, -0.087375120241, 1e-5);
}

// 3. GK put reference (same parameters) and put-call parity.
{
  const put = garmanKohlhagen({
    cp: "put", spot: 1.13919, strike: 1.14, yearsToExpiry: 30 / 365, ...ASSUME,
  });
  near("reference put premium", put.price, 0.012709377458, 1e-6);
  near("reference put delta", put.delta, -0.486218507922, 1e-6);
  const call = garmanKohlhagen({
    cp: "call", spot: 1.13919, strike: 1.14, yearsToExpiry: 30 / 365, ...ASSUME,
  });
  const T = 30 / 365;
  const parity = 1.13919 * Math.exp(-ASSUME.rateForeign * T) - 1.14 * Math.exp(-ASSUME.rateDomestic * T);
  near("put-call parity C - P = S·e^{-rfT} - K·e^{-rdT}", call.price - put.price, parity, 1e-6);
}

// 4. JPY-quoted reference: USDJPY put, S=148.5, K=150, T=90/365, σ=0.12,
//    rd=0.005 (JPY domestic), rf=0.035 (USD foreign).
{
  const gk = garmanKohlhagen({
    cp: "put", spot: 148.5, strike: 150, yearsToExpiry: 90 / 365,
    volatility: 0.12, rateDomestic: 0.005, rateForeign: 0.035,
  });
  near("reference USDJPY put premium", gk.price, 4.975915060851, 1e-5);
  near("reference USDJPY put delta", gk.delta, -0.598541444610, 1e-6);
}

// 5. Expired option collapses to intrinsic.
{
  const itm = garmanKohlhagen({ cp: "call", spot: 1.20, strike: 1.14, yearsToExpiry: 0, ...ASSUME });
  const otm = garmanKohlhagen({ cp: "call", spot: 1.10, strike: 1.14, yearsToExpiry: 0, ...ASSUME });
  check("expired ITM call is intrinsic with delta 1",
    Math.abs(itm.price - 0.06) < 1e-12 && itm.delta === 1 && itm.expired);
  check("expired OTM call is worthless with delta 0",
    otm.price === 0 && otm.delta === 0 && otm.expired);
}

// --- fills -----------------------------------------------------------------

// 6. Buying a call fills at the model ASK (mid + half spread), pays the
//    premium in quote currency, and the blotter record is model-labelled with
//    the full model inputs and the assumed spread.
{
  const { book, pf } = rig();
  const o = book.placeOptionOrder(callTicket());
  check("option order filled", o.status === "filled" && o.lotId === "xl1");
  const f = pf.blotter[0];
  // The model itself is pinned against the independent reference above; here
  // the module's own mid anchors the spread-application arithmetic.
  const mid = garmanKohlhagen({
    cp: "call", spot: 1.13919, strike: 1.14, yearsToExpiry: 30 / 365, ...ASSUME,
  }).price;
  const ask = mid * (1 + SPREAD / 2);
  near("option fill price is model ask, not mid", f.price, ask, 1e-10);
  near("premium debited in quote currency", pf.cash.USD, 100_000 - ask * 100_000, 1e-6);
  check("fill is labelled model-derived with model inputs",
    f.kind === "option" && f.modelDerived === true
    && f.model.name === "Garman-Kohlhagen"
    && f.model.volatility === 0.10 && f.model.rateDomestic === 0.035 && f.model.rateForeign === 0.02
    && f.model.spreadFraction === SPREAD
    && f.model.modelMid === mid
    && Math.abs(f.model.modelBid - mid * (1 - SPREAD / 2)) < 1e-12);
  check("fill records the observed spot quote used by the model",
    f.quote.mid === 1.13919 && f.quote.source === "MetaTrader 5 (Broker 1)"
    && f.quote.timestampMs === Date.parse("2026-09-24T11:59:59Z"));
  check("option notional carries explicit currency unit",
    f.qty.amount === 100_000 && f.qty.currency === "EUR");
}

// 7. Selling a put opens a short lot and credits the model BID premium.
{
  const { book, pf } = rig();
  const o = book.placeOptionOrder(callTicket({ cp: "put", side: "sell" }));
  const mid = garmanKohlhagen({
    cp: "put", spot: 1.13919, strike: 1.14, yearsToExpiry: 30 / 365, ...ASSUME,
  }).price;
  const bid = mid * (1 - SPREAD / 2);
  const lot = pf.optionLots[0];
  check("short put lot opened", o.status === "filled" && lot.qtySign === -1 && lot.status === "open");
  near("premium credited at model bid", pf.cash.USD, 100_000 + bid * 100_000, 1e-6);
  near("lot open price is model bid", lot.openPricePerUnit, bid, 1e-10);
}

// 8. Gate closed (stale quote): the option order is rejected, recorded, and no
//    lot, cash movement or blotter entry is created.
{
  const { quotes, book, pf } = rig();
  quotes.ingest(tick({ timestamp: "2026-09-24T11:59:15Z" }), NOW); // 45s old
  const o = book.placeOptionOrder(callTicket());
  check("stale quote rejects option order", o.status === "rejected"
    && o.statusReason.includes("quote_stale"));
  check("rejection leaves lots, cash and blotter untouched",
    pf.optionLots.length === 0 && pf.blotter.length === 0 && pf.cash.USD === 100_000
    && pf.optionOrders.length === 1);
}

// 9. Insufficient funds: a premium whose cash effect would borrow far beyond
//    equity is rejected. (Buying power is the shared rule from
//    app/domain/buyingPower.js: total borrowed cash, valued in the reporting
//    currency, must not exceed equity — buys and sells alike.)
{
  const { book, pf } = rig({ equity: 1_000 });
  const o = book.placeOptionOrder(callTicket({ notionalAmount: 10_000_000 }));
  check("unaffordable option buy rejected", o.status === "rejected"
    && o.statusReason.includes("insufficient_funds"));
  check("rejection leaves no lot", pf.optionLots.length === 0 && pf.cash.USD === 1_000);
}

// 10. Marks move with the spot: after an uptick the long call's model mark and
//     unrealized P&L rise by the re-priced amount (all labelled model-derived).
{
  const { quotes, book, pf } = rig();
  book.placeOptionOrder(callTicket());
  const lot = pf.optionLots[0];
  quotes.ingest(tick({ bid: 1.14916, ask: 1.14922, mid: 1.14919 }), NOW);
  const mark = book.markLot(lot, NOW);
  const gk = garmanKohlhagen({
    cp: "call", spot: 1.14919, strike: 1.14, yearsToExpiry: 30 / 365, ...ASSUME,
  });
  check("mark is model-derived with the new spot", mark.ok && mark.modelDerived
    && Math.abs(mark.spot - 1.14919) < 1e-12);
  near("mark price equals re-priced GK model", mark.pricePerUnit, gk.price, 1e-9);
  near("unrealized P&L = (mark - open) * notional",
    mark.unrealizedPnlQuote, (gk.price - lot.openPricePerUnit) * 100_000, 1e-4);
  check("no fill happened on re-mark (blotter unchanged)", pf.blotter.length === 1);
}

// 11. Position delta and aggregate signed delta by pair, in base units.
{
  const { book, engine, pf } = rig();
  book.placeOptionOrder(callTicket()); // long call, delta 0.5121 * 100k
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 50_000 });
  const agg = book.aggregateDelta(pf, NOW).EURUSD;
  near("spot delta is the net quantity in base units", agg.spotDeltaBase, 50_000, 1e-9);
  near("options delta is GK delta * notional", agg.optionsDeltaBase, 0.512139006819 * 100_000, 1);
  near("net delta sums spot and options", agg.netDeltaBase, 50_000 + 51_213.9006819, 1);
}

// 12. Risk-view hedge: the suggestion flattens net delta; after it fills via
//     the ordinary spot engine, net delta is ~0 and the fill is tagged.
{
  const { book, engine, pf } = rig();
  book.placeOptionOrder(callTicket());
  const sug = book.hedgeSuggestion(pf, "EURUSD", NOW);
  check("hedge suggestion opposes the net delta",
    sug.side === "sell" && Math.abs(sug.qtyAmount - 51_213.9006819) < 1
    && sug.qtyCurrency === "EUR");
  const o = engine.placeOrder({
    symbol: sug.symbol, side: sug.side, type: "market", qtyAmount: sug.qtyAmount, origin: "delta-hedge",
  });
  check("hedge fills through the spot engine at bid", o.status === "filled"
    && pf.blotter[1].price === 1.13916 && pf.blotter[1].origin === "delta-hedge");
  const after = book.aggregateDelta(pf, NOW).EURUSD;
  check("net delta is flat after the hedge", Math.abs(after.netDeltaBase) < 1e-6);
  check("flat book yields no further hedge suggestion",
    book.hedgeSuggestion(pf, "EURUSD", NOW) === null);
}

// 13. Closing a long lot at the model price realizes P&L against the open
//     premium and returns the exit premium to quote-currency cash.
{
  const { quotes, book, pf } = rig();
  book.placeOptionOrder(callTicket());
  const cashAfterOpen = pf.cash.USD;
  quotes.ingest(tick({ bid: 1.14916, ask: 1.14922, mid: 1.14919 }), NOW);
  const close = book.closeLot("xl1");
  check("close order filled", close.status === "filled" && close.closesLotId === "xl1");
  const lot = pf.optionLots[0];
  const gk = garmanKohlhagen({
    cp: "call", spot: 1.14919, strike: 1.14, yearsToExpiry: 30 / 365, ...ASSUME,
  });
  const exitBid = gk.price * (1 - SPREAD / 2);
  near("close realizes (exit - open) * notional",
    lot.realizedPnlQuote, (exitBid - lot.openPricePerUnit) * 100_000, 1e-4);
  near("exit premium credited to cash", pf.cash.USD, cashAfterOpen + exitBid * 100_000, 1e-4);
  check("lot is closed", lot.status === "closed");
  check("close fill is model-labelled on the blotter",
    pf.blotter[1].kind === "option" && pf.blotter[1].modelDerived === true
    && Math.abs(pf.blotter[1].realizedPnlQuote - lot.realizedPnlQuote) < 1e-9);
}

// 14. Expiry settlement: an ITM call settles at intrinsic against the last
//     known quote; an OTM put expires worthless. Both are model-derived.
//     Settlement obeys the fill-suspension rule, so a fresh tradable quote is
//     ingested at settlement time (as the live feed would have delivered).
{
  const NOW2 = EXPIRY + 3_600_000; // 1h after expiry
  const quotes = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  quotes.setConnectionState("streaming");
  quotes.ingest(tick(), NOW);
  const portfolios = new PortfolioStore({ storage: null, now: () => NOW });
  const book = new OptionBook({
    quoteStore: quotes, portfolioStore: portfolios, optionSpreadFraction: SPREAD, now: () => NOW,
  });
  const pf = portfolios.create({ name: "Alpha", startingEquityAmount: 100_000, reportingCurrency: "USD" });
  book.placeOptionOrder(callTicket({ strike: 1.13 }));                       // ITM at 1.13919
  book.placeOptionOrder(callTicket({ cp: "put", side: "buy", strike: 1.10 })); // OTM
  quotes.ingest(tick({ timestamp: new Date(NOW2 - 1_000).toISOString() }), NOW2); // fresh at settlement
  const settlements = book.settleExpired(NOW2);
  check("both expired lots settle", settlements.length === 2
    && pf.optionLots.every((l) => l.status === "expired"));
  const callFill = settlements.find((f) => f.cp === "call");
  const putFill = settlements.find((f) => f.cp === "put");
  near("ITM call settles at intrinsic (mid - strike)", callFill.price, 1.13919 - 1.13, 1e-9);
  check("expiry fills are labelled model-derived", callFill.modelDerived && putFill.modelDerived
    && callFill.kind === "option_expiry");
  check("OTM put expires worthless", putFill.price === 0 && putFill.premiumTotal.amount === 0);
  const callLot = pf.optionLots[0];
  near("expiry realizes payoff - premium paid",
    callLot.realizedPnlQuote, (1.13919 - 1.13) * 100_000 - callLot.openPricePerUnit * 100_000, 1e-4);
  check("settlement credited intrinsic payoff to cash",
    Math.abs(pf.cash.USD - (100_000
      - pf.optionLots[0].openPricePerUnit * 100_000
      - pf.optionLots[1].openPricePerUnit * 100_000
      + (1.13919 - 1.13) * 100_000)) < 1e-4);
}

// 15. No quote at all: an expired lot is NOT settled against an invented price.
{
  const quotes = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  quotes.setConnectionState("streaming");
  const portfolios = new PortfolioStore({ storage: null, now: () => NOW });
  const pf = portfolios.create({ name: "Alpha", startingEquityAmount: 100_000, reportingCurrency: "USD" });
  pf.optionLots.push({
    lotId: "xl1", symbol: "EURUSD", cp: "call", qtySign: 1,
    notional: { amount: 100_000, currency: "EUR" }, strike: 1.13,
    expiryMs: EXPIRY, ...{ volatility: 0.1, rateDomestic: 0.035, rateForeign: 0.02 },
    openPricePerUnit: 0.01, openModelMid: 0.01, openedAtMs: NOW,
    status: "open", closedAtMs: null, closePricePerUnit: null, realizedPnlQuote: null,
  });
  const book = new OptionBook({
    quoteStore: quotes, portfolioStore: portfolios, optionSpreadFraction: SPREAD, now: () => NOW,
  });
  const settlements = book.settleExpired(EXPIRY + 1000);
  check("expired lot without any quote stays open (no invented price)",
    settlements.length === 0 && pf.optionLots[0].status === "open");
  const mark = book.markLot(pf.optionLots[0], NOW);
  check("mark without quote is refused, not fabricated", !mark.ok && mark.reason === "no_quote");
}

// 16. Persistence: option lots, option orders and blotter survive a reopen,
//     and a v1 (S02) document migrates to v2 without losing the spot record.
{
  const storage = fakeStorage();
  const { book, engine, portfolios } = rig({ storage });
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 1_000 });
  book.placeOptionOrder(callTicket());
  const reopened = new PortfolioStore({ storage, now: () => NOW });
  const pf2 = reopened.active();
  check("option lots and orders persist across reopen",
    pf2.optionLots.length === 1 && pf2.optionOrders.length === 1 && pf2.blotter.length === 2);
  check("stored document is schema v3",
    JSON.parse(storage.getItem("fx-terminal.portfolios.v1")).version === PORTFOLIO_SCHEMA_VERSION
    && PORTFOLIO_SCHEMA_VERSION === 3);

  const v1doc = {
    version: 1, activeId: "pf-1", portfolioSeq: 1,
    portfolios: {
      "pf-1": {
        id: "pf-1", name: "Legacy", reportingCurrency: "USD",
        startingEquity: { amount: 5_000, currency: "USD" }, createdAtMs: NOW,
        cash: { EUR: 0, USD: 5_000, GBP: 0, JPY: 0 },
        positions: {}, orders: [], blotter: [], seq: { order: 0, fill: 0 },
      },
    },
  };
  const legacyStorage = fakeStorage();
  legacyStorage.setItem("fx-terminal.portfolios.v1", JSON.stringify(v1doc));
  const migrated = new PortfolioStore({ storage: legacyStorage, now: () => NOW });
  const legacy = migrated.get("pf-1");
  check("v1 document migrates and keeps its cash and name",
    legacy.cash.USD === 5_000 && legacy.name === "Legacy"
    && Array.isArray(legacy.optionLots) && Array.isArray(legacy.optionOrders)
    && Array.isArray(legacy.equityHistory)
    && legacy.seq.lot === 0 && legacy.seq.optionOrder === 0);
  check("migrated document is saved as v3",
    JSON.parse(legacyStorage.getItem("fx-terminal.portfolios.v1")).version === 3);

  // v2 (S03) documents predate the equity curve: migration adds the field and
  // keeps every option lot and blotter fill.
  const v2doc = JSON.parse(storage.getItem("fx-terminal.portfolios.v1"));
  v2doc.version = 2;
  for (const p of Object.values(v2doc.portfolios)) delete p.equityHistory;
  const v2storage = fakeStorage();
  v2storage.setItem("fx-terminal.portfolios.v1", JSON.stringify(v2doc));
  const migratedV2 = new PortfolioStore({ storage: v2storage, now: () => NOW });
  const pf3 = migratedV2.active();
  check("v2 document migrates keeping lots and blotter",
    Array.isArray(pf3.equityHistory) && pf3.equityHistory.length === 0
    && pf3.optionLots.length === 1 && pf3.blotter.length === 2
    && JSON.parse(v2storage.getItem("fx-terminal.portfolios.v1")).version === 3);
}

// 17. Blotter immutability extends to option fills: later ticks, option trades
//     and settlements never rewrite an earlier execution record.
{
  const { quotes, book, pf } = rig();
  book.placeOptionOrder(callTicket());
  const snapshot = JSON.stringify(pf.blotter[0]);
  quotes.ingest(tick({ bid: 1.2, ask: 1.20006, mid: 1.20003 }), NOW);
  book.placeOptionOrder(callTicket({ cp: "put", side: "sell" }));
  book.closeLot("xl1");
  check("option fill record frozen across later ticks and trades",
    JSON.stringify(pf.blotter[0]) === snapshot && pf.blotter.length === 3);
}

// 18. Portfolio isolation: option lots and deltas do not leak between
//     portfolios.
{
  const { book, portfolios, pf } = rig();
  book.placeOptionOrder(callTicket());
  const beta = portfolios.create({ name: "Beta", startingEquityAmount: 10_000, reportingCurrency: "USD" });
  check("option lot stays in its portfolio",
    pf.optionLots.length === 1 && beta.optionLots.length === 0);
  const aggBeta = book.aggregateDelta(beta, NOW);
  check("other portfolio has no exposure", Object.keys(aggBeta).length === 0);
}

// 19. F6: an expired lot is NOT settled on a stale quote or a down feed — it
//     stays open past expiry and settles on the first fresh, tradable quote,
//     whose timestamp lands on the blotter fill.
{
  const NOW2 = EXPIRY + 3_600_000; // 1h after expiry
  const quotes = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  quotes.setConnectionState("streaming");
  quotes.ingest(tick({ timestamp: "2026-09-24T11:59:59Z" }), NOW);
  const portfolios = new PortfolioStore({ storage: null, now: () => NOW });
  const book = new OptionBook({
    quoteStore: quotes, portfolioStore: portfolios, optionSpreadFraction: SPREAD, now: () => NOW,
  });
  const pf = portfolios.create({ name: "Alpha", startingEquityAmount: 100_000, reportingCurrency: "USD" });
  book.placeOptionOrder(callTicket({ strike: 1.13 })); // ITM call
  const blotterLen = pf.blotter.length;

  // Quote is 30 days old at expiry+1h: suspended, settlement deferred.
  check("stale quote defers expiry settlement", book.settleExpired(NOW2).length === 0
    && pf.optionLots[0].status === "open" && pf.blotter.length === blotterLen);
  // A down feed defers too, even with a fresh quote in hand.
  quotes.ingest(tick({ timestamp: new Date(NOW2 - 1_000).toISOString() }), NOW2);
  quotes.setConnectionState("disconnected");
  check("down feed defers expiry settlement", book.settleExpired(NOW2).length === 0
    && pf.optionLots[0].status === "open");
  // Fresh quote + live feed: settles at intrinsic, fill records the quote used.
  quotes.setConnectionState("streaming");
  const settlements = book.settleExpired(NOW2);
  check("fresh quote settles the expired lot", settlements.length === 1
    && pf.optionLots[0].status === "expired");
  check("settlement fill records the quote it used",
    settlements[0].quote.timestampMs === NOW2 - 1_000
    && Math.abs(settlements[0].price - (1.13919 - 1.13)) < 1e-9);
}

// 20. F1: a USD portfolio can BUY a USDJPY call — the JPY premium is borrowed
//     against equity, not refused for lack of JPY cash.
{
  const quotes = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  quotes.setConnectionState("streaming");
  quotes.ingest(tick({
    symbol: "USDJPY", bid: 148.48, ask: 148.52, mid: 148.50, spread: 0.04,
  }), NOW);
  const portfolios = new PortfolioStore({ storage: null, now: () => NOW });
  const book = new OptionBook({
    quoteStore: quotes, portfolioStore: portfolios, optionSpreadFraction: SPREAD, now: () => NOW,
  });
  const pf = portfolios.create({ name: "Alpha", startingEquityAmount: 100_000, reportingCurrency: "USD" });
  const o = book.placeOptionOrder({
    symbol: "USDJPY", cp: "call", side: "buy", notionalAmount: 10_000,
    strike: 148.50, expiryMs: EXPIRY,
    volatility: 0.10, rateDomestic: 0.005, rateForeign: 0.035,
  });
  check("USD portfolio buys a USDJPY call", o.status === "filled"
    && pf.optionLots.length === 1 && pf.optionLots[0].status === "open");
  check("JPY premium is borrowed, not refused", pf.cash.JPY < 0 && pf.cash.USD === 100_000);
  check("USDJPY option fill is model-labelled", pf.blotter[0].modelDerived === true
    && pf.blotter[0].priceCurrency === "JPY");
}

console.log(failures === 0 ? "\nAll checks passed." : `\n${failures} check(s) FAILED.`);
process.exit(failures === 0 ? 0 : 1);
