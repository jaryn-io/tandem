// Deterministic checks for S02: portfolios, spot execution rules and the
// enduring blotter. Synthetic ticks stand in for the feed; storage is a fake
// with the localStorage interface. No network. Run:
//   node checks/portfolio-execution.check.mjs

import { QuoteStore } from "../app/marketdata/quoteStore.js";
import { PortfolioStore, PORTFOLIO_SCHEMA_VERSION } from "../app/domain/portfolio.js";
import { ExecutionEngine } from "../app/domain/execution.js";

const NOW = Date.parse("2026-09-24T12:00:00Z");
let failures = 0;

function check(name, cond) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
  if (!cond) failures++;
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

// Rig: quote store streaming a fresh EURUSD quote + store + engine.
function rig({ storage = null, equity = 100_000, currency = "USD" } = {}) {
  const quotes = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  quotes.setConnectionState("streaming");
  quotes.ingest(tick(), NOW);
  const portfolios = new PortfolioStore({ storage, now: () => NOW });
  const engine = new ExecutionEngine({ quoteStore: quotes, portfolioStore: portfolios, now: () => NOW });
  const pf = portfolios.create({ name: "Alpha", startingEquityAmount: equity, reportingCurrency: currency });
  return { quotes, portfolios, engine, pf };
}

// 1. First launch: no stored document -> no portfolios, empty record.
{
  const s = new PortfolioStore({ storage: fakeStorage(), now: () => NOW });
  check("first launch has no portfolios", s.list().length === 0 && s.active() === null);
}

// 2. Create: starting equity lands in the reporting currency cash ledger.
{
  const { pf } = rig();
  check("starting equity deposited in reporting currency",
    pf.cash.USD === 100_000 && pf.startingEquity.currency === "USD");
  check("new portfolio has empty positions/orders/blotter",
    pf.orders.length === 0 && pf.blotter.length === 0
    && Object.keys(pf.positions).length === 0);
}

// 3. Persistence round-trip: a store re-opened on the same storage sees the
//    portfolio, its active selection and its starting equity.
{
  const storage = fakeStorage();
  const { portfolios } = rig({ storage });
  portfolios.create({ name: "Beta", startingEquityAmount: 50_000, reportingCurrency: "EUR" });
  const reopened = new PortfolioStore({ storage, now: () => NOW });
  check("portfolios persist across reopen", reopened.list().length === 2);
  check("active selection persists", reopened.active()?.name === "Beta");
  check("stored document is schema-versioned",
    JSON.parse(storage.getItem("fx-terminal.portfolios.v1")).version === PORTFOLIO_SCHEMA_VERSION);
}

// 4. Market buy fills at the current ASK, records the quote and moves cash.
{
  const { engine, pf } = rig();
  const o = engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  const f = pf.blotter[0];
  check("market buy status filled", o.status === "filled" && o.fillId === f.fillId);
  check("market buy fills at ask", f.price === 1.13922);
  check("fill quantity carries explicit currency unit",
    f.qty.amount === 10_000 && f.qty.currency === "EUR");
  check("fill records the quote used (bid/ask/source/timestamp)",
    f.quote.bid === 1.13916 && f.quote.ask === 1.13922
    && f.quote.source === "MetaTrader 5 (Broker 1)"
    && f.quote.timestampMs === Date.parse("2026-09-24T11:59:59Z"));
  check("cash moves in both currencies",
    pf.cash.EUR === 10_000 && Math.abs(pf.cash.USD - (100_000 - 10_000 * 1.13922)) < 1e-6);
  check("long position opened at ask", pf.positions.EURUSD.qtyBase === 10_000
    && pf.positions.EURUSD.avgPrice === 1.13922);
}

// 5. Market sell fills at the current BID; closing a long realizes P&L.
{
  const { engine, pf } = rig();
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  const o = engine.placeOrder({ symbol: "EURUSD", side: "sell", type: "market", qtyAmount: 10_000 });
  const f = pf.blotter[1];
  check("market sell fills at bid", o.status === "filled" && f.price === 1.13916);
  check("close realizes (bid - ask) * qty in quote currency",
    Math.abs(f.realizedPnlQuote - (1.13916 - 1.13922) * 10_000) < 1e-6);
  check("flat position is retained with its realized P&L",
    pf.positions.EURUSD.qtyBase === 0
    && Math.abs(pf.positions.EURUSD.realizedPnlQuote - (-0.6)) < 1e-6);
}

