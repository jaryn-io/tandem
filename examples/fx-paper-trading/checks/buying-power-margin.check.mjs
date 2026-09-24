// Deterministic checks for the buying-power admission rule: short option
// sales carry a margin requirement (F1), new short option lots always face
// the margin/equity test — even when they reduce net delta or their premium
// repays borrowing, since the incoming premium is not buying power —
// delta-flattening fills are admitted only when they do not flip the
// exposure's sign past a small tolerance, and fills that do not increase the
// requirement or that flatten delta — partial closes over the limit,
// exact-flat risk-view hedges after an adverse move — stay admitted
// (G-verify-F7). Synthetic ticks stand in for the feed; no network.
// Run:
//   node checks/buying-power-margin.check.mjs

import { QuoteStore } from "../app/marketdata/quoteStore.js";
import { PortfolioStore } from "../app/domain/portfolio.js";
import { ExecutionEngine } from "../app/domain/execution.js";
import { OptionBook } from "../app/domain/optionBook.js";
import { computeEquity } from "../app/domain/analytics.js";

const NOW = Date.parse("2026-09-24T12:00:00Z");
const SPREAD = 0.02;
const MARGIN = 0.10;
let failures = 0;

function check(name, cond) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
  if (!cond) failures++;
}

function eurTick(overrides = {}) {
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

function jpyTick(overrides = {}) {
  return {
    symbol: "USDJPY",
    bid: 157.98, ask: 158.02, mid: 158.00, spread: 0.04,
    timestamp: "2026-09-24T11:59:59Z",
    source: "MetaTrader 5 (Broker 1)",
    marketState: "open", stale: false, quoteAgeSeconds: 1,
    direction: "FLAT", high: 159, low: 157,
    ...overrides,
  };
}

const ASSUME = { volatility: 0.10, rateDomestic: 0.035, rateForeign: 0.02 };
const EXPIRY = NOW + 30 * 86400_000;

// Rig wired like main.js: equity includes option model values, the spot
// engine sees the book's net delta, and short lots carry margin.
function rig({ equity = 100_000 } = {}) {
  const quotes = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  quotes.setConnectionState("streaming");
  const portfolios = new PortfolioStore({ storage: null, now: () => NOW });
  let book = null;
  const equityOf = (pf, nowMs) => computeEquity(pf, quotes, book, nowMs);
  const netDeltaOf = (pf, symbol) => {
    const agg = book?.aggregateDelta(pf)[symbol];
    return agg && agg.unpricedLots === 0 ? { ok: true, netDeltaBase: agg.netDeltaBase } : { ok: false };
  };
  const engine = new ExecutionEngine({
    quoteStore: quotes, portfolioStore: portfolios, equityOf,
    optionMarginFraction: MARGIN, netDeltaOf, now: () => NOW,
  });
  book = new OptionBook({
    quoteStore: quotes, portfolioStore: portfolios, optionSpreadFraction: SPREAD,
    optionMarginFraction: MARGIN, equityOf, now: () => NOW,
  });
  const pf = portfolios.create({ name: "Alpha", startingEquityAmount: equity, reportingCurrency: "USD" });
  return { quotes, portfolios, engine, book, pf };
}

function sellCall(notionalAmount) {
  return {
    symbol: "EURUSD", cp: "call", side: "sell", notionalAmount,
    strike: 1.14, expiryMs: EXPIRY, ...ASSUME,
  };
}

// 1. F1: a short option sale consumes buying power through margin — an
//    ordinary sale fills, an outsized one is rejected even though selling
//    only ever RECEIVES premium (borrowing alone would never bound it).
{
  const { quotes, book, pf } = rig();
  quotes.ingest(eurTick(), NOW);
  const small = book.placeOptionOrder(sellCall(100_000)); // margin ~11.4k USD
  check("short option sale within margin fills", small.status === "filled"
    && pf.optionLots.length === 1 && pf.optionLots[0].qtySign === -1);
  const big = book.placeOptionOrder(sellCall(2_000_000)); // margin ~228k USD
  check("outsized short option sale rejected despite premium income",
    big.status === "rejected" && big.statusReason.includes("insufficient_funds"));
  check("rejected sale leaves no lot", pf.optionLots.length === 1);
}

// 2. G-verify-F7 case A: a partial close is admitted while the account is
//    over the limit. USD portfolio buys 90,000 USDJPY at 158; USDJPY falls to
//    140, so the JPY borrowing exceeds equity; selling part of the long
//    reduces the requirement and must fill. A fresh buy that would increase
//    the requirement beyond equity is still rejected.
{
  const { quotes, engine, pf } = rig();
  quotes.ingest(jpyTick(), NOW);
  const buy = engine.placeOrder({ symbol: "USDJPY", side: "buy", type: "market", qtyAmount: 90_000 });
  check("initial USDJPY long fills", buy.status === "filled");
  quotes.ingest(jpyTick({ bid: 139.97, ask: 140.03, mid: 140.00 }), NOW);
  const equity = 190_000 - 90_000 * 158.02 / 140; // ~88.4k, below the borrowed ~101.6k
  const reduce = engine.placeOrder({ symbol: "USDJPY", side: "sell", type: "market", qtyAmount: 10_000 });
  check("partial close fills while over the limit", reduce.status === "filled");
  check("partial close realizes the loss against the entry",
    Math.abs(pf.blotter[1].realizedPnlQuote - (139.97 - 158.02) * 10_000) < 1e-6);
  const increase = engine.placeOrder({ symbol: "USDJPY", side: "buy", type: "market", qtyAmount: 10_000 });
  check("new risk beyond equity still rejected while over the limit",
    increase.status === "rejected" && increase.statusReason.includes("insufficient_funds"));
  check("account really was over the limit during the partial close",
    equity < 90_000 * 158.02 / 140);
}

// 3. G-verify-F7 case B: after an adverse move leaves equity negative, the
//    risk-view hedge (a delta-reducing spot buy that borrows) is admitted,
//    while a fresh risk-increasing buy is still rejected.
{
  const { quotes, engine, book, pf } = rig();
  quotes.ingest(eurTick(), NOW);
  const sale = book.placeOptionOrder(sellCall(800_000)); // margin ~91.1k <= equity
  check("short call within margin fills", sale.status === "filled");
  quotes.ingest(eurTick({ bid: 1.29997, ask: 1.30003, mid: 1.30000 }), NOW); // +14% adverse
  const eq = computeEquity(pf, quotes, book, NOW);
  check("adverse move leaves equity negative", eq.ok && eq.equity.amount < 0);

  const sug = book.hedgeSuggestion(pf, "EURUSD", NOW);
  check("hedge suggestion opposes the short-call delta", sug && sug.side === "buy");
  const hedge = engine.placeOrder({
    symbol: sug.symbol, side: sug.side, type: "market", qtyAmount: sug.qtyAmount, origin: "delta-hedge",
  });
  check("risk-view hedge fills despite borrowing beyond equity", hedge.status === "filled"
    && pf.blotter[1].origin === "delta-hedge");
  const after = book.aggregateDelta(pf, NOW).EURUSD;
  check("net delta is flat after the hedge", Math.abs(after.netDeltaBase) < 1e-6);

  const more = engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 10_000 });
  check("risk-increasing buy from flat delta still rejected",
    more.status === "rejected" && more.statusReason.includes("insufficient_funds"));
}

