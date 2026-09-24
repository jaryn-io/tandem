// P&L analytics, currency exposure, spread cost and equity for a paper
// portfolio. Pure computations over the portfolio record, the quote store and
// the option book — the same objects the execution layer uses, so the numbers
// shown here can never disagree with the blotter.
//
// Conventions:
//  - spot P&L comes from observed bid/ask executions and exit-side marks;
//    option P&L comes from Garman–Kohlhagen model marks and is always labelled
//    model-derived — the two are never merged into an unlabelled figure;
//  - every conversion into the reporting currency uses a live feed mid quote
//    via convertMoney and names the rate it used; when a needed quote is
//    missing the figure is reported unavailable rather than estimated;
//  - spread cost is what the fill paid away from the mid: half the observed
//    bid/ask spread for spot fills, the assumed model spread for option fills.
//
// Equity over time: the S02 cash model means multi-currency cash already
// contains the value of every spot position, so current equity is converted
// cash plus the signed model value of open option lots. Equity samples are
// appended to the portfolio's persisted equityHistory (schema v3) by
// maybeRecordEquity — on every blotter change and at most once per minute
// otherwise — so the curve survives a restart.

import { PAIRS, money, convertMoney, legDisclosure, formatMoney } from "./units.js";

export const FILL_GROUPS = ["spot", "hedge", "option"];

export function fillGroup(fill) {
  if (fill.kind === "option" || fill.kind === "option_expiry") return "option";
  if (fill.kind === "spot" && fill.origin === "delta-hedge") return "hedge";
  return "spot";
}

// What the fill paid away from the mid, in the fill's price currency.
export function fillSpreadCost(fill) {
  if (fill.kind === "spot") {
    const mid = (fill.quote.bid + fill.quote.ask) / 2;
    return fill.side === "buy"
      ? (fill.price - mid) * fill.qty.amount
      : (mid - fill.price) * fill.qty.amount;
  }
  if (fill.kind === "option") {
    return Math.abs(fill.price - fill.model.modelMid) * fill.qty.amount;
  }
  return 0; // expiry settlement at intrinsic carries no spread
}

// Unrealized P&L of a net spot position, marked at the exit side of the
// market (long at bid, short at ask), in the pair's quote currency. A flat
// position is a known zero, not "unavailable"; only a live position without
// a quote is unpriced.
export function spotUnrealized(symbol, pos, quoteStore) {
  if (pos.qtyBase === 0) return { ok: true, amount: 0, currency: PAIRS[symbol].quote };
  const q = quoteStore.latest(symbol);
  if (!q) return { ok: false, reason: "no_quote" };
  const exit = pos.qtyBase > 0 ? q.bid : q.ask;
  return { ok: true, amount: (exit - pos.avgPrice) * pos.qtyBase, currency: PAIRS[symbol].quote };
}

function toReporting(amountNumber, fromCurrency, reportingCurrency, quoteStore, nowMs, used) {
  if (fromCurrency === reportingCurrency) {
    return { ok: true, amount: amountNumber };
  }
  if (amountNumber === 0) return { ok: true, amount: 0 }; // zero needs no rate
  const conv = convertMoney(money(amountNumber, fromCurrency), reportingCurrency, quoteStore, nowMs);
  if (!conv.ok) return { ok: false, neededSymbol: conv.neededSymbol, fromCurrency };
  // Disclose every observed leg the conversion used (one for a direct pair,
  // two for a USD cross).
  for (const leg of conv.legs ?? []) {
    if (leg.rateSymbol) used.set(leg.rateSymbol, legDisclosure(leg, quoteStore, nowMs));
  }
  return { ok: true, amount: conv.result.amount };
}

