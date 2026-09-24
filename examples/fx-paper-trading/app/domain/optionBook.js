// Option book: simulated European vanilla option orders, open lots, marking,
// delta aggregation and expiry settlement. Built on the Garman–Kohlhagen model
// (app/domain/options.js) and the S01 fill gate.
//
// There is no options quote feed. Every option price here is model-derived:
//  - the model mid premium comes from GK on the observed spot mid plus the
//    user's volatility and rate assumptions;
//  - a simulated fill executes at the model mid plus/minus half of the assumed
//    execution spread (config.optionSpreadFraction of the premium), and the
//    spread is recorded and shown;
//  - every blotter entry and mark carries modelDerived: true and the full set
//    of model inputs, so a theoretical premium is never presentable as an
//    observed market bid or ask.
//
// Option fills are simulated fills, so they consume the same fillEligibility
// gate as spot: a closed market, dead feed or stale quote rejects the order
// (recorded as rejected, never silently queued). Marks (not fills) still use
// the retained last quote for display, labelled with its timestamp.
//
// Lots are directional: buying an option opens a long lot, selling opens a
// short lot. Closing or expiry realizes P&L against the open premium and moves
// quote-currency cash accordingly.

import { pairOf, money } from "./units.js";
import { garmanKohlhagen, OPTION_CP } from "./options.js";
import { checkBuyingPower } from "./buyingPower.js";

export const LOT_STATUS = ["open", "closed", "expired"];
export const YEAR_MS = 365 * 24 * 3600 * 1000;

export class OptionBook {
  constructor({ quoteStore, portfolioStore, optionSpreadFraction = 0.02, optionMarginFraction = 0,
    equityOf = null, now = () => Date.now() }) {
    this.quotes = quoteStore;
    this.portfolios = portfolioStore;
    this.spreadFraction = optionSpreadFraction;
    // Fraction of a short lot's notional carried as margin in the shared
    // buying-power rule — selling options is bounded, not free.
    this.marginFraction = optionMarginFraction;
    this.equityOf = equityOf;
    this.now = now;
  }

  // ticket: { symbol, cp: call|put, side: buy|sell, notionalAmount, strike,
  //           expiryMs, volatility, rateDomestic, rateForeign }
  // Notional unit is always the pair's base currency, stated on the order.
  // Returns the option order record (filled or rejected).
  placeOptionOrder(ticket) {
    const pf = this.portfolios.active();
    if (!pf) throw new Error("no portfolio selected — create one first");
    const order = this._newOptionOrder(pf, ticket); // validates and appends

    const gate = this.quotes.fillEligibility(order.symbol, this.now());
    if (!gate.tradable) {
      this._reject(pf, order, `fill_suspended: ${gate.reasons.join(", ")}`);
    } else {
      const q = this.quotes.latest(order.symbol);
      this._fill(pf, order, q, null);
    }

    this.portfolios.update(pf.id, () => {});
    return order;
  }

  // Close an open lot at the current model price (long sells at model bid,
  // short buys back at model ask). Records an offsetting option order, so the
  // full history — including a rejected close attempt — is retained.
  closeLot(lotId) {
    const pf = this.portfolios.active();
    if (!pf) throw new Error("no portfolio selected");
    const lot = pf.optionLots.find((l) => l.lotId === lotId);
    if (!lot) throw new Error(`unknown option lot: ${lotId}`);
    if (lot.status !== "open") throw new Error(`lot ${lotId} is ${lot.status}; only open lots can be closed`);

    const order = this._newOptionOrder(pf, {
      symbol: lot.symbol,
      cp: lot.cp,
      side: lot.qtySign > 0 ? "sell" : "buy",
      notionalAmount: lot.notional.amount,
      strike: lot.strike,
      expiryMs: lot.expiryMs,
      volatility: lot.volatility,
      rateDomestic: lot.rateDomestic,
      rateForeign: lot.rateForeign,
    });
    order.closesLotId = lot.lotId;

    const gate = this.quotes.fillEligibility(order.symbol, this.now());
    if (!gate.tradable) {
      this._reject(pf, order, `fill_suspended: ${gate.reasons.join(", ")}`);
    } else {
      const q = this.quotes.latest(order.symbol);
      this._fill(pf, order, q, lot);
    }

    this.portfolios.update(pf.id, () => {});
    return order;
  }