// 4. G-verify-F7: buying back the short lot after the adverse move is
//    admitted — it releases margin and removes delta even though the
//    buy-back premium borrows against negative equity.
{
  const { quotes, book, pf } = rig();
  quotes.ingest(eurTick(), NOW);
  book.placeOptionOrder(sellCall(800_000));
  quotes.ingest(eurTick({ bid: 1.29997, ask: 1.30003, mid: 1.30000 }), NOW);
  const close = book.closeLot("xl1");
  check("short buy-back fills over the limit", close.status === "filled"
    && close.closesLotId === "xl1");
  check("lot is closed with realized P&L", pf.optionLots[0].status === "closed"
    && typeof pf.optionLots[0].realizedPnlQuote === "number");
  const flat = book.aggregateDelta(pf, NOW).EURUSD;
  check("no exposure remains after buy-back", !flat || Math.abs(flat.netDeltaBase) < 1e-9);
}

// 5. Buying options is unchanged: a long call is premium-bounded only, and
//    closing it returns cash (requirement never increases) even over the
//    limit.
{
  const { quotes, book, pf } = rig();
  quotes.ingest(eurTick(), NOW);
  const buy = book.placeOptionOrder({
    symbol: "EURUSD", cp: "call", side: "buy", notionalAmount: 100_000,
    strike: 1.14, expiryMs: EXPIRY, ...ASSUME,
  });
  check("long option buy still fills", buy.status === "filled");
  quotes.ingest(eurTick({ bid: 1.09997, ask: 1.10003, mid: 1.10000 }), NOW); // adverse for the long
  const close = book.closeLot("xl1");
  check("long close fills after an adverse move", close.status === "filled");
  check("long close realizes the loss",
    pf.optionLots[0].status === "closed" && pf.optionLots[0].realizedPnlQuote < 0);
}

