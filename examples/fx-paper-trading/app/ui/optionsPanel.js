// Options panel: ticket for simulated European vanilla options, open lots with
// live model marks and Greeks, and the option order history. Every price shown
// here is model-derived (Garman–Kohlhagen on the observed spot mid plus the
// user's assumptions) and is labelled as such; the assumed execution spread is
// always visible next to the model bid/ask.

import { PAIRS, formatMoney, money } from "../domain/units.js";
import { garmanKohlhagen } from "../domain/options.js";
import { YEAR_MS } from "../domain/optionBook.js";

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function fmtTs(ms) {
  return ms ? new Date(ms).toISOString().replace("T", " ").slice(0, 19) + "Z" : "—";
}

const STATUS_BADGE = {
  pending: "badge-warn",
  filled: "badge-ok",
  rejected: "badge-bad",
};

export class OptionsPanel {
  // roots: { ticket, lots, orders }; onChanged re-renders siblings.
  constructor(roots, { book, quoteStore, symbols, optionDefaults, onChanged }) {
    this.roots = roots;
    this.book = book;
    this.quotes = quoteStore;
    this.symbols = symbols;
    this.defaults = optionDefaults;
    this.onChanged = onChanged;
    this._buildTicket();
  }

  _label(text, input) {
    const l = el("label", null, text + " ");
    l.appendChild(input);
    return l;
  }

  _buildTicket() {
    const form = el("div", "ticket-form");

    this.symbolSel = el("select");
    for (const s of this.symbols) this.symbolSel.appendChild(new Option(s, s));

    this.cpSel = el("select");
    this.cpSel.append(new Option("Call", "call"), new Option("Put", "put"));

    this.sideSel = el("select");
    this.sideSel.append(new Option("Buy (long)", "buy"), new Option("Sell (short)", "sell"));

    this.notionalInput = el("input");
    this.notionalInput.type = "number";
    this.notionalInput.min = "0";
    this.notionalInput.step = "any";
    this.notionalInput.value = "100000";
    this.unitBadge = el("span", "badge badge-warn", PAIRS[this.symbolSel.value].base);

    this.strikeInput = el("input");
    this.strikeInput.type = "number";
    this.strikeInput.min = "0";
    this.strikeInput.step = "any";
    this.strikeInput.placeholder = "strike";
    const atmBtn = el("button", null, "ATM");
    atmBtn.type = "button";
    atmBtn.title = "Set strike to the current observed spot mid";
    atmBtn.addEventListener("click", () => {
      const q = this.quotes.latest(this.symbolSel.value);
      if (q) this.strikeInput.value = q.mid.toFixed(PAIRS[this.symbolSel.value].priceDecimals);
      this._preview();
    });

    this.expiryInput = el("input");
    this.expiryInput.type = "date";
    this.expiryInput.value = new Date(Date.now() + this.defaults.defaultExpiryDays * 86400_000)
      .toISOString().slice(0, 10);

    this.volInput = el("input");
    this.volInput.type = "number";
    this.volInput.min = "0";
    this.volInput.step = "any";
    this.volInput.value = (this.defaults.defaultVolatility * 100).toString();

    this.rdInput = el("input");
    this.rdInput.type = "number";
    this.rdInput.step = "any";
    this.rfInput = el("input");
    this.rfInput.type = "number";
    this.rfInput.step = "any";
    this._syncRates();

    this.submitBtn = el("button", null, "Trade option (model price)");
    this.submitBtn.type = "button";
    this.gateBadge = el("span", "badge badge-warn", "awaiting quote");

    form.append(
      this._label("Instrument", this.symbolSel),
      this._label("Type", this.cpSel),
      this._label("Side", this.sideSel),
      this._label("Notional", this.notionalInput),
      this.unitBadge,
      this._label("Strike", this.strikeInput),
      atmBtn,
      this._label("Expiry", this.expiryInput),
      this._label("Vol %", this.volInput),
      this._label("r(dom) %", this.rdInput),
      this._label("r(for) %", this.rfInput),
      this.submitBtn,
      this.gateBadge,
    );

    this.preview = el("p", "hint", "Model premium preview appears here.");
    this.message = el("p", "hint", "");
    this.roots.ticket.append(form, this.preview, this.message);

    this.symbolSel.addEventListener("change", () => { this._syncRates(); this._sync(); });
    for (const inp of [this.cpSel, this.sideSel, this.notionalInput, this.strikeInput,
      this.expiryInput, this.volInput, this.rdInput, this.rfInput]) {
      inp.addEventListener("input", () => this._preview());
      inp.addEventListener("change", () => this._preview());
    }
    this.submitBtn.addEventListener("click", () => this._submit());
  }

