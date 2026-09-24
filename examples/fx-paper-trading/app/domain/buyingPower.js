// Buying power: one admission rule for both sides of every simulated fill.
//
// The cash ledger is multi-currency, so a fill can legitimately leave a
// currency negative: buying USDJPY from a USD portfolio borrows JPY, selling
// EURUSD short borrows EUR. A short option lot only ever RECEIVES premium, so
// borrowing alone never bounds option sales: every open short lot also carries
// a margin requirement of optionMarginFraction x notional, valued exactly like
// borrowed cash.
//
// A fill is admitted when ANY of these holds:
//   1. it does not increase the total requirement (borrowed + short-option
//      margin) — partial closes, buy-backs and other risk-off trades stay
//      possible even when the account is already over the limit;
//   2. it flattens the pair's net delta without flipping it to the opposite
//      side — the post-fill delta keeps its sign, or crosses flat by no more
//      than a small tolerance, so the exact-flat risk-view hedge is admitted
//      even when it borrows against negative equity, while an overshooting
//      "hedge" is not;
//   3. the post-fill requirement does not exceed current equity — the
//      ordinary bound for new risk, identical for buys and sells, spot and
//      options.
//
// A fill that opens a NEW short option lot never qualifies under rule 1 or
// rule 2 and always faces rule 3: a sale that shrinks net delta still adds
// vega/gamma risk and can double notional exposure, and the premium it
// receives is the price of a liability the book now carries, not buying
// power. For such a fill the requirement tested against equity is the
// account's pre-fill requirement plus the new lot's margin — the incoming
// premium is never credited against borrowing for the admission test. This
// closes the ratchet in which a deep-in-the-money sale repays borrowing with
// its premium (rule 1) and the exact-flat hedge (rule 2) then re-leverages
// the account for a larger sale. Closes, buy-backs and the exact-flat spot
// hedge open no short lot and stay admitted under rules 1 and 2 as before.
//
// Equity, borrowed balances and margin are valued only with observed quotes:
// if a needed conversion has no quote, the fill falls back to the delta test
// and is otherwise rejected as unprovable rather than allowed on an assumed
// rate.

import { money, convertMoney } from "./units.js";

const TOL = 1e-6;

// Rule-2 tolerance: a delta-flattening fill may cross flat by at most this
// many base units — the same display-noise floor OptionBook.hedgeSuggestion
// uses — so the exact-flat risk-view hedge stays admitted while an
// overshooting fill that flips the pair's exposure falls through to the
// equity test.
const DELTA_FLAT_TOL_BASE = 0.5;

// Cash-only equity in the reporting currency: every cash balance converted at
// observed mids. This is the default equity measure; the app injects
// computeEquity (analytics.js), which adds the model value of open option
// lots, so checks and the live terminal share the same rule.
export function cashEquity(pf, quoteStore, nowMs = Date.now()) {
  let total = 0;
  const missing = [];
  for (const [currency, amount] of Object.entries(pf.cash)) {
    if (Math.abs(amount) < 1e-9) continue;
    const conv = convertMoney(money(amount, currency), pf.reportingCurrency, quoteStore, nowMs);
    if (!conv.ok) { missing.push(conv.neededSymbol ?? `${currency} (${conv.reason})`); continue; }
    total += conv.result.amount;
  }
  if (missing.length) return { ok: false, missing };
  return { ok: true, amount: total, currency: pf.reportingCurrency };
}

// Total borrowed across currencies: every negative cash balance, valued in
// the reporting currency at observed mids.
function borrowedTotal(cash, reportingCurrency, quoteStore, nowMs) {
  let total = 0;
  const missing = [];
  for (const [currency, amount] of Object.entries(cash)) {
    if (amount >= -1e-9) continue;
    const conv = convertMoney(money(-amount, currency), reportingCurrency, quoteStore, nowMs);
    if (!conv.ok) { missing.push(conv.neededSymbol ?? `${currency} (${conv.reason})`); continue; }
    total += conv.result.amount;
  }
  if (missing.length) return { ok: false, missing };
  return { ok: true, amount: total };
}

