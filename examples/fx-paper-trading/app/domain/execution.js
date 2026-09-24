// Spot execution: order tickets, the fill rules from the brief, and the
// enduring blotter. The engine consumes the quote store's fillEligibility gate
// (the single definition of "may a simulated fill happen now") so execution
// and the market watch never disagree about suspension.
//
// Fill rules:
//  - market buy fills at the current ask, market sell at the current bid;
//  - a market order while the gate is closed is rejected (never silently
//    queued) with the gate's reasons recorded on the order;
//  - a limit order rests as pending and fills only when an incoming quote
//    reaches its price — a buy at the available ask when ask <= limit, a sell
//    at the available bid when bid >= limit — and only while the gate is open
//    for that symbol at that moment;
//  - buying power (app/domain/buyingPower.js) is one admission rule for both
//    sides: the total requirement — borrowed cash plus short-option margin,
//    valued in the reporting currency at observed mids — must not exceed the
//    portfolio's equity for new risk, while fills that do not increase the
//    requirement or that flatten the pair's net delta without flipping its
//    sign (partial closes, exact-flat risk-view hedges) are always admitted.
//    The rule is checked again at fill time for resting limits;
//  - every fill is written to the append-only blotter with the exact quote
//    (bid/ask/source/timestamp) it used, then frozen.
//
// Pending limit orders are processed for every portfolio on each incoming
// tick, not just the selected one — quotes are global, so a resting order
// fills regardless of which portfolio is on screen.

import { pairOf, money } from "./units.js";
import { ORDER_SIDES, ORDER_TYPES, applyFillToPortfolio } from "./portfolio.js";
import { checkBuyingPower } from "./buyingPower.js";

export class ExecutionEngine {
  // equityOf: optional (pf, nowMs) -> { ok, equity: Money } | { ok: false, missing };
  // the app injects computeEquity so open option lots count toward equity.
  // optionMarginFraction / netDeltaOf feed the shared buying-power rule: the
  // margin on open short option lots counts toward the requirement, and the
  // pair's net delta lets delta-flattening fills (hedges that do not flip the
  // exposure's sign) in when they borrow.
  constructor({ quoteStore, portfolioStore, equityOf = null, optionMarginFraction = 0,
    netDeltaOf = null, now = () => Date.now() }) {
    this.quotes = quoteStore;
    this.portfolios = portfolioStore;
    this.equityOf = equityOf;
    this.optionMarginFraction = optionMarginFraction;
    this.netDeltaOf = netDeltaOf;
    this.now = now;
  }

  // ticket: { symbol, side: buy|sell, type: market|limit, qtyAmount, limitPrice? }
  // Quantity unit is always the pair's base currency, stated on the order.
  // Returns the order record (whatever its terminal or resting status).
  placeOrder(ticket) {
    const pf = this.portfolios.active();
    if (!pf) throw new Error("no portfolio selected — create one first");
    const order = this._newOrder(pf, ticket); // validates and appends as pending

    if (order.type === "market") {
      const gate = this.quotes.fillEligibility(order.symbol, this.now());
      if (!gate.tradable) {
        this._reject(pf, order, `fill_suspended: ${gate.reasons.join(", ")}`);
      } else {
        const q = this.quotes.latest(order.symbol);
        this._tryFill(pf, order, q, order.side === "buy" ? q.ask : q.bid);
      }
    } else {
      // A resting limit that is already reachable fills immediately.
      this._tryFillLimit(pf, order);
    }

    this.portfolios.update(pf.id, () => {});
    return order;
  }

  cancelOrder(orderId) {
    const pf = this.portfolios.active();
    if (!pf) throw new Error("no portfolio selected");
    const order = pf.orders.find((o) => o.orderId === orderId);
    if (!order) throw new Error(`unknown order: ${orderId}`);
    if (order.status !== "pending") {
      throw new Error(`order ${orderId} is ${order.status}; only pending orders can be cancelled`);
    }
    order.status = "cancelled";
    order.statusReason = "cancelled by user";
    order.closedAtMs = this.now();
    this.portfolios.update(pf.id, () => {});
    return order;
  }

  // Called on every ingested tick: work resting limit orders for the tick's
  // symbol across all portfolios. Returns the fills that happened.
  processSymbol(symbol) {
    const fills = [];
    for (const pf of this.portfolios.allPortfolios()) {
      let changed = false;
      for (const order of pf.orders) {
        if (order.status === "pending" && order.type === "limit" && order.symbol === symbol) {
          const fill = this._tryFillLimit(pf, order);
          if (fill) { fills.push(fill); changed = true; }
          if (order.status !== "pending") changed = true;
        }
      }
      if (changed) this.portfolios.update(pf.id, () => {});
    }
    return fills;
  }