  _syncRates() {
    const pair = PAIRS[this.symbolSel.value];
    this.rdInput.value = (this.defaults.defaultRates[pair.quote] * 100).toString();
    this.rfInput.value = (this.defaults.defaultRates[pair.base] * 100).toString();
    this.unitBadge.textContent = pair.base;
  }

  _sync() {
    this.unitBadge.textContent = PAIRS[this.symbolSel.value].base;
    this.renderGate();
    this._preview();
  }

  _readTicket() {
    const expiryMs = Date.parse(`${this.expiryInput.value}T17:00:00Z`); // 5pm UTC convention
    return {
      symbol: this.symbolSel.value,
      cp: this.cpSel.value,
      side: this.sideSel.value,
      notionalAmount: Number(this.notionalInput.value),
      strike: Number(this.strikeInput.value),
      expiryMs,
      volatility: Number(this.volInput.value) / 100,
      rateDomestic: Number(this.rdInput.value) / 100,
      rateForeign: Number(this.rfInput.value) / 100,
    };
  }

  // Model premium preview: mid, the assumed execution spread and the resulting
  // model bid/ask — always labelled model-derived, never as an observed quote.
  _preview(nowMs = Date.now()) {
    const pair = PAIRS[this.symbolSel.value];
    const q = this.quotes.latest(this.symbolSel.value);
    if (!q) {
      this.preview.textContent = "No spot quote yet — model premium cannot be computed.";
      return;
    }
    let t;
    try { t = this._readTicket(); } catch { this.preview.textContent = ""; return; }
    const T = (t.expiryMs - nowMs) / YEAR_MS;
    if (!(T > 0) || !(t.strike > 0) || !(t.volatility > 0)
      || !Number.isFinite(t.rateDomestic) || !Number.isFinite(t.rateForeign)) {
      this.preview.textContent = "Enter strike, future expiry, vol and rate assumptions for a model preview.";
      return;
    }
    const gk = garmanKohlhagen({
      cp: t.cp, spot: q.mid, strike: t.strike, yearsToExpiry: T,
      volatility: t.volatility, rateDomestic: t.rateDomestic, rateForeign: t.rateForeign,
    });
    const half = gk.price * this.book.spreadFraction / 2;
    const d = pair.priceDecimals + 2; // premiums need more decimals than spot
    this.preview.textContent =
      `Model-derived (Garman–Kohlhagen, not an observed quote): mid ${gk.price.toFixed(d)} ${pair.quote}`
      + ` per ${pair.base} · assumed spread ${(this.book.spreadFraction * 100).toFixed(1)}% of premium`
      + ` → model bid ${(gk.price - half).toFixed(d)} / model ask ${(gk.price + half).toFixed(d)}`
      + ` · total at model ask ≈ ${((gk.price + half) * (t.notionalAmount || 0)).toFixed(2)} ${pair.quote}`
      + ` · delta ${gk.delta.toFixed(4)} per ${pair.base}`;
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
      const order = this.book.placeOptionOrder(this._readTicket());
      if (order.status === "filled") {
        this.message.textContent =
          `${order.orderId} filled at model-derived price (fill ${order.fillId}) — see blotter and lots below.`;
      } else {
        this.message.className = "hint msg-bad";
        this.message.textContent = `${order.orderId} rejected: ${order.statusReason}`;
      }
    } catch (err) {
      this.message.className = "hint msg-bad";
      this.message.textContent = err.message;
    }
    this.onChanged?.();
  }

  render(nowMs = Date.now()) {
    this.renderGate(nowMs);
    this._preview(nowMs);
    this._renderLots(nowMs);
    this._renderOrders();
  }

  _renderLots(nowMs) {
    const tbody = this.roots.lots.querySelector("tbody");
    tbody.textContent = "";
    const pf = this.book.portfolios.active();
    if (!pf) { this._emptyRow(tbody, 12, "No portfolio selected — create one above."); return; }

    const lots = [...pf.optionLots].reverse(); // newest first
    for (const lot of lots) {
      const pair = PAIRS[lot.symbol];
      const tr = el("tr");
      if (lot.status !== "open") tr.className = "lot-inactive";
      const daysLeft = Math.max(0, (lot.expiryMs - nowMs) / 86400_000);
      const d = pair.priceDecimals + 2;

      let markCell = "—";
      let pnlCell = "—";
      let deltaCell = "—";
      let greeksCell = "—";
      if (lot.status === "open") {
        const mark = this.book.markLot(lot, nowMs);
        if (mark.ok) {
          markCell = `${mark.pricePerUnit.toFixed(d)} ${pair.quote} (model, spot ${mark.spot.toFixed(pair.priceDecimals)}`
            + ` @ ${new Date(mark.quoteTimestampMs).toISOString().slice(11, 19)}Z)`;
          pnlCell = `${mark.unrealizedPnlQuote.toFixed(2)} ${pair.quote} (model)`;
          deltaCell = `${mark.deltaBase >= 0 ? "+" : ""}${Math.round(mark.deltaBase).toLocaleString("en-US")} ${pair.base}`;
          greeksCell = `Γ ${mark.gammaBase.toFixed(0)} · V ${mark.vegaQuotePerVolPt.toFixed(2)} ${pair.quote}/vol pt`
            + ` · Θ ${mark.thetaQuotePerDay.toFixed(2)} ${pair.quote}/day`;
        } else {
          markCell = `no quote — unpriced (${mark.reason})`;
        }
      } else if (lot.closePricePerUnit !== null) {
        markCell = `${lot.closePricePerUnit.toFixed(d)} ${pair.quote} (${lot.status})`;
        pnlCell = `${lot.realizedPnlQuote.toFixed(2)} ${pair.quote} realized`;
      }

      for (const cell of [
        lot.lotId,
        `${lot.cp} ${lot.qtySign > 0 ? "long" : "short"}`,
        `${lot.notional.amount.toLocaleString("en-US")} ${lot.notional.currency}`,
        lot.strike.toFixed(pair.priceDecimals),
        `${new Date(lot.expiryMs).toISOString().slice(0, 10)} (${daysLeft.toFixed(1)}d)`,
        `σ ${(lot.volatility * 100).toFixed(1)}% · rd ${(lot.rateDomestic * 100).toFixed(2)}% · rf ${(lot.rateForeign * 100).toFixed(2)}%`,
        `${lot.openPricePerUnit.toFixed(d)} ${pair.quote}`,
        markCell, pnlCell, deltaCell, greeksCell,
      ]) tr.appendChild(el("td", null, cell));

      const actionTd = el("td");
      if (lot.status === "open") {
        const btn = el("button", null, "Close at model price");
        btn.type = "button";
        btn.addEventListener("click", () => {
          this.message.className = "hint";
          try {
            const order = this.book.closeLot(lot.lotId);
            this.message.textContent = order.status === "filled"
              ? `${lot.lotId} closed at model-derived price (fill ${order.fillId}).`
              : `close rejected: ${order.statusReason}`;
            if (order.status !== "filled") this.message.className = "hint msg-bad";
          } catch (err) {
            this.message.className = "hint msg-bad";
            this.message.textContent = err.message;
          }
          this.onChanged?.();
        });
        actionTd.appendChild(btn);
      } else {
        actionTd.appendChild(el("span", `badge ${lot.status === "expired" ? "badge-warn" : "badge-ok"}`, lot.status));
      }
      tr.appendChild(actionTd);
      tbody.appendChild(tr);
    }
    if (!tbody.children.length) this._emptyRow(tbody, 12, "No option lots yet.");
  }

  _renderOrders() {
    const tbody = this.roots.orders.querySelector("tbody");
    tbody.textContent = "";
    const pf = this.book.portfolios.active();
    if (!pf) { this._emptyRow(tbody, 9, "No portfolio selected — create one above."); return; }
    const orders = [...pf.optionOrders].reverse();
    for (const o of orders) {
      const pair = PAIRS[o.symbol];
      const tr = el("tr");
      for (const cell of [
        o.orderId, fmtTs(o.createdAtMs),
        `${o.cp} ${o.side}${o.closesLotId ? ` (close ${o.closesLotId})` : ""}`,
        o.symbol,
        `${o.notional.amount.toLocaleString("en-US")} ${o.notional.currency}`,
        `${o.strike.toFixed(pair.priceDecimals)} · exp ${new Date(o.expiryMs).toISOString().slice(0, 10)}`,
      ]) tr.appendChild(el("td", null, cell));
      const statusTd = el("td");
      statusTd.appendChild(el("span", `badge ${STATUS_BADGE[o.status] ?? "badge-warn"}`, o.status));
      tr.appendChild(statusTd);
      tr.appendChild(el("td", null, o.statusReason ?? (o.fillId ? `fill ${o.fillId}` : "—")));
      tr.appendChild(el("td", null,
        `σ ${(o.volatility * 100).toFixed(1)}% rd ${(o.rateDomestic * 100).toFixed(2)}% rf ${(o.rateForeign * 100).toFixed(2)}%`));
      tbody.appendChild(tr);
    }
    if (!tbody.children.length) this._emptyRow(tbody, 9, "No option orders yet.");
  }

  _emptyRow(tbody, colSpan, text) {
    const tr = el("tr");
    const td = el("td", "hint", text);
    td.colSpan = colSpan;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}