// 6. F1 rework: a delta-reducing short-option sale is NOT exempt from the
//    margin test. After a 1M EUR long call (net delta ~+512k), selling 2.5M
//    EUR of calls shrinks the absolute net delta — but the new short lot's
//    margin (~285k USD) exceeds equity, so the sale is rejected. A modest
//    sale whose margin fits equity still fills.
{
  const { quotes, book, pf } = rig();
  quotes.ingest(eurTick(), NOW);
  const long = book.placeOptionOrder({
    symbol: "EURUSD", cp: "call", side: "buy", notionalAmount: 1_000_000,
    strike: 1.14, expiryMs: EXPIRY, ...ASSUME,
  });
  check("long 1M EUR call fills", long.status === "filled");
  const before = book.aggregateDelta(pf, NOW).EURUSD.netDeltaBase;
  check("long call leaves positive net delta", before > 400_000);
  const sale = book.placeOptionOrder({
    symbol: "EURUSD", cp: "call", side: "sell", notionalAmount: 2_500_000,
    strike: 1.1674, expiryMs: EXPIRY, ...ASSUME,
  });
  check("delta-reducing short sale beyond margin refused",
    sale.status === "rejected" && sale.statusReason.includes("insufficient_funds"));
  check("refused sale leaves no short lot",
    pf.optionLots.filter((l) => l.qtySign < 0).length === 0);
  const modest = book.placeOptionOrder(sellCall(300_000)); // margin ~34k USD
  check("short sale within margin still fills against a long",
    modest.status === "filled" && pf.optionLots.filter((l) => l.qtySign < 0).length === 1);
}

// 7. F1 rework: a spot "hedge" that overshoots flat is refused when it
//    borrows beyond equity; the exact-flat hedge against the same position
//    is still admitted and leaves the pair delta-flat.
{
  const { quotes, engine, book, pf } = rig();
  quotes.ingest(eurTick(), NOW);
  const sale = book.placeOptionOrder(sellCall(800_000)); // margin ~91.1k USD
  check("short call within margin fills", sale.status === "filled");
  const nd = book.aggregateDelta(pf, NOW).EURUSD.netDeltaBase;
  check("short call leaves negative net delta", nd < -300_000);
  const overshoot = engine.placeOrder({
    symbol: "EURUSD", side: "buy", type: "market", qtyAmount: Math.abs(nd) * 1.9,
  });
  check("overshooting spot hedge beyond equity refused",
    overshoot.status === "rejected" && overshoot.statusReason.includes("insufficient_funds"));
  check("overshoot left no spot position", !pf.positions.EURUSD);
  const sug = book.hedgeSuggestion(pf, "EURUSD", NOW);
  const hedge = engine.placeOrder({
    symbol: sug.symbol, side: sug.side, type: "market", qtyAmount: sug.qtyAmount, origin: "delta-hedge",
  });
  check("exact-flat hedge still fills", hedge.status === "filled");
  const after = book.aggregateDelta(pf, NOW).EURUSD;
  check("net delta flat after exact hedge", Math.abs(after.netDeltaBase) < 1e-6);
}

