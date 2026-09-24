// Deterministic checks for S04: indicator math (SMA, EMA, RSI, MACD) pinned to
// an independent Python reference, P&L analytics (realized/unrealized, spread
// cost, per-pair and per-group contributions), reporting-currency conversion
// with disclosure and refusal, equity computation, and equity-over-time
// sampling. Synthetic ticks stand in for the feed; storage is a fake with the
// localStorage interface. No network. Run:
//   node checks/indicators-analytics.check.mjs

import { QuoteStore } from "../app/marketdata/quoteStore.js";
import { PortfolioStore } from "../app/domain/portfolio.js";
import { ExecutionEngine } from "../app/domain/execution.js";
import { OptionBook } from "../app/domain/optionBook.js";
import { smaSeries, emaSeries, rsiSeries, macdSeries } from "../app/domain/indicators.js";
import {
  fillGroup, fillSpreadCost, spotUnrealized, computeAnalytics, computeEquity, maybeRecordEquity,
} from "../app/domain/analytics.js";

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

function rig({ storage = null, equity = 100_000, currency = "USD", now = NOW } = {}) {
  const quotes = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  quotes.setConnectionState("streaming");
  quotes.ingest(tick(), now);
  const portfolios = new PortfolioStore({ storage, now: () => now });
  const engine = new ExecutionEngine({ quoteStore: quotes, portfolioStore: portfolios, now: () => now });
  const book = new OptionBook({
    quoteStore: quotes, portfolioStore: portfolios, optionSpreadFraction: SPREAD, now: () => now,
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

// --- indicators --------------------------------------------------------------

// 1. SMA(5) and EMA(5) on a fixed 20-close sequence, pinned to the independent
//    Python reference (SMA-seeded EMA, k = 2/(p+1)).
{
  const A = [1.1000, 1.1010, 1.1005, 1.1020, 1.1030, 1.1025, 1.1040, 1.1035, 1.1050, 1.1045,
    1.1060, 1.1055, 1.1070, 1.1080, 1.1075, 1.1090, 1.1085, 1.1100, 1.1095, 1.1110];
  const sma = smaSeries(A, 5);
  check("SMA pads with null until enough data",
    sma.length === 20 && sma.slice(0, 4).every((v) => v === null));
  near("SMA5 first value", sma[4], 1.1013, 1e-12);
  near("SMA5 last value", sma[19], 1.1096, 1e-12);
  const ema = emaSeries(A, 5);
  near("EMA5 seeds at the SMA", ema[4], 1.1013, 1e-12);
  near("EMA5 sixth value", ema[6], 1.1024666667, 1e-9);
  near("EMA5 last value", ema[19], 1.1095817566, 1e-9);
}

// 2. Wilder RSI(14) on the same sequence; an all-gains path reads exactly 100.
{
  const A = [1.1000, 1.1010, 1.1005, 1.1020, 1.1030, 1.1025, 1.1040, 1.1035, 1.1050, 1.1045,
    1.1060, 1.1055, 1.1070, 1.1080, 1.1075, 1.1090, 1.1085, 1.1100, 1.1095, 1.1110];
  const rsi = rsiSeries(A, 14);
  check("RSI pads with null", rsi.slice(0, 14).every((v) => v === null));
  near("RSI14 first value", rsi[14], 77.77777777777614, 1e-9);
  near("RSI14 last value", rsi[19], 79.169193464576, 1e-9);
  const up = rsiSeries([...Array(20)].map((_, i) => 1.0 + 0.001 * i), 14);
  check("RSI of an all-gains series is 100", up[19] === 100);
  check("RSI refuses insufficient data",
    rsiSeries([1.1, 1.2], 14).every((v) => v === null));
}

// 3. MACD(12,26,9) on a 60-close deterministic sequence, pinned to the Python
//    reference (signal = EMA9 of macd values, SMA-seeded).
{
  const B = [...Array(60)].map((_, i) => 1.1 + 0.0008 * i + 0.004 * Math.sin(i * 0.7));
  const { macd, signal, histogram } = macdSeries(B, 12, 26, 9);
  near("EMA12 last", emaSeries(B, 12)[59], 1.143284916826424, 1e-9);
  near("EMA26 last", emaSeries(B, 26)[59], 1.1374992731595104, 1e-9);
  near("MACD first value (index 25)", macd[25], 0.004849414620189618, 1e-9);
  near("MACD last", macd[59], 0.005785643666913698, 1e-9);
  check("MACD null before slow EMA exists", macd.slice(0, 25).every((v) => v === null));
  near("signal first value (index 33)", signal[33], 0.005331791735289477, 1e-9);
  near("signal last", signal[59], 0.005714066970612964, 1e-9);
  near("histogram last", histogram[59], 7.15766963007335e-05, 1e-9);
  check("histogram null before signal exists", histogram.slice(0, 33).every((v) => v === null));
}

// --- spread cost and grouping ------------------------------------------------

// 4. Spread cost: a spot fill pays half the observed bid/ask spread away from
//    the mid; an option fill pays the assumed model spread; an expiry fill at
//    intrinsic carries none. Fill grouping separates spot, hedges and options.
{
  const { engine, book, pf } = rig();
  const o1 = engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  const o2 = book.placeOptionOrder(callTicket());
  engine.placeOrder({ symbol: "EURUSD", side: "sell", type: "market", qtyAmount: 1_000, origin: "delta-hedge" });
  const [fSpot, fOpt, fHedge] = pf.blotter;
  check("fill groups classify spot / option / hedge",
    fillGroup(fSpot) === "spot" && fillGroup(fOpt) === "option" && fillGroup(fHedge) === "hedge");
  near("spot spread cost = half spread * qty", fillSpreadCost(fSpot), (1.13922 - 1.13919) * 10_000, 1e-9);
  near("option spread cost = assumed spread half * notional",
    fillSpreadCost(fOpt), fOpt.model.modelMid * (SPREAD / 2) * 100_000, 1e-6);
  near("hedge spread cost measured like any spot fill", fillSpreadCost(fHedge), (1.13919 - 1.13916) * 1_000, 1e-9);
  check("orders filled as expected", o1.status === "filled" && o2.status === "filled");
}

// --- analytics: P&L, contributions, trade sequence ---------------------------

// 5. USD-reporting portfolio with a spot buy, a hedge sell and an option open
//    then close: per-pair split, group contributions, totals and the trade
//    sequence's cumulative P&L are exact (same currency, no conversion).
{
  const { engine, book, pf, quotes } = rig();
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  book.placeOptionOrder(callTicket());
  engine.placeOrder({ symbol: "EURUSD", side: "sell", type: "market", qtyAmount: 1_000, origin: "delta-hedge" });
  engine.placeOrder({ symbol: "EURUSD", side: "sell", type: "market", qtyAmount: 4_000 });
  book.closeLot("xl1");

  const a = computeAnalytics(pf, quotes, book, NOW);
  const row = a.perPair.EURUSD;
  near("per-pair realized spot", row.realized.spot, (1.13916 - 1.13922) * 4_000, 1e-9);
  near("per-pair realized hedge", row.realized.hedge, (1.13916 - 1.13922) * 1_000, 1e-9);
  // Option round trip at the same spot realizes exactly the full assumed spread.
  const lot = pf.optionLots[0];
  const optionRealized = (lot.closePricePerUnit - lot.openPricePerUnit) * 100_000;
  near("per-pair realized option (model)", row.realized.option, optionRealized, 1e-6);
  near("contributions split matches per-pair split (USD reporting)",
    a.contributions.spot + a.contributions.hedge + a.contributions.option,
    row.realized.spot + row.realized.hedge + row.realized.option, 1e-9);
  check("all group contributions known", a.contributionKnown.spot && a.contributionKnown.hedge && a.contributionKnown.option);
  check("realized total known and equals sum of groups",
    a.totals.realized.ok
    && Math.abs(a.totals.realized.amount
      - (a.contributions.spot + a.contributions.hedge + a.contributions.option)) < 1e-9);

  // Unrealized spot on the remaining long 5,000 EUR at the exit side (bid).
  const u = spotUnrealized("EURUSD", pf.positions.EURUSD, quotes);
  near("unrealized spot at bid for the remaining long", u.amount, (1.13916 - pf.positions.EURUSD.avgPrice) * 5_000, 1e-9);
  near("unrealized spot total matches", a.totals.unrealizedSpot.amount, u.amount, 1e-9);
  check("no open lots left: option unrealized is zero and known",
    a.totals.unrealizedOptions.ok && a.totals.unrealizedOptions.amount === 0);

  // Trade sequence: 5 fills, cumulative equals running realized sum.
  check("trade sequence lists every fill in order", a.tradeSequence.length === 5
    && a.tradeSequence[0].fillId === "f1" && a.tradeSequence[4].fillId === "f5");
  const lastCum = a.tradeSequence[4].cumulativeReporting;
  near("cumulative P&L at the last fill equals realized total", lastCum, a.totals.realized.amount, 1e-9);
  check("option fills are labelled model-derived in the sequence",
    a.tradeSequence[1].modelDerived === true && a.tradeSequence[4].modelDerived === true
    && a.tradeSequence[0].modelDerived === false);

  // Spread cost total: both spot sells + the buy paid half spread each way;
  // the option paid the full assumed spread across open and close.
  const expectSpread = (1.13922 - 1.13919) * 10_000 + (1.13919 - 1.13916) * 1_000
    + (1.13919 - 1.13916) * 4_000
    + (lot.openPricePerUnit - pf.blotter[1].model.modelMid) * 100_000
    + (pf.blotter[4].model.modelMid - lot.closePricePerUnit) * 100_000;
  near("spread cost total aggregates every fill", a.totals.spreadCost.amount, expectSpread, 1e-6);
}

// 6. JPY-reporting portfolio: USD P&L is converted at the observed USDJPY mid
//    with a disclosure; a currency with no conversion pair is reported
//    unavailable, never estimated.
{
  const quotes = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  quotes.setConnectionState("streaming");
  quotes.ingest(tick(), NOW);
  quotes.ingest(tick({
    symbol: "USDJPY", bid: 148.48, ask: 148.52, mid: 148.50, spread: 0.04,
  }), NOW);
  const portfolios = new PortfolioStore({ storage: null, now: () => NOW });
  const engine = new ExecutionEngine({ quoteStore: quotes, portfolioStore: portfolios, now: () => NOW });
  const book = new OptionBook({
    quoteStore: quotes, portfolioStore: portfolios, optionSpreadFraction: SPREAD, now: () => NOW,
  });
  const pf = portfolios.create({ name: "Yen", startingEquityAmount: 1_000_000, reportingCurrency: "JPY" });
  // Fund the USD wallet so the EURUSD round trip stays within buying power
  // (1M JPY of equity would not cover borrowing the full USD cost); the
  // conversion under test is USD P&L → JPY.
  portfolios.update(pf.id, (doc) => { doc.cash.USD = 20_000; });
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  engine.placeOrder({ symbol: "EURUSD", side: "sell", type: "market", qtyAmount: 10_000 });

  const a = computeAnalytics(pf, quotes, book, NOW);
  const realizedUsd = (1.13916 - 1.13922) * 10_000; // -0.60 USD
  near("realized converted to JPY at the observed mid",
    a.totals.realized.amount, realizedUsd * 148.50, 1e-6);
  check("conversion disclosure names pair, rate and timestamp",
    a.conversionDisclosures.some((d) => d.includes("USDJPY mid = 148.500") && d.includes("2026-09-24T11:59:59")));
  check("cash exposure shows converted balances",
    a.cashExposure.length === 2 && a.cashExposure.every((c) => c.reporting !== null));

  // A currency with no feed pair cannot be converted: equity is unavailable.
  portfolios.update(pf.id, (doc) => { doc.cash.GBP = 500; });
  const eq = computeEquity(pf, quotes, book, NOW);
  check("equity refuses a currency with no conversion pair",
    !eq.ok && eq.missing.some((m) => String(m).includes("GBP")));
  const a2 = computeAnalytics(pf, quotes, book, NOW);
  check("exposure marks the unconvertible balance instead of estimating",
    a2.cashExposure.find((c) => c.currency === "GBP").reporting === null);
}

// --- equity and equity-over-time sampling ------------------------------------

// 7. Equity = converted cash + model value of open option lots. With all
//    conversions at mid, equity equals starting equity minus the spread cost
//    paid so far — an exact invariant of the cash model.
{
  const { engine, book, pf, quotes } = rig();
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  book.placeOptionOrder(callTicket());
  const eq = computeEquity(pf, quotes, book, NOW);
  check("equity computed in reporting currency", eq.ok && eq.equity.currency === "USD");
  const a = computeAnalytics(pf, quotes, book, NOW);
  near("equity = starting equity - spread cost", eq.equity.amount,
    100_000 - a.totals.spreadCost.amount, 1e-6);

  // maybeRecordEquity: first sample, skip within the minute, sample on blotter
  // change, skip when unconvertible, persistence across reopen, thinning.
  const storage = fakeStorage();
  const r2 = rig({ storage });
  const s1 = maybeRecordEquity(r2.portfolios, r2.quotes, r2.book, NOW);
  check("first equity sample recorded", s1 !== null && s1.amount === 100_000 && s1.currency === "USD");
  const s2 = maybeRecordEquity(r2.portfolios, r2.quotes, r2.book, NOW + 1_000);
  check("no second sample within the minute without a fill", s2 === null
    && r2.pf.equityHistory.length === 1);
  r2.engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  const s3 = maybeRecordEquity(r2.portfolios, r2.quotes, r2.book, NOW + 2_000);
  check("blotter change forces a sample immediately", s3 !== null
    && r2.pf.equityHistory.length === 2 && s3.blotterLength === 1);
  near("sample after the buy reflects the spread paid", s3.amount, 100_000 - 0.30, 1e-9);
  const s4 = maybeRecordEquity(r2.portfolios, r2.quotes, r2.book, NOW + 62_000);
  check("sample after the interval even without a fill", s4 !== null
    && r2.pf.equityHistory.length === 3);

  r2.portfolios.update(r2.pf.id, (doc) => { doc.cash.GBP = 100; });
  const s5 = maybeRecordEquity(r2.portfolios, r2.quotes, r2.book, NOW + 122_000);
  check("unconvertible equity is skipped, never estimated", s5 === null
    && r2.pf.equityHistory.length === 3);

  const reopened = new PortfolioStore({ storage, now: () => NOW });
  check("equity history persists across reopen",
    reopened.active().equityHistory.length === 3);

  // Thinning keeps the curve bounded and preserves the latest samples.
  const r3 = rig({});
  for (let i = 0; i < 6; i++) {
    maybeRecordEquity(r3.portfolios, r3.quotes, r3.book, NOW + i * 61_000, { maxSamples: 4 });
  }
  check("equity history is thinned at the cap, newest samples kept",
    r3.pf.equityHistory.length <= 4
    && r3.pf.equityHistory[r3.pf.equityHistory.length - 1].t === NOW + 5 * 61_000);
}

// 8. Empty portfolio: analytics are well-defined zeroes, not errors.
{
  const { pf, quotes, book } = rig();
  const a = computeAnalytics(pf, quotes, book, NOW);
  check("empty portfolio: no fills, totals known and zero",
    a.tradeSequence.length === 0 && a.totals.realized.ok && a.totals.realized.amount === 0
    && a.totals.spreadCost.ok && a.totals.spreadCost.amount === 0);
  const eq = computeEquity(pf, quotes, book, NOW);
  check("empty portfolio equity is the starting equity", eq.ok && eq.equity.amount === 100_000);
}

// 9. F5: the reconciliation line ties the equity change to the P&L cards. An
//    open spot position carries a half-spread adjustment (equity at mid vs
//    unrealized spot at the exit side); a flat book reconciles exactly.
{
  const { engine, pf, quotes, book } = rig();
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  const a = computeAnalytics(pf, quotes, book, NOW);
  check("reconciliation computed", a.reconciliation.ok);
  near("equity change = -half spread", a.reconciliation.equityChange, -0.30, 1e-9);
  near("P&L sum = unrealized spot at exit side", a.reconciliation.pnlSum, -0.60, 1e-9);
  near("adjustment is the retained half-spread", a.reconciliation.adjustment, 0.30, 1e-9);
  near("equity change = P&L sum + adjustment",
    a.reconciliation.pnlSum + a.reconciliation.adjustment, a.reconciliation.equityChange, 1e-12);

  engine.placeOrder({ symbol: "EURUSD", side: "sell", type: "market", qtyAmount: 10_000 });
  const flat = computeAnalytics(pf, quotes, book, NOW);
  near("flat book reconciles exactly", flat.reconciliation.adjustment, 0, 1e-9);
}

// 10. F2: an EUR-reporting portfolio trading GBPUSD gets equity and converted
//     figures through the USD cross — not a permanent "unavailable".
{
  const quotes = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  quotes.setConnectionState("streaming");
  quotes.ingest(tick(), NOW); // EURUSD 1.13919
  quotes.ingest(tick({
    symbol: "GBPUSD", bid: 1.27294, ask: 1.27306, mid: 1.27300, spread: 0.00012,
  }), NOW);
  const portfolios = new PortfolioStore({ storage: null, now: () => NOW });
  const engine = new ExecutionEngine({ quoteStore: quotes, portfolioStore: portfolios, now: () => NOW });
  const book = new OptionBook({
    quoteStore: quotes, portfolioStore: portfolios, optionSpreadFraction: SPREAD, now: () => NOW,
  });
  const pf = portfolios.create({ name: "Euro", startingEquityAmount: 100_000, reportingCurrency: "EUR" });
  const o = engine.placeOrder({ symbol: "GBPUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  check("EUR portfolio can trade GBPUSD", o.status === "filled");
  const eq = computeEquity(pf, quotes, book, NOW);
  check("equity available for EUR reporting via USD cross", eq.ok
    && eq.equity.currency === "EUR" && Math.abs(eq.equity.amount - 100_000) < 1);
  const a = computeAnalytics(pf, quotes, book, NOW);
  check("both cross legs are disclosed",
    a.conversionDisclosures.some((d) => d.includes("EURUSD mid"))
    && a.conversionDisclosures.some((d) => d.includes("GBPUSD mid")));
}

console.log(failures === 0 ? "\nAll checks passed." : `\n${failures} check(s) FAILED.`);
process.exit(failures === 0 ? 0 : 1);