  // Settle open lots whose expiry has passed, across all portfolios. Payoff is
  // intrinsic at the last known spot mid — model-derived, never fabricated: a
  // lot with no quote at all stays open (awaiting a quote) rather than being
  // settled against an invented price. Settlement writes a blotter fill, so it
  // obeys the same fill-suspension rule as every simulated fill: it happens
  // only while the fill gate passes for the lot's symbol (open market, live
  // feed, fresh quote). An expired lot whose quote is stale or whose feed is
  // down stays open past expiry and settles on the first fresh quote after
  // expiry. Returns the settlement fills.
  settleExpired(nowMs = this.now()) {
    const fills = [];
    for (const pf of this.portfolios.allPortfolios()) {
      let changed = false;
      for (const lot of pf.optionLots) {
        if (lot.status !== "open" || lot.expiryMs > nowMs) continue;
        const q = this.quotes.latest(lot.symbol);
        if (!q) continue; // no observed price exists; do not invent one
        const gate = this.quotes.fillEligibility(lot.symbol, nowMs);
        if (!gate.tradable) continue; // deferred until a fresh, tradable quote
        const fill = this._settleLot(pf, lot, q, nowMs);
        fills.push(fill);
        changed = true;
      }
      if (changed) this.portfolios.update(pf.id, () => {});
    }
    return fills;
  }

  // Current model mark of a lot: price per unit, value, unrealized P&L and
  // position Greeks scaled by notional. Uses the retained last quote (display
  // only — no fill happens here). Returns { ok: false, reason } without a quote.
  markLot(lot, nowMs = this.now()) {
    const q = this.quotes.latest(lot.symbol);
    if (!q) return { ok: false, reason: "no_quote" };
    const pair = pairOf(lot.symbol);
    const T = Math.max(0, (lot.expiryMs - nowMs) / YEAR_MS);
    const gk = garmanKohlhagen({
      cp: lot.cp, spot: q.mid, strike: lot.strike, yearsToExpiry: T,
      volatility: lot.volatility, rateDomestic: lot.rateDomestic, rateForeign: lot.rateForeign,
    });
    const n = lot.notional.amount;
    const s = lot.qtySign;
    return {
      ok: true,
      modelDerived: true,
      pricePerUnit: gk.price,
      valueQuote: gk.price * n,
      unrealizedPnlQuote: s * (gk.price - lot.openPricePerUnit) * n,
      deltaBase: s * gk.delta * n,                 // in base currency units
      gammaBase: s * gk.gamma * n,                 // dDelta per 1.00 spot move
      vegaQuotePerVolPt: s * gk.vega * n / 100,    // quote ccy per +1 vol point
      thetaQuotePerDay: s * gk.theta * n / 365,    // quote ccy per day
      spot: q.mid,
      quoteTimestampMs: q.timestampMs,
      yearsToExpiry: T,
      priceCurrency: pair.quote,
    };
  }

  // Aggregate signed delta per pair, in base currency units: the spot position
  // contributes its net quantity (delta 1), each open option lot its GK delta
  // times notional. Lots without a quote are unpriceable and flagged rather
  // than silently dropped or guessed.
  aggregateDelta(pf, nowMs = this.now()) {
    const out = {};
    for (const [symbol, pos] of Object.entries(pf.positions)) {
      out[symbol] = out[symbol] ?? { spotDeltaBase: 0, optionsDeltaBase: 0, unpricedLots: 0 };
      out[symbol].spotDeltaBase += pos.qtyBase;
    }
    for (const lot of pf.optionLots) {
      if (lot.status !== "open") continue;
      out[lot.symbol] = out[lot.symbol] ?? { spotDeltaBase: 0, optionsDeltaBase: 0, unpricedLots: 0 };
      const mark = this.markLot(lot, nowMs);
      if (!mark.ok) { out[lot.symbol].unpricedLots += 1; continue; }
      out[lot.symbol].optionsDeltaBase += mark.deltaBase;
    }
    for (const v of Object.values(out)) {
      v.netDeltaBase = v.spotDeltaBase + v.optionsDeltaBase;
    }
    return out;
  }

