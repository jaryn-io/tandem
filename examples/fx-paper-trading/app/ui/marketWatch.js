// Market watch rendering: one card per instrument showing bid/ask/mid/spread,
// source, quote timestamp, live quote age, market state and the fill gate.

import { PAIRS } from "../domain/units.js";

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export class MarketWatch {
  constructor(root, quoteStore, symbols) {
    this.store = quoteStore;
    this.symbols = symbols;
    this.cards = new Map();
    for (const s of symbols) {
      const card = this._buildCard(s);
      this.cards.set(s, card);
      root.appendChild(card.root);
    }
  }

  _buildCard(symbol) {
    const pair = PAIRS[symbol];
    const root = el("article", "watch-card");
    root.dataset.symbol = symbol;

    const title = el("h3");
    title.append(el("span", null, symbol), el("span", "desc", ""));
    const prices = el("div", "prices");
    const bidB = el("div", "price-block");
    bidB.append(el("span", "label", "Bid"), el("span", "value bid", "—"));
    const askB = el("div", "price-block");
    askB.append(el("span", "label", "Ask"), el("span", "value ask", "—"));
    const spreadB = el("div", "price-block");
    spreadB.append(el("span", "label", "Spread"), el("span", "value", "—"));
    prices.append(bidB, askB, spreadB);

    const meta = el("div", "meta");
    const fields = {};
    for (const key of ["Source", "Quote time (UTC)", "Quote age", "Market"]) {
      const k = el("span", null, key);
      const v = el("b");
      meta.append(k, v);
      fields[key] = v;
    }
    const gate = el("div", "gate");
    const gateBadge = el("span", "badge badge-warn", "awaiting quote");
    gate.appendChild(gateBadge);

    root.append(title, prices, meta, gate);
    return {
      root, pair,
      desc: title.querySelector(".desc"),
      bid: bidB.querySelector(".value"),
      ask: askB.querySelector(".value"),
      spread: spreadB.querySelector(".value"),
      source: fields["Source"],
      quoteTime: fields["Quote time (UTC)"],
      quoteAge: fields["Quote age"],
      market: fields["Market"],
      gateBadge,
    };
  }

  // Re-render all cards. Called on every tick and once per second for age.
  render(nowMs = Date.now()) {
    for (const s of this.symbols) {
      const c = this.cards.get(s);
      const q = this.store.latest(s);
      if (!q) continue;

      c.desc.textContent = q.source;
      c.bid.textContent = q.bid.toFixed(c.pair.priceDecimals);
      c.ask.textContent = q.ask.toFixed(c.pair.priceDecimals);
      c.spread.textContent = q.spread.toFixed(c.pair.priceDecimals);
      c.bid.className = "value bid" + (q.direction === "DOWN" ? " dir-down" : q.direction === "UP" ? " dir-up" : "");
      c.source.textContent = q.source;
      c.quoteTime.textContent = new Date(q.timestampMs).toISOString().replace("T", " ").slice(0, 19) + "Z";
      const age = this.store.ageSeconds(s, nowMs);
      c.quoteAge.textContent = age === null ? "unknown (clock skew)" : `${age.toFixed(0)}s`;
      c.market.textContent = q.marketState;

      const gate = this.store.fillEligibility(s, nowMs);
      if (gate.tradable) {
        c.gateBadge.className = "badge badge-ok";
        c.gateBadge.textContent = "fills enabled";
      } else {
        c.gateBadge.className = "badge badge-bad";
        c.gateBadge.textContent = `fills suspended — ${gate.reasons.join(", ")}`;
      }
    }
  }
}

const STATE_BADGE = {
  connecting: "badge-warn",
  streaming: "badge-ok",
  polling: "badge-ok",
  reconnecting: "badge-warn",
  disconnected: "badge-bad",
  failed: "badge-bad",
};

export function renderConnectionState(elm, state, detail) {
  elm.className = `badge ${STATE_BADGE[state] ?? "badge-warn"}`;
  elm.textContent = state + (detail ? ` (${detail})` : "");
}
