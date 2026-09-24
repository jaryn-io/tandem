// Portfolio panel: selector + creation form, cash ledger, positions, orders
// (pending/filled/cancelled/rejected) and the enduring blotter. Rendering only;
// all state transitions go through the execution engine and portfolio store.

import { CURRENCIES, PAIRS, formatMoney, money } from "../domain/units.js";

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
  cancelled: "badge-warn",
  rejected: "badge-bad",
};

export class PortfolioPanel {
  // roots: { bar, positions, orders, blotter }; onChanged re-renders siblings.
  constructor(roots, { portfolioStore, engine, quoteStore, defaultReportingCurrency, defaultStartingEquity, onChanged }) {
    this.roots = roots;
    this.store = portfolioStore;
    this.engine = engine;
    this.quotes = quoteStore;
    this.defaultReportingCurrency = defaultReportingCurrency;
    this.defaultStartingEquity = defaultStartingEquity ?? 100000;
    this.onChanged = onChanged;
    this._buildBar();
  }

  _buildBar() {
    const bar = this.roots.bar;
    this.selector = el("select");
    this.selector.addEventListener("change", () => {
      if (this.selector.value) {
        this.store.select(this.selector.value);
        this.onChanged?.();
      }
    });

    this.nameInput = el("input");
    this.nameInput.placeholder = "portfolio name";
    this.equityInput = el("input");
    this.equityInput.type = "number";
    this.equityInput.min = "0";
    this.equityInput.step = "any";
    this.equityInput.value = String(this.defaultStartingEquity);
    this.ccySel = el("select");
    for (const c of CURRENCIES) {
      this.ccySel.appendChild(new Option(c, c, c === this.defaultReportingCurrency, c === this.defaultReportingCurrency));
    }
    const createBtn = el("button", null, "Create portfolio");
    createBtn.type = "button";
    createBtn.addEventListener("click", () => {
      this.createMsg.className = "hint";
      try {
        const pf = this.store.create({
          name: this.nameInput.value.trim(),
          startingEquityAmount: Number(this.equityInput.value),
          reportingCurrency: this.ccySel.value,
        });
        this.createMsg.textContent = `Created "${pf.name}" with ${formatMoney(pf.startingEquity)} starting equity.`;
        this.nameInput.value = "";
      } catch (err) {
        this.createMsg.className = "hint msg-bad";
        this.createMsg.textContent = err.message;
      }
      this.onChanged?.();
    });

    const createRow = el("div", "portfolio-create");
    createRow.append(this.nameInput, this.equityInput, this.ccySel, createBtn);
    this.createMsg = el("p", "hint", "");
    this.summary = el("p", "portfolio-summary", "");
    bar.append(createRow, this.selector, this.summary, this.createMsg);
  }

  render(nowMs = Date.now()) {
    this._renderSelector();
    const pf = this.store.active();
    this._renderSummary(pf);
    this._renderPositions(pf, nowMs);
    this._renderOrders(pf);
    this._renderBlotter(pf);
  }

  _renderSelector() {
    const list = this.store.list();
    const current = this.store.active()?.id ?? "";
    this.selector.textContent = "";
    this.selector.appendChild(new Option(list.length ? "— select portfolio —" : "no portfolios yet", ""));
    for (const p of list) {
      this.selector.appendChild(new Option(
        `${p.name} (${formatMoney(p.startingEquity)} start)`, p.id, p.id === current, p.id === current));
    }
  }

  _renderSummary(pf) {
    if (!pf) {
      this.summary.textContent = "No portfolio yet — create one above. The trading record starts empty.";
      return;
    }
    const cash = Object.entries(pf.cash)
      .filter(([, v]) => Math.abs(v) > 1e-9)
      .map(([c, v]) => formatMoney(money(v, c)))
      .join("  ·  ");
    this.summary.textContent = `${pf.name} — reporting currency ${pf.reportingCurrency}, `
      + `started ${fmtTs(pf.createdAtMs)} with ${formatMoney(pf.startingEquity)}. Cash: ${cash || "0"}.`;
  }