  // The spot trade that would flatten the pair's net delta right now, or null
  // when already flat / unpriceable. Pure computation — execution stays with
  // the spot ExecutionEngine so the fill gate and blotter rules are identical.
  hedgeSuggestion(pf, symbol, nowMs = this.now()) {
    const agg = this.aggregateDelta(pf, nowMs)[symbol];
    if (!agg || agg.unpricedLots > 0) return null;
    // Below half a base unit the residual (e.g. gamma drift between the mark
    // and the hedge fill) is display noise, not an exposure to hedge.
    if (Math.abs(agg.netDeltaBase) < 0.5) return null;
    const pair = pairOf(symbol);
    return {
      symbol,
      side: agg.netDeltaBase > 0 ? "sell" : "buy",
      qtyAmount: Math.abs(agg.netDeltaBase),
      qtyCurrency: pair.base,
      netDeltaBeforeBase: agg.netDeltaBase,
    };
  }

  _newOptionOrder(pf, ticket) {
    const pair = pairOf(ticket.symbol); // throws on unknown instrument
    if (!OPTION_CP.includes(ticket.cp)) throw new Error(`bad option type: ${ticket.cp}`);
    if (!["buy", "sell"].includes(ticket.side)) throw new Error(`bad side: ${ticket.side}`);
    if (!(typeof ticket.notionalAmount === "number" && Number.isFinite(ticket.notionalAmount)
      && ticket.notionalAmount > 0)) {
      throw new Error(`notional must be a positive number, got ${ticket.notionalAmount}`);
    }
    if (!(typeof ticket.strike === "number" && Number.isFinite(ticket.strike) && ticket.strike > 0)) {
      throw new Error(`strike must be a positive number, got ${ticket.strike}`);
    }
    if (!(typeof ticket.expiryMs === "number" && Number.isFinite(ticket.expiryMs)
      && ticket.expiryMs > this.now())) {
      throw new Error("expiry must be in the future");
    }
    if (!(typeof ticket.volatility === "number" && Number.isFinite(ticket.volatility)
      && ticket.volatility > 0)) {
      throw new Error(`volatility must be positive, got ${ticket.volatility}`);
    }
    if (!Number.isFinite(ticket.rateDomestic) || !Number.isFinite(ticket.rateForeign)) {
      throw new Error("domestic and foreign rate assumptions are required");
    }
    const order = {
      orderId: `xo${++pf.seq.optionOrder}`,
      kind: "option",
      createdAtMs: this.now(),
      symbol: pair.symbol,
      cp: ticket.cp,
      side: ticket.side,
      notional: money(ticket.notionalAmount, pair.base), // explicit currency unit
      strike: ticket.strike,
      expiryMs: ticket.expiryMs,
      volatility: ticket.volatility,
      rateDomestic: ticket.rateDomestic,
      rateForeign: ticket.rateForeign,
      closesLotId: null,
      status: "pending",
      statusReason: null,
      closedAtMs: null,
      fillId: null,
    };
    pf.optionOrders.push(order);
    return order;
  }

  // The pair's current net delta for the buying-power rule's delta test, or
  // unprovable when any open lot has no quote to price its delta.
  _netDelta(pf, symbol) {
    const agg = this.aggregateDelta(pf, this.now())[symbol];
    if (!agg || agg.unpricedLots > 0) return { ok: false };
    return { ok: true, netDeltaBase: agg.netDeltaBase };
  }

