// Risk view: aggregate signed delta per pair (spot net quantity plus option GK
// deltas, in base currency units) and the simulated spot hedge. Hedging places
// an ordinary spot market order through the ExecutionEngine — the fill gate,
// fill price (ask/bid) and blotter rules are identical to the order ticket —
// tagged origin "delta-hedge" so its purpose stays visible in the history.

import { PAIRS } from "../domain/units.js";

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function fmtDelta(v, unit) {
  const sign = v > 0 ? "+" : "";
  return `${sign}${Math.round(v).toLocaleString("en-US")} ${unit}`;
}

export class RiskView {
  constructor(root, { book, engine, portfolioStore, quoteStore, onChanged }) {
    this.root = root;
    this.book = book;
    this.engine = engine;
    this.store = portfolioStore;
    this.quotes = quoteStore;
    this.onChanged = onChanged;

    this.table = el("table", "data-table");
    const thead = el("thead");
    const hr = el("tr");
    for (const h of ["Pair", "Spot delta", "Options delta", "Net delta", "Gate", "Hedge"]) {
      hr.appendChild(el("th", null, h));
    }
    thead.appendChild(hr);
    this.tbody = el("tbody");
    this.table.append(thead, this.tbody);
    this.message = el("p", "hint",
      "Delta is signed and expressed in the pair's base currency: +1,000,000 EUR means the portfolio"
      + " gains like a 1,000,000 EUR long spot position when EURUSD rises. Option deltas are"
      + " model-derived (Garman–Kohlhagen).");
    this.result = el("p", "hint", "");
    root.append(this.table, this.message, this.result);
  }

  render(nowMs = Date.now()) {
    this.tbody.textContent = "";
    const pf = this.store.active();
    if (!pf) {
      const tr = el("tr");
      const td = el("td", "hint", "No portfolio selected — create one above.");
      td.colSpan = 6;
      tr.appendChild(td);
      this.tbody.appendChild(tr);
      return;
    }
    const agg = this.book.aggregateDelta(pf, nowMs);
    const symbols = Object.keys(agg);
    if (!symbols.length) {
      const tr = el("tr");
      const td = el("td", "hint", "No exposure yet — delta appears after the first spot fill or option lot.");
      td.colSpan = 6;
      tr.appendChild(td);
      this.tbody.appendChild(tr);
      return;
    }
    for (const symbol of symbols.sort()) {
      const pair = PAIRS[symbol];
      const a = agg[symbol];
      const tr = el("tr");
      tr.appendChild(el("td", null, symbol));
      tr.appendChild(el("td", null, fmtDelta(a.spotDeltaBase, pair.base)));
      tr.appendChild(el("td", null,
        fmtDelta(a.optionsDeltaBase, pair.base)
        + (a.unpricedLots ? ` (${a.unpricedLots} lot(s) unpriced — no quote)` : " (model-derived)")));
      const netTd = el("td", a.netDeltaBase > 1e-9 ? "delta-pos" : a.netDeltaBase < -1e-9 ? "delta-neg" : null,
        fmtDelta(a.netDeltaBase, pair.base));
      tr.appendChild(netTd);

      const gate = this.quotes.fillEligibility(symbol, nowMs);
      const gateTd = el("td");
      gateTd.appendChild(el("span", `badge ${gate.tradable ? "badge-ok" : "badge-bad"}`,
        gate.tradable ? "fills enabled" : `suspended: ${gate.reasons.join(", ")}`));
      tr.appendChild(gateTd);

      const hedgeTd = el("td");
      const suggestion = this.book.hedgeSuggestion(pf, symbol, nowMs);
      if (suggestion) {
        const btn = el("button", null,
          `Hedge: ${suggestion.side} ${Math.round(suggestion.qtyAmount).toLocaleString("en-US")} ${suggestion.qtyCurrency}`);
        btn.type = "button";
        btn.title = "Place a spot market order that flattens the current net delta";
        btn.addEventListener("click", () => this._hedge(suggestion));
        hedgeTd.appendChild(btn);
      } else {
        hedgeTd.appendChild(el("span", "hint",
          a.unpricedLots ? "unavailable — unpriced lots" : "delta flat"));
      }
      tr.appendChild(hedgeTd);
      this.tbody.appendChild(tr);
    }
  }

  _hedge(suggestion) {
    this.result.className = "hint";
    try {
      const order = this.engine.placeOrder({
        symbol: suggestion.symbol,
        side: suggestion.side,
        type: "market",
        qtyAmount: suggestion.qtyAmount,
        origin: "delta-hedge",
      });
      if (order.status === "filled") {
        this.result.textContent =
          `Hedge filled (${order.fillId}): ${suggestion.side} ${Math.round(suggestion.qtyAmount).toLocaleString("en-US")}`
          + ` ${suggestion.qtyCurrency}. Net ${suggestion.symbol} delta before hedge was`
          + ` ${fmtDelta(suggestion.netDeltaBeforeBase, suggestion.qtyCurrency)}; the table above shows the new exposure.`;
      } else {
        this.result.className = "hint msg-bad";
        this.result.textContent = `Hedge order ${order.orderId} ${order.status}: ${order.statusReason}`;
      }
    } catch (err) {
      this.result.className = "hint msg-bad";
      this.result.textContent = err.message;
    }
    this.onChanged?.();
  }
}
