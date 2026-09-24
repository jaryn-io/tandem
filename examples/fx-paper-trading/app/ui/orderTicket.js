// Order ticket: market/limit buy/sell for the active portfolio. The ticket
// shows the fill gate of the selected symbol so a suspension is visible before
// submission; a market order submitted against a closed gate is still recorded
// as a rejected order rather than silently queued.

import { PAIRS } from "../domain/units.js";

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export class OrderTicket {
  // onChanged() is called after any placement attempt so panels re-render.
  constructor(root, { engine, quoteStore, symbols, onChanged }) {
    this.engine = engine;
    this.quotes = quoteStore;
    this.onChanged = onChanged;

    const form = el("div", "ticket-form");

    this.symbolSel = el("select");
    for (const s of symbols) this.symbolSel.appendChild(new Option(s, s));

    this.sideSel = el("select");
    this.sideSel.append(new Option("Buy", "buy"), new Option("Sell", "sell"));

    this.typeSel = el("select");
    this.typeSel.append(new Option("Market", "market"), new Option("Limit", "limit"));

    this.qtyInput = el("input");
    this.qtyInput.type = "number";
    this.qtyInput.min = "0";
    this.qtyInput.step = "any";
    this.qtyInput.value = "10000";

    this.unitBadge = el("span", "badge badge-warn", PAIRS[this.symbolSel.value].base);

    this.limitInput = el("input");
    this.limitInput.type = "number";
    this.limitInput.min = "0";
    this.limitInput.step = "any";
    this.limitInput.placeholder = "limit price";
    this.limitLabel = el("label", null, "Limit price ");
    this.limitLabel.appendChild(this.limitInput);
    this.limitLabel.style.display = "none";

    this.submitBtn = el("button", null, "Place order");
    this.submitBtn.type = "button";

    this.gateBadge = el("span", "badge badge-warn", "awaiting quote");
    this.message = el("p", "hint", "");

    form.append(
      this._label("Instrument", this.symbolSel),
      this._label("Side", this.sideSel),
      this._label("Type", this.typeSel),
      this._label("Quantity", this.qtyInput),
      this.unitBadge,
      this.limitLabel,
      this.submitBtn,
      this.gateBadge,
    );
    root.append(form, this.message);

    this.symbolSel.addEventListener("change", () => this._sync());
    this.typeSel.addEventListener("change", () => this._sync());
    this.submitBtn.addEventListener("click", () => this._submit());
  }

  _label(text, input) {
    const l = el("label", null, text + " ");
    l.appendChild(input);
    return l;
  }

  _sync() {
    this.unitBadge.textContent = PAIRS[this.symbolSel.value].base;
    this.limitLabel.style.display = this.typeSel.value === "limit" ? "" : "none";
    this.renderGate();
  }

  renderGate(nowMs = Date.now()) {
    const gate = this.quotes.fillEligibility(this.symbolSel.value, nowMs);
    if (gate.tradable) {
      this.gateBadge.className = "badge badge-ok";
      this.gateBadge.textContent = "fills enabled";
    } else {
      this.gateBadge.className = "badge badge-bad";
      this.gateBadge.textContent = `fills suspended — ${gate.reasons.join(", ")}`;
    }
  }

  _submit() {
    this.message.className = "hint";
    try {
      const order = this.engine.placeOrder({
        symbol: this.symbolSel.value,
        side: this.sideSel.value,
        type: this.typeSel.value,
        qtyAmount: Number(this.qtyInput.value),
        limitPrice: this.typeSel.value === "limit" ? Number(this.limitInput.value) : null,
      });
      if (order.status === "filled") {
        this.message.textContent = `${order.orderId} filled (fill ${order.fillId}) — see blotter.`;
      } else if (order.status === "rejected") {
        this.message.className = "hint msg-bad";
        this.message.textContent = `${order.orderId} rejected: ${order.statusReason}`;
      } else {
        this.message.textContent = `${order.orderId} resting as pending limit.`;
      }
    } catch (err) {
      this.message.className = "hint msg-bad";
      this.message.textContent = err.message;
    }
    this.onChanged?.();
  }
}