  _emptyRow(tbody, colSpan, text) {
    const tr = el("tr");
    const td = el("td", "hint", text);
    td.colSpan = colSpan;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  _renderPositions(pf, nowMs) {
    const tbody = this.roots.positions.querySelector("tbody");
    tbody.textContent = "";
    if (!pf) { this._emptyRow(tbody, 5, "No portfolio selected — create one above."); return; }
    for (const [symbol, pos] of Object.entries(pf.positions)) {
      const pair = PAIRS[symbol];
      const q = this.quotes.latest(symbol);
      // Unrealized at the exit side of the market: a long is marked at bid,
      // a short at ask — the price actually received if closed now.
      let unreal = "—";
      if (q && pos.qtyBase !== 0) {
        const exit = pos.qtyBase > 0 ? q.bid : q.ask;
        const u = (exit - pos.avgPrice) * pos.qtyBase;
        unreal = `${u.toFixed(2)} ${pair.quote} (@ ${pos.qtyBase > 0 ? "bid" : "ask"})`;
      }
      const tr = el("tr");
      for (const cell of [
        symbol,
        `${pos.qtyBase.toLocaleString("en-US")} ${pair.base}`,
        pos.avgPrice ? pos.avgPrice.toFixed(pair.priceDecimals) : "—",
        unreal,
        `${pos.realizedPnlQuote.toFixed(2)} ${pair.quote}`,
      ]) tr.appendChild(el("td", null, cell));
      tbody.appendChild(tr);
    }
    if (!tbody.children.length) {
      const tr = el("tr");
      const td = el("td", "hint", "No open positions.");
      td.colSpan = 5;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  _renderOrders(pf) {
    const tbody = this.roots.orders.querySelector("tbody");
    tbody.textContent = "";
    if (!pf) { this._emptyRow(tbody, 9, "No portfolio selected — create one above."); return; }
    const orders = [...pf.orders].reverse(); // newest first
    for (const o of orders) {
      const tr = el("tr");
      const pair = PAIRS[o.symbol];
      const detail = (o.type === "limit" ? `limit ${o.limitPrice.toFixed(pair.priceDecimals)}` : "market")
        + (o.origin === "delta-hedge" ? " · hedge" : "");
      for (const cell of [
        o.orderId, fmtTs(o.createdAtMs), o.symbol, o.side, detail,
        `${o.qty.amount.toLocaleString("en-US")} ${o.qty.currency}`,
      ]) tr.appendChild(el("td", null, cell));
      const statusTd = el("td");
      statusTd.appendChild(el("span", `badge ${STATUS_BADGE[o.status]}`, o.status));
      tr.appendChild(statusTd);
      tr.appendChild(el("td", null, o.statusReason ?? (o.fillId ? `fill ${o.fillId}` : "—")));
      const cancelTd = el("td");
      if (o.status === "pending") {
        const btn = el("button", null, "Cancel");
        btn.type = "button";
        btn.addEventListener("click", () => {
          try { this.engine.cancelOrder(o.orderId); } catch { /* raced a fill */ }
          this.onChanged?.();
        });
        cancelTd.appendChild(btn);
      }
      tr.appendChild(cancelTd);
      tbody.appendChild(tr);
    }
    if (!tbody.children.length) {
      const tr = el("tr");
      const td = el("td", "hint", "No orders yet.");
      td.colSpan = 9;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  _renderBlotter(pf) {
    const tbody = this.roots.blotter.querySelector("tbody");
    tbody.textContent = "";
    if (!pf) { this._emptyRow(tbody, 11, "No portfolio selected — create one above."); return; }
    for (const f of [...pf.blotter].reverse()) {
      const pair = PAIRS[f.symbol];
      const tr = el("tr");
      if (f.kind === "option" || f.kind === "option_expiry") {
        // Option executions are model-derived: show the model label and the
        // assumed spread instead of presenting the premium as an observed
        // market bid/ask. The recorded spot quote used by the model stays
        // visible in the source/quote-time columns.
        const d = pair.priceDecimals + 2;
        const what = f.kind === "option_expiry"
          ? `${f.cp} expiry settlement`
          : `${f.cp} option`;
        for (const cell of [
          f.fillId, f.orderId ?? "—", fmtTs(f.filledAtMs), f.symbol, `${f.side} (${what})`,
          `${f.qty.amount.toLocaleString("en-US")} ${f.qty.currency}`,
          `${f.price.toFixed(d)} ${f.priceCurrency} (model)`,
          `model ${f.model.modelBid.toFixed(d)} / ${f.model.modelAsk.toFixed(d)}`
            + (f.model.spreadFraction ? ` (assumed spread ${(f.model.spreadFraction * 100).toFixed(1)}%)` : ""),
          `model-derived · spot mid ${f.quote.mid.toFixed(pair.priceDecimals)} (${f.quote.source})`,
          fmtTs(f.quote.timestampMs),
          f.realizedPnlQuote ? `${f.realizedPnlQuote.toFixed(2)} ${pair.quote}` : "—",
        ]) tr.appendChild(el("td", null, cell));
        tbody.appendChild(tr);
        continue;
      }
      for (const cell of [
        `${f.fillId}${f.origin === "delta-hedge" ? " (hedge)" : ""}`,
        f.orderId, fmtTs(f.filledAtMs), f.symbol, f.side,
        `${f.qty.amount.toLocaleString("en-US")} ${f.qty.currency}`,
        `${f.price.toFixed(pair.priceDecimals)} ${f.priceCurrency}`,
        `${f.quote.bid.toFixed(pair.priceDecimals)} / ${f.quote.ask.toFixed(pair.priceDecimals)}`,
        f.quote.source,
        fmtTs(f.quote.timestampMs),
        f.realizedPnlQuote ? `${f.realizedPnlQuote.toFixed(2)} ${pair.quote}` : "—",
      ]) tr.appendChild(el("td", null, cell));
      tbody.appendChild(tr);
    }
    if (!tbody.children.length) {
      const tr = el("tr");
      const td = el("td", "hint", "Blotter is empty — no fills yet.");
      td.colSpan = 11;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }
}