  _newOrder(pf, ticket) {
    const pair = pairOf(ticket.symbol); // throws on unknown instrument
    if (!ORDER_SIDES.includes(ticket.side)) throw new Error(`bad side: ${ticket.side}`);
    if (!ORDER_TYPES.includes(ticket.type)) throw new Error(`bad order type: ${ticket.type}`);
    if (!(typeof ticket.qtyAmount === "number" && Number.isFinite(ticket.qtyAmount)
      && ticket.qtyAmount > 0)) {
      throw new Error(`quantity must be a positive number, got ${ticket.qtyAmount}`);
    }
    if (ticket.type === "limit"
      && !(typeof ticket.limitPrice === "number" && Number.isFinite(ticket.limitPrice)
        && ticket.limitPrice > 0)) {
      throw new Error(`limit price must be a positive number, got ${ticket.limitPrice}`);
    }
    const order = {
      orderId: `o${++pf.seq.order}`,
      createdAtMs: this.now(),
      symbol: pair.symbol,
      side: ticket.side,
      type: ticket.type,
      qty: money(ticket.qtyAmount, pair.base), // explicit currency unit
      limitPrice: ticket.type === "limit" ? ticket.limitPrice : null,
      origin: ticket.origin ?? null, // e.g. "delta-hedge" for risk-view hedges
      status: "pending",
      statusReason: null,
      closedAtMs: null,
      fillId: null,
    };
    pf.orders.push(order);
    return order;
  }

  _tryFillLimit(pf, order) {
    const q = this.quotes.latest(order.symbol);
    if (!q || q.marketState !== "open") return null;
    const reachable = order.side === "buy"
      ? q.ask <= order.limitPrice
      : q.bid >= order.limitPrice;
    if (!reachable) return null;
    const gate = this.quotes.fillEligibility(order.symbol, this.now());
    if (!gate.tradable) return null; // stays pending until a fresh open quote
    return this._tryFill(pf, order, q, order.side === "buy" ? q.ask : q.bid);
  }

  _tryFill(pf, order, quote, price) {
    const pair = pairOf(order.symbol);
    // One admission rule for both sides: the fill's cash effect plus the
    // existing short-option margin must fit within equity for new risk;
    // fills that reduce the requirement or flatten the pair's net delta
    // without flipping its sign (partial closes, delta hedges) are always
    // admitted.
    const signedQty = (order.side === "buy" ? 1 : -1) * order.qty.amount;
    const bp = checkBuyingPower(pf, {
      [pair.base]: signedQty,
      [pair.quote]: -signedQty * price,
    }, this.quotes, this.now(), this.equityOf, {
      marginFraction: this.optionMarginFraction,
      deltaEffect: { symbol: order.symbol, deltaBase: signedQty },
      netDeltaOf: this.netDeltaOf,
    });
    if (!bp.ok) {
      this._reject(pf, order, bp.reason === "insufficient_funds"
        ? `insufficient_funds: fill would require ${bp.requirement.toFixed(2)} ${bp.currency}`
          + ` (borrowed ${bp.borrowed.toFixed(2)} + short-option margin ${bp.margin.toFixed(2)})`
          + ` against equity ${bp.equity.toFixed(2)} ${bp.currency}`
        : `buying_power_unprovable: ${bp.detail}`);
      return null;
    }
    const realizedPnlQuote = applyFillToPortfolio(pf, {
      symbol: order.symbol, side: order.side, qtyAmount: order.qty.amount, price,
    });
    const fill = Object.freeze({
      fillId: `f${++pf.seq.fill}`,
      orderId: order.orderId,
      portfolioId: pf.id,
      kind: "spot",
      filledAtMs: this.now(),
      symbol: order.symbol,
      side: order.side,
      type: order.type,
      origin: order.origin ?? null,
      qty: money(order.qty.amount, order.qty.currency),
      price,
      priceCurrency: pair.quote,
      quote: Object.freeze({
        bid: quote.bid, ask: quote.ask, source: quote.source, timestampMs: quote.timestampMs,
      }),
      realizedPnlQuote,
    });
    pf.blotter.push(fill);
    order.status = "filled";
    order.statusReason = null;
    order.closedAtMs = fill.filledAtMs;
    order.fillId = fill.fillId;
    return fill;
  }

  _reject(pf, order, reason) {
    order.status = "rejected";
    order.statusReason = reason;
    order.closedAtMs = this.now();
  }
}