  // Fill an option order at the model price. When closingLot is set the fill
  // closes that lot and realizes P&L; otherwise it opens a new lot.
  _fill(pf, order, quote, closingLot) {
    const pair = pairOf(order.symbol);
    const T = (order.expiryMs - this.now()) / YEAR_MS;
    const gk = garmanKohlhagen({
      cp: order.cp, spot: quote.mid, strike: order.strike, yearsToExpiry: T,
      volatility: order.volatility, rateDomestic: order.rateDomestic, rateForeign: order.rateForeign,
    });
    const half = gk.price * this.spreadFraction / 2;
    const modelBid = gk.price - half;
    const modelAsk = gk.price + half;
    const execPrice = order.side === "buy" ? modelAsk : modelBid;
    const premiumTotal = execPrice * order.notional.amount;

    // The same admission rule as spot. The premium's cash effect can borrow
    // (a premium payable in a currency the portfolio does not hold borrows it
    // instead of being refused outright); a sale that opens a short lot also
    // adds optionMarginFraction x notional of margin, so option sales are
    // bounded. An opening short sale never qualifies for the risk-off or
    // delta-flattening admissions — it always faces the margin/equity test,
    // even when it shrinks the pair's net delta or its premium repays
    // borrowing, and that premium is not credited against the requirement.
    // Closing a short releases its margin and removes its delta, so
    // risk-reducing closes stay possible even over the limit.
    const signedPremium = (order.side === "buy" ? -1 : 1) * premiumTotal;
    const openingShort = !closingLot && order.side === "sell";
    const closingShort = closingLot && closingLot.qtySign < 0;
    const marginDelta = openingShort
      ? { currency: pair.base, amount: order.notional.amount }
      : closingShort
        ? { currency: pair.base, amount: -closingLot.notional.amount }
        : null;
    // Signed delta change of this fill: the new lot's contribution when
    // opening, the removed contribution when closing.
    const deltaSign = closingLot ? -closingLot.qtySign : (order.side === "buy" ? 1 : -1);
    const bp = checkBuyingPower(pf, { [pair.quote]: signedPremium },
      this.quotes, this.now(), this.equityOf, {
        marginFraction: this.marginFraction,
        marginDelta,
        deltaEffect: { symbol: order.symbol, deltaBase: deltaSign * gk.delta * order.notional.amount },
        netDeltaOf: (pfArg, symbol) => this._netDelta(pfArg, symbol),
        opensShortLot: openingShort,
      });
    if (!bp.ok) {
      const what = closingLot ? "buy-back premium" : "premium";
      this._reject(pf, order, bp.reason === "insufficient_funds"
        ? `insufficient_funds: ${what} would require ${bp.requirement.toFixed(2)} ${bp.currency}`
          + ` (borrowed ${bp.borrowed.toFixed(2)} + short-option margin ${bp.margin.toFixed(2)})`
          + ` against equity ${bp.equity.toFixed(2)} ${bp.currency}`
        : `buying_power_unprovable: ${bp.detail}`);
      return null;
    }

    let realizedPnlQuote = 0;
    let lot = closingLot;
    if (closingLot) {
      // Close: cash returns at the exit premium; P&L against the open premium.
      realizedPnlQuote = closingLot.qtySign * (execPrice - closingLot.openPricePerUnit)
        * closingLot.notional.amount;
      pf.cash[pair.quote] += closingLot.qtySign * premiumTotal;
      closingLot.status = "closed";
      closingLot.closedAtMs = this.now();
      closingLot.closePricePerUnit = execPrice;
      closingLot.realizedPnlQuote = realizedPnlQuote;
    } else {
      // Open: buyer pays the ask-side premium, seller receives the bid-side.
      pf.cash[pair.quote] += (order.side === "buy" ? -1 : 1) * premiumTotal;
      lot = {
        lotId: `xl${++pf.seq.lot}`,
        symbol: order.symbol,
        cp: order.cp,
        qtySign: order.side === "buy" ? 1 : -1, // long / short
        notional: money(order.notional.amount, order.notional.currency),
        strike: order.strike,
        expiryMs: order.expiryMs,
        volatility: order.volatility,
        rateDomestic: order.rateDomestic,
        rateForeign: order.rateForeign,
        openPricePerUnit: execPrice,
        openModelMid: gk.price,
        openedAtMs: this.now(),
        status: "open",
        closedAtMs: null,
        closePricePerUnit: null,
        realizedPnlQuote: null,
      };
      pf.optionLots.push(lot);
    }

    const fill = Object.freeze({
      fillId: `f${++pf.seq.fill}`,
      orderId: order.orderId,
      portfolioId: pf.id,
      kind: "option",
      modelDerived: true,
      filledAtMs: this.now(),
      symbol: order.symbol,
      side: order.side,
      cp: order.cp,
      lotId: lot.lotId,
      qty: money(order.notional.amount, order.notional.currency),
      price: execPrice,
      priceCurrency: pair.quote,
      premiumTotal: money(premiumTotal, pair.quote),
      quote: Object.freeze({
        bid: quote.bid, ask: quote.ask, mid: quote.mid,
        source: quote.source, timestampMs: quote.timestampMs,
      }),
      model: Object.freeze({
        name: "Garman-Kohlhagen",
        spot: quote.mid, strike: order.strike, yearsToExpiry: T,
        volatility: order.volatility,
        rateDomestic: order.rateDomestic, rateForeign: order.rateForeign,
        modelMid: gk.price, modelBid, modelAsk, spreadFraction: this.spreadFraction,
      }),
      realizedPnlQuote,
    });
    pf.blotter.push(fill);
    order.status = "filled";
    order.statusReason = null;
    order.closedAtMs = fill.filledAtMs;
    order.fillId = fill.fillId;
    order.lotId = lot.lotId;
    return fill;
  }