// 8. F1 rework: repeated short sales cannot ratchet past equity through
//    delta effects — every new short lot faces the margin test, so sales
//    accumulate margin until it exceeds equity and the next one is refused.
{
  const { quotes, book, pf } = rig();
  quotes.ingest(eurTick(), NOW);
  const shortCall = { symbol: "EURUSD", cp: "call", side: "sell", notionalAmount: 400_000, strike: 1.17, expiryMs: EXPIRY, ...ASSUME };
  const shortPut = { ...shortCall, cp: "put", strike: 1.11 };
  const s1 = book.placeOptionOrder(shortCall); // margin ~45.6k USD
  const s2 = book.placeOptionOrder(shortPut);  // cumulative margin ~91.1k USD
  check("first two short sales fit within equity",
    s1.status === "filled" && s2.status === "filled");
  const s3 = book.placeOptionOrder(shortCall); // cumulative margin ~136.7k USD > equity
  check("third short sale refused once cumulative margin exceeds equity",
    s3.status === "rejected" && s3.statusReason.includes("insufficient_funds"));
  check("only the two admitted short lots exist",
    pf.optionLots.filter((l) => l.qtySign < 0 && l.status === "open").length === 2);
}

// 9. F1 refine: the premium of an opening short sale is not buying power.
//    From a borrowed-USD position (spot buy of 150k EUR), a deep-ITM short
//    call whose premium would repay most of the borrowing is still refused,
//    because the pre-fill requirement plus the new lot's margin exceeds
//    equity. The exact-flat spot hedge after the refusal stays admitted.
{
  const { quotes, engine, book, pf } = rig();
  quotes.ingest(eurTick(), NOW);
  const spot = engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 150_000 });
  check("spot buy within equity fills", spot.status === "filled");
  const itmSale = book.placeOptionOrder({
    symbol: "EURUSD", cp: "call", side: "sell", notionalAmount: 500_000,
    strike: 1.00, expiryMs: EXPIRY, ...ASSUME,
  });
  check("ITM short sale that pays down borrowing refused",
    itmSale.status === "rejected" && itmSale.statusReason.includes("insufficient_funds"));
  check("refused ITM sale leaves no lot", pf.optionLots.length === 0);
  const sug = book.hedgeSuggestion(pf, "EURUSD", NOW);
  const hedge = engine.placeOrder({
    symbol: sug.symbol, side: sug.side, type: "market", qtyAmount: sug.qtyAmount, origin: "delta-hedge",
  });
  check("exact-flat hedge after the refusal still fills", hedge.status === "filled");
  const flat = book.aggregateDelta(pf, NOW).EURUSD;
  check("net delta flat after the hedge", Math.abs(flat.netDeltaBase) < 1e-6);
}

// 10. F1 refine: the sale-then-hedge ratchet stops at the second sale. A
//     first ITM sale small enough to fit within equity fills, the exact-flat
//     hedge fills, and the next ITM sale is refused — short exposure can no
//     longer grow by alternating premium-funded sales and hedges.
{
  const { quotes, engine, book, pf } = rig();
  quotes.ingest(eurTick(), NOW);
  engine.placeOrder({ symbol: "EURUSD", side: "buy", type: "market", qtyAmount: 150_000 });
  const itmTicket = {
    symbol: "EURUSD", cp: "call", side: "sell", notionalAmount: 200_000,
    strike: 1.00, expiryMs: EXPIRY, ...ASSUME,
  };
  const first = book.placeOptionOrder(itmTicket);
  check("first ITM sale within equity fills", first.status === "filled");
  const sug = book.hedgeSuggestion(pf, "EURUSD", NOW);
  const hedge = engine.placeOrder({
    symbol: sug.symbol, side: sug.side, type: "market", qtyAmount: sug.qtyAmount, origin: "delta-hedge",
  });
  check("hedge after the first sale fills", hedge.status === "filled");
  const second = book.placeOptionOrder(itmTicket);
  check("second ITM sale after the hedge refused",
    second.status === "rejected" && second.statusReason.includes("insufficient_funds"));
  check("only the first short lot remains open",
    pf.optionLots.filter((l) => l.qtySign < 0 && l.status === "open").length === 1);
}

console.log(failures === 0 ? "\nAll checks passed." : `\n${failures} check(s) FAILED.`);
process.exit(failures === 0 ? 0 : 1);