// Full analytics snapshot for one portfolio. All per-pair figures are in the
// pair's quote currency; reporting-currency totals carry their conversion
// disclosures. `unavailable` lists what could not be converted and why.
export function computeAnalytics(pf, quoteStore, book, nowMs = Date.now()) {
  const reporting = pf.reportingCurrency;
  const used = new Map(); // rateSymbol -> disclosure string
  const unavailable = [];

  const perPair = {}; // symbol -> { realized:{spot,hedge,option}, spreadCost, unrealizedSpot, unrealizedOptions, quoteCcy }
  const pairRow = (symbol) => perPair[symbol] ??= {
    realized: { spot: 0, hedge: 0, option: 0 },
    spreadCost: 0,
    unrealizedSpot: null,   // number | null when no quote
    unrealizedOptions: 0,   // model-derived
    unpricedLots: 0,
    quoteCurrency: PAIRS[symbol].quote,
  };

  const contributions = { spot: 0, hedge: 0, option: 0 }; // realized, reporting ccy
  const contributionKnown = { spot: true, hedge: true, option: true };
  let realizedTotal = 0;
  let realizedKnown = true;
  let spreadTotal = 0;
  let spreadKnown = true;

  const tradeSequence = [];
  let cumulative = 0;
  let cumulativeKnown = true;
  for (const f of pf.blotter) { // blotter is append-only chronological
    const pair = PAIRS[f.symbol];
    const group = fillGroup(f);
    const row = pairRow(f.symbol);
    row.realized[group] += f.realizedPnlQuote;
    const spread = fillSpreadCost(f);
    row.spreadCost += spread;

    const realizedConv = toReporting(f.realizedPnlQuote, f.priceCurrency, reporting, quoteStore, nowMs, used);
    if (realizedConv.ok) {
      contributions[group] += realizedConv.amount;
      realizedTotal += realizedConv.amount;
      cumulative += realizedConv.amount;
    } else {
      contributionKnown[group] = false;
      realizedKnown = false;
      cumulativeKnown = false;
      unavailable.push(`realized P&L of ${f.fillId} (${realizedConv.neededSymbol ?? realizedConv.reason})`);
    }
    const spreadConv = toReporting(spread, f.priceCurrency, reporting, quoteStore, nowMs, used);
    if (spreadConv.ok) spreadTotal += spreadConv.amount;
    else {
      spreadKnown = false;
      unavailable.push(`spread cost of ${f.fillId} (${spreadConv.neededSymbol ?? spreadConv.reason})`);
    }

    tradeSequence.push({
      fillId: f.fillId,
      filledAtMs: f.filledAtMs,
      symbol: f.symbol,
      kind: f.kind,
      group,
      modelDerived: f.modelDerived === true,
      side: f.side,
      qty: f.qty,
      price: f.price,
      priceCurrency: f.priceCurrency,
      realizedQuote: f.realizedPnlQuote,
      realizedReporting: realizedConv.ok ? realizedConv.amount : null,
      cumulativeReporting: cumulativeKnown ? cumulative : null,
      quoteTimestampMs: f.quote.timestampMs,
    });
  }

  // Unrealized spot P&L on net positions (exit side of the observed market).
  let unrealSpotTotal = 0;
  let unrealSpotKnown = true;
  for (const [symbol, pos] of Object.entries(pf.positions)) {
    const u = spotUnrealized(symbol, pos, quoteStore);
    const row = pairRow(symbol);
    row.unrealizedSpot = u.ok ? u.amount : null;
    if (!u.ok) {
      unrealSpotKnown = false;
      unavailable.push(`unrealized spot P&L of ${symbol} (${u.reason})`);
      continue;
    }
    const conv = toReporting(u.amount, PAIRS[symbol].quote, reporting, quoteStore, nowMs, used);
    if (conv.ok) unrealSpotTotal += conv.amount;
    else {
      unrealSpotKnown = false;
      unavailable.push(`unrealized spot P&L of ${symbol} (${conv.neededSymbol ?? conv.reason})`);
    }
  }

  // Unrealized option P&L from Garman–Kohlhagen model marks (labelled as such).
  let unrealOptTotal = 0;
  let unrealOptKnown = true;
  for (const lot of pf.optionLots) {
    if (lot.status !== "open") continue;
    const row = pairRow(lot.symbol);
    const mark = book.markLot(lot, nowMs);
    if (!mark.ok) {
      row.unpricedLots += 1;
      unrealOptKnown = false;
      unavailable.push(`model mark of lot ${lot.lotId} (${mark.reason})`);
      continue;
    }
    row.unrealizedOptions += mark.unrealizedPnlQuote;
    const conv = toReporting(mark.unrealizedPnlQuote, PAIRS[lot.symbol].quote, reporting, quoteStore, nowMs, used);
    if (conv.ok) unrealOptTotal += conv.amount;
    else {
      unrealOptKnown = false;
      unavailable.push(`unrealized option P&L of lot ${lot.lotId} (${conv.neededSymbol ?? conv.reason})`);
    }
  }

  // Currency exposure: the cash ledger per currency with its conversion.
  const cashExposure = [];
  for (const [currency, amount] of Object.entries(pf.cash)) {
    if (Math.abs(amount) < 1e-9) continue;
    const conv = toReporting(amount, currency, reporting, quoteStore, nowMs, used);
    cashExposure.push({
      currency,
      amount,
      reporting: conv.ok ? conv.amount : null,
      neededSymbol: conv.ok ? null : (conv.neededSymbol ?? null),
    });
  }

  // Reconciliation between the equity change and the P&L cards. Equity values
  // every cash balance at mid; unrealized spot P&L is marked at the exit side
  // of the observed bid/ask, so the two legitimately differ by the half-spread
  // still carried on open spot positions. (Realized fills and option marks
  // reconcile exactly: cash effects are the same numbers both ways.) The
  // adjustment line makes that difference explicit during a demo instead of
  // leaving the figures apparently inconsistent.
  const eqForRecon = computeEquity(pf, quoteStore, book, nowMs, used);
  let reconciliation;
  if (eqForRecon.ok && realizedKnown && unrealSpotKnown && unrealOptKnown) {
    const equityChange = eqForRecon.equity.amount - pf.startingEquity.amount;
    const pnlSum = realizedTotal + unrealSpotTotal + unrealOptTotal;
    reconciliation = { ok: true, equityChange, pnlSum, adjustment: equityChange - pnlSum };
  } else {
    reconciliation = { ok: false };
  }

  return {
    reportingCurrency: reporting,
    perPair,
    contributions,
    contributionKnown,
    tradeSequence,
    cashExposure,
    totals: {
      realized: { ok: realizedKnown, amount: realizedTotal },
      unrealizedSpot: { ok: unrealSpotKnown, amount: unrealSpotTotal },
      unrealizedOptions: { ok: unrealOptKnown, amount: unrealOptTotal }, // model-derived
      spreadCost: { ok: spreadKnown, amount: spreadTotal },
    },
    reconciliation,
    conversionDisclosures: [...used.values()],
    unavailable,
    disclosuresUsed: used, // rateSymbol -> disclosure, for equity reuse
  };
}