  _settleLot(pf, lot, quote, nowMs) {
    const pair = pairOf(lot.symbol);
    const intrinsic = lot.cp === "call"
      ? Math.max(quote.mid - lot.strike, 0)
      : Math.max(lot.strike - quote.mid, 0);
    const payoff = intrinsic * lot.notional.amount;
    const realizedPnlQuote = lot.qtySign > 0
      ? payoff - lot.openPricePerUnit * lot.notional.amount
      : lot.openPricePerUnit * lot.notional.amount - payoff;
    pf.cash[pair.quote] += lot.qtySign * payoff;
    lot.status = "expired";
    lot.closedAtMs = nowMs;
    lot.closePricePerUnit = intrinsic;
    lot.realizedPnlQuote = realizedPnlQuote;

    const fill = Object.freeze({
      fillId: `f${++pf.seq.fill}`,
      orderId: null,
      portfolioId: pf.id,
      kind: "option_expiry",
      modelDerived: true,
      filledAtMs: nowMs,
      symbol: lot.symbol,
      side: lot.qtySign > 0 ? "sell" : "buy", // cash settlement direction
      cp: lot.cp,
      lotId: lot.lotId,
      qty: money(lot.notional.amount, lot.notional.currency),
      price: intrinsic,
      priceCurrency: pair.quote,
      premiumTotal: money(payoff, pair.quote),
      quote: Object.freeze({
        bid: quote.bid, ask: quote.ask, mid: quote.mid,
        source: quote.source, timestampMs: quote.timestampMs,
      }),
      model: Object.freeze({
        name: "intrinsic-at-expiry",
        spot: quote.mid, strike: lot.strike, yearsToExpiry: 0,
        volatility: lot.volatility,
        rateDomestic: lot.rateDomestic, rateForeign: lot.rateForeign,
        modelMid: intrinsic, modelBid: intrinsic, modelAsk: intrinsic, spreadFraction: 0,
      }),
      realizedPnlQuote,
    });
    pf.blotter.push(fill);
    return fill;
  }

  _reject(pf, order, reason) {
    order.status = "rejected";
    order.statusReason = reason;
    order.closedAtMs = this.now();
  }
}