// Margin carried by open SHORT option lots: optionMarginFraction x notional
// per lot, converted to the reporting currency at observed mids. Long lots
// carry no margin — their premium was paid up front and is their maximum loss.
export function shortOptionMargin(pf, quoteStore, nowMs = Date.now(), marginFraction = 0) {
  if (!marginFraction) return { ok: true, amount: 0 };
  let total = 0;
  const missing = [];
  for (const lot of pf.optionLots ?? []) {
    if (lot.status !== "open" || lot.qtySign >= 0) continue;
    const conv = convertMoney(money(lot.notional.amount * marginFraction, lot.notional.currency),
      pf.reportingCurrency, quoteStore, nowMs);
    if (!conv.ok) { missing.push(conv.neededSymbol ?? `${lot.notional.currency} (${conv.reason})`); continue; }
    total += conv.result.amount;
  }
  if (missing.length) return { ok: false, missing };
  return { ok: true, amount: total };
}

// Would this fill be admitted? cashDelta: { currency: signed change } exactly
// as the fill would apply it. opts:
//   marginFraction — fraction of short-option notional carried as margin;
//   marginDelta    — { currency, amount } signed change in short-option
//                    notional caused by this fill (selling to open adds,
//                    buying back a short releases);
//   deltaEffect    — { symbol, deltaBase } signed base-currency delta change;
//   netDeltaOf     — (pf, symbol) -> { ok, netDeltaBase } | { ok: false },
//                    the pair's current net delta (spot + option lots);
//   opensShortLot  — true when the fill opens a new short option lot: such
//                    fills never qualify for the risk-off or delta-flattening
//                    admissions and always face the margin/equity test, with
//                    the premium they receive not credited against the
//                    requirement (see rule 3 below).
// Returns:
//   { ok: true, admittedBy, borrowed, margin, requirement, currency }
//   { ok: false, reason: "insufficient_funds", borrowed, margin, requirement, equity, currency }
//   { ok: false, reason: "buying_power_unprovable", detail }
export function checkBuyingPower(pf, cashDelta, quoteStore, nowMs = Date.now(), equityOf = null, opts = {}) {
  const { marginFraction = 0, marginDelta = null, deltaEffect = null, netDeltaOf = null,
    opensShortLot = false } = opts;
  const reporting = pf.reportingCurrency;

  const simulated = { ...pf.cash };
  for (const [currency, delta] of Object.entries(cashDelta)) {
    simulated[currency] = (simulated[currency] ?? 0) + delta;
  }
  const borrowedBefore = borrowedTotal(pf.cash, reporting, quoteStore, nowMs);
  const borrowedAfter = borrowedTotal(simulated, reporting, quoteStore, nowMs);

  const marginBefore = shortOptionMargin(pf, quoteStore, nowMs, marginFraction);
  let marginDeltaAmount = 0;
  let marginDeltaMissing = null;
  if (marginFraction && marginDelta && Math.abs(marginDelta.amount) > 1e-9) {
    const conv = convertMoney(money(marginDelta.amount * marginFraction, marginDelta.currency),
      reporting, quoteStore, nowMs);
    if (conv.ok) marginDeltaAmount = conv.result.amount;
    else marginDeltaMissing = conv.neededSymbol ?? `${marginDelta.currency} (${conv.reason})`;
  }

  const requirementKnown = borrowedBefore.ok && borrowedAfter.ok
    && marginBefore.ok && marginDeltaMissing === null;
  const marginAfterAmount = requirementKnown ? marginBefore.amount + marginDeltaAmount : null;
  const reqBefore = requirementKnown ? borrowedBefore.amount + marginBefore.amount : null;
  const reqAfter = requirementKnown ? borrowedAfter.amount + marginAfterAmount : null;

  // 1. Risk-off: a fill that does not increase the total requirement is
  //    always admitted, whatever the equity level. A fill opening a new short
  //    option lot never qualifies here: its premium can repay borrowing and
  //    make the requirement look flat while the book takes on a large short
  //    option liability — such fills always face the rule-3 equity test.
  if (!opensShortLot && requirementKnown && reqAfter <= reqBefore + TOL) {
    return {
      ok: true, admittedBy: "requirement_not_increased",
      borrowed: borrowedAfter.amount, margin: marginAfterAmount,
      requirement: reqAfter, currency: reporting,
    };
  }

  // 2. Delta-flattening: a fill that shrinks the pair's absolute net delta is
  //    a hedge or partial risk reduction and is admitted even when it borrows
  //    — but only if it does not flip the exposure to the opposite side. The
  //    post-fill delta must keep its sign or cross flat within the tolerance,
  //    so an exact-flat hedge qualifies while an overshooting "hedge" that
  //    leaves the pair exposed the other way faces the equity test instead.
  //    Fills opening a new short option lot never qualify: a delta-reducing
  //    sale still adds margin, vega and gamma risk.
  if (!opensShortLot && deltaEffect && netDeltaOf) {
    const nd = netDeltaOf(pf, deltaEffect.symbol);
    if (nd && nd.ok && Number.isFinite(nd.netDeltaBase)) {
      const before = nd.netDeltaBase;
      const after = before + deltaEffect.deltaBase;
      const reducesAbs = Math.abs(after) < Math.abs(before) - 1e-9;
      const keepsSide = Math.sign(after) === Math.sign(before)
        || Math.abs(after) <= DELTA_FLAT_TOL_BASE;
      if (reducesAbs && keepsSide) {
        return {
          ok: true, admittedBy: "delta_reduced",
          borrowed: borrowedAfter.ok ? borrowedAfter.amount : null,
          margin: marginAfterAmount, requirement: reqAfter, currency: reporting,
        };
      }
    }
  }

  // 3. Ordinary new risk: the post-fill requirement must fit within equity.
  //    A fill opening a new short option lot reaches this test directly
  //    (rules 1 and 2 never apply to it), and the premium it receives is not
  //    buying power: it is the price of the liability the book now carries.
  //    The requirement tested is therefore the account's pre-fill requirement
  //    plus the new lot's margin, never reduced by the incoming premium.
  if (!requirementKnown) {
    const missing = [
      ...(borrowedBefore.ok ? [] : borrowedBefore.missing),
      ...(borrowedAfter.ok ? [] : borrowedAfter.missing),
      ...(marginBefore.ok ? [] : marginBefore.missing),
      ...(marginDeltaMissing ? [marginDeltaMissing] : []),
    ];
    return {
      ok: false, reason: "buying_power_unprovable",
      detail: `requirement cannot be valued (${missing.join(", ")})`,
    };
  }
  const eq = equityOf ? equityOf(pf, nowMs) : cashEquity(pf, quoteStore, nowMs);
  if (!eq.ok) {
    return {
      ok: false, reason: "buying_power_unprovable",
      detail: `equity unavailable (${(eq.missing ?? []).join(", ")})`,
    };
  }
  // computeEquity returns { equity: Money }; cashEquity returns { amount }.
  const equityAmount = eq.equity ? eq.equity.amount : eq.amount;
  // For an opening short lot the premium's paydown of borrowing is excluded
  // from the requirement; for every other fill the post-fill requirement is
  // used as computed. marginDeltaAmount is exactly the new lot's margin.
  const requirementTested = opensShortLot ? reqBefore + marginDeltaAmount : reqAfter;
  if (requirementTested > equityAmount + TOL) {
    return {
      ok: false, reason: "insufficient_funds",
      borrowed: opensShortLot ? borrowedBefore.amount : borrowedAfter.amount,
      margin: marginAfterAmount,
      requirement: requirementTested, equity: equityAmount, currency: reporting,
    };
  }
  return {
    ok: true,
    admittedBy: opensShortLot ? "within_equity_opening_short" : "within_equity",
    borrowed: borrowedAfter.amount, margin: marginAfterAmount,
    requirement: requirementTested, equity: equityAmount, currency: reporting,
  };
}