// Current equity in the portfolio's reporting currency: converted cash plus
// the signed model value of open option lots. Spot positions are already
// inside the multi-currency cash ledger. Option values are model-derived.
export function computeEquity(pf, quoteStore, book, nowMs = Date.now(), used = new Map()) {
  const reporting = pf.reportingCurrency;
  let total = 0;
  const missing = [];
  for (const [currency, amount] of Object.entries(pf.cash)) {
    if (Math.abs(amount) < 1e-9) continue;
    const conv = toReporting(amount, currency, reporting, quoteStore, nowMs, used);
    if (!conv.ok) missing.push(conv.neededSymbol ?? `${currency} (${conv.reason ?? "no path"})`);
    else total += conv.amount;
  }
  for (const lot of pf.optionLots) {
    if (lot.status !== "open") continue;
    const mark = book.markLot(lot, nowMs);
    if (!mark.ok) { missing.push(`lot ${lot.lotId} (${mark.reason})`); continue; }
    const conv = toReporting(lot.qtySign * mark.valueQuote, PAIRS[lot.symbol].quote,
      reporting, quoteStore, nowMs, used);
    if (!conv.ok) missing.push(conv.neededSymbol ?? `lot ${lot.lotId}`);
    else total += conv.amount;
  }
  if (missing.length) return { ok: false, missing, reportingCurrency: reporting };
  return { ok: true, equity: money(total, reporting), reportingCurrency: reporting };
}

// Append an equity sample when the blotter changed or minIntervalMs elapsed
// since the last one. Samples are persisted with the portfolio. Returns the
// sample written, or null when nothing was recorded (no change yet, or the
// equity is not convertible right now — an unconvertible moment is skipped,
// never estimated).
export function maybeRecordEquity(portfolioStore, quoteStore, book, nowMs = Date.now(),
  { minIntervalMs = 60_000, maxSamples = 2000 } = {}) {
  const pf = portfolioStore.active();
  if (!pf) return null;
  const hist = pf.equityHistory;
  const last = hist[hist.length - 1];
  const blotterLength = pf.blotter.length;
  if (last && last.blotterLength === blotterLength && nowMs - last.t < minIntervalMs) return null;
  const used = new Map();
  const eq = computeEquity(pf, quoteStore, book, nowMs, used);
  if (!eq.ok) return null;
  const sample = {
    t: nowMs, amount: eq.equity.amount, currency: eq.equity.currency, blotterLength,
  };
  portfolioStore.update(pf.id, (doc) => {
    doc.equityHistory.push(sample);
    if (doc.equityHistory.length > maxSamples) {
      // Thin old samples rather than dropping the curve's shape or its end.
      doc.equityHistory = doc.equityHistory.filter((_, i) => i % 2 === 0 || i >= doc.equityHistory.length - 2);
    }
  });
  return sample;
}

export function formatReporting(value, reporting) {
  // value: { ok, amount } — honest display helper for totals.
  if (!value.ok) return "unavailable (see notes)";
  return formatMoney(money(value.amount, reporting));
}