// 6. Short: selling without a position opens a short at bid; buying back
//    realizes (entry bid - exit ask) * qty.
{
  const { engine, pf } = rig();
  engine.placeOrder({ symbol: "EURUSD", side: "sell", type: "market", qtyAmount: 20_000 });
  check("short position opens at bid", pf.positions.EURUSD.qtyBase === -20_000
    && pf.positions.EURUSD.avgPrice === 1.13916);
  const o = engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 20_000 });
  const f = pf.blotter[1];
  check("short cover realizes (entry - exit) * qty",
    o.status === "filled"
    && Math.abs(f.realizedPnlQuote - (1.13916 - 1.13922) * 20_000) < 1e-6);
}

// 7. Gate closed (stale quote): market order is rejected, never queued, and
//    the blotter is untouched.
{
  const { quotes, engine, pf } = rig();
  quotes.ingest(tick({ timestamp: "2026-09-24T11:59:15Z" }), NOW); // 45s old
  const o = engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 5_000 });
  check("stale quote rejects market order", o.status === "rejected"
    && o.statusReason.includes("quote_stale"));
  check("rejected order leaves no fill", pf.blotter.length === 0 && pf.cash.EUR === 0);
}

// 8. Limit buy rests pending while ask > limit, fills at the available ASK
//    when an incoming quote reaches the limit price.
{
  const { quotes, engine, pf } = rig();
  const o = engine.placeOrder({
    symbol: "EURUSD", side: "buy", type: "limit", qtyAmount: 10_000, limitPrice: 1.13910,
  });
  check("limit buy pending while ask above limit", o.status === "pending");
  quotes.ingest(tick({ bid: 1.13900, ask: 1.13906, mid: 1.13903 }), NOW); // ask now <= 1.13910
  engine.processSymbol("EURUSD");
  check("limit buy fills when incoming ask reaches the price", o.status === "filled");
  const f = pf.blotter[0];
  check("limit fill uses the available ask, not the limit", f.price === 1.13906);
}

// 9. Limit sell fills at the available BID when bid >= limit.
{
  const { quotes, engine, pf } = rig();
  const o = engine.placeOrder({
    symbol: "EURUSD", side: "sell", type: "limit", qtyAmount: 5_000, limitPrice: 1.13920,
  });
  check("limit sell pending while bid below limit", o.status === "pending");
  quotes.ingest(tick({ bid: 1.13925, ask: 1.13931, mid: 1.13928 }), NOW);
  engine.processSymbol("EURUSD");
  const f = pf.blotter[0];
  check("limit sell fills at available bid", o.status === "filled" && f.price === 1.13925);
}

// 10. A closed-market tick never fills a resting limit, even if its price
//     would be reachable.
{
  const { quotes, engine } = rig();
  const o = engine.placeOrder({
    symbol: "EURUSD", side: "buy", type: "limit", qtyAmount: 10_000, limitPrice: 1.13900,
  });
  check("limit below the ask rests pending", o.status === "pending");
  quotes.ingest(tick({ marketState: "closed", bid: 1.0, ask: 1.0, mid: 1.0 }), NOW);
  engine.processSymbol("EURUSD");
  check("closed-market tick does not fill a resting limit", o.status === "pending");
}

// 11. Cancel: a pending limit becomes cancelled and never fills afterwards.
{
  const { quotes, engine, pf } = rig();
  const o = engine.placeOrder({
    symbol: "EURUSD", side: "buy", type: "limit", qtyAmount: 10_000, limitPrice: 1.13900,
  });
  engine.cancelOrder(o.orderId);
  check("pending order can be cancelled", o.status === "cancelled");
  quotes.ingest(tick({ ask: 1.13894, bid: 1.13888, mid: 1.13891 }), NOW); // would have been reachable
  engine.processSymbol("EURUSD");
  check("cancelled order never fills", pf.blotter.length === 0);
}

// 12. Insufficient funds: a market buy that would borrow far beyond equity is
//     rejected with the amounts named; the blotter stays empty. (Buying power:
//     after the fill, total borrowed cash valued in the reporting currency
//     must not exceed equity — one rule for buys and sells.)
{
  const { engine, pf } = rig({ equity: 1_000 });
  const o = engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  check("unaffordable market buy rejected", o.status === "rejected"
    && o.statusReason.includes("insufficient_funds"));
  check("rejection leaves cash and blotter untouched",
    pf.blotter.length === 0 && pf.cash.USD === 1_000);
}

// 13. Blotter immutability: later ticks and fills never rewrite an earlier
//     execution record.
{
  const { quotes, engine, pf } = rig();
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  const snapshot = JSON.stringify(pf.blotter[0]);
  quotes.ingest(tick({ bid: 1.2, ask: 1.20006, mid: 1.20003 }), NOW);
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 5_000 });
  check("earlier fill unchanged after later ticks and fills",
    JSON.stringify(pf.blotter[0]) === snapshot && pf.blotter.length === 2);
}

// 14. Two portfolios are independent: fills, cash and orders do not leak.
{
  const { quotes, portfolios, engine } = rig();
  const beta = portfolios.create({ name: "Beta", startingEquityAmount: 10_000, reportingCurrency: "USD" });
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 1_000 });
  check("fill lands in the active portfolio only",
    beta.blotter.length === 1 && portfolios.get("pf-1").blotter.length === 0);
  // A resting limit in the non-selected portfolio still fills on incoming quotes.
  portfolios.select("pf-1");
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "limit", qtyAmount: 2_000, limitPrice: 1.13800 });
  portfolios.select("pf-2");
  quotes.ingest(tick({ bid: 1.13788, ask: 1.13794, mid: 1.13791 }), NOW);
  engine.processSymbol("EURUSD");
  check("resting limits fill even while their portfolio is not selected",
    portfolios.get("pf-1").blotter.length === 1);
}

// 15. Order history retains every state transition, including rejected and
//     cancelled, so the full trading record is inspectable.
{
  const { quotes, engine, pf } = rig();
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 1_000 }); // filled
  const l = engine.placeOrder({ symbol: "EURUSD", side: "sell", type: "limit", qtyAmount: 1_000, limitPrice: 2.0 });
  engine.cancelOrder(l.orderId); // cancelled
  quotes.setConnectionState("disconnected");
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 1_000 }); // rejected
  const states = pf.orders.map((o) => o.status).sort();
  check("history holds filled, cancelled and rejected orders",
    states.join(",") === "cancelled,filled,rejected" && pf.orders.length === 3);
}

// 16. F1: a USD portfolio can BUY USDJPY spot — the JPY cost is borrowed
//     against equity, not refused for lack of JPY cash.
{
  const quotes = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  quotes.setConnectionState("streaming");
  quotes.ingest(tick(), NOW);
  quotes.ingest(tick({
    symbol: "USDJPY", bid: 148.48, ask: 148.52, mid: 148.50, spread: 0.04,
  }), NOW);
  const portfolios = new PortfolioStore({ storage: null, now: () => NOW });
  const engine = new ExecutionEngine({ quoteStore: quotes, portfolioStore: portfolios, now: () => NOW });
  const pf = portfolios.create({ name: "Alpha", startingEquityAmount: 100_000, reportingCurrency: "USD" });
  const o = engine.placeOrder({ symbol: "USDJPY", side: "buy", type: "market", qtyAmount: 10_000 });
  check("USD portfolio buys USDJPY spot (fills at ask)", o.status === "filled"
    && pf.blotter[0].price === 148.52);
  check("JPY cost is borrowed, valued inside buying power",
    pf.cash.JPY === -10_000 * 148.52 && pf.cash.USD === 110_000);
  // Beyond equity it is still rejected: 1,000,000 USDJPY borrows ~1M USD.
  const big = engine.placeOrder({ symbol: "USDJPY", side: "buy", type: "market", qtyAmount: 1_000_000 });
  check("USDJPY buy beyond equity is rejected", big.status === "rejected"
    && big.statusReason.includes("insufficient_funds"));
}

// 17. F1: the sell side is bounded by the same rule — an outsized short is
//     rejected while an ordinary short fills (check 6).
{
  const { engine, pf } = rig();
  const o = engine.placeOrder({ symbol: "EURUSD", side: "sell", type: "market", qtyAmount: 5_000_000 });
  check("outsized short rejected by the same buying-power rule", o.status === "rejected"
    && o.statusReason.includes("insufficient_funds"));
  check("rejected short leaves no fill", pf.blotter.length === 0 && pf.cash.EUR === 0);
}

console.log(failures === 0 ? "\nAll checks passed." : `\n${failures} check(s) FAILED.`);
process.exit(failures === 0 ? 0 : 1);
