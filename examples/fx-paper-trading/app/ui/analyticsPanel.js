// Analysis panel: the P&L and demonstration surface. Shows current equity with
// its conversion disclosure, realized/unrealized totals (spot at observed
// bid/ask, options at Garman–Kohlhagen model marks — always labelled), the
// equity-over-time curve from persisted samples, per-pair results, spread
// cost, currency exposure, the contribution split across spot / options /
// hedges, and the trade sequence with cumulative P&L. Rendering only: every
// number comes from computeAnalytics / computeEquity in app/domain/analytics.js.

import { PAIRS, money, formatMoney } from "../domain/units.js";
import { computeAnalytics, computeEquity, formatReporting } from "../domain/analytics.js";

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function fmtTs(ms) {
  return ms ? new Date(ms).toISOString().replace("T", " ").slice(0, 19) + "Z" : "—";
}

function signed(amount, currency, decimals = 2) {
  const s = amount > 0 ? "+" : "";
  return `${s}${formatMoney(money(amount, currency), decimals)}`;
}

export class AnalyticsPanel {
  constructor(root, { portfolioStore, quoteStore, book }) {
    this.root = root;
    this.store = portfolioStore;
    this.quotes = quoteStore;
    this.book = book;

    this.summary = el("div", "analysis-summary");
    this.disclosure = el("p", "hint", "");
    this.equityCanvas = el("canvas", "chart-canvas");
    this.equityCanvas.height = 160;
    this.equityCaption = el("p", "hint", "");
    this.pairTable = this._table(["Pair", "Realized spot", "Realized hedges", "Realized options", "Unrealized spot (bid/ask)", "Unrealized options (model)", "Spread cost"]);
    this.contribTable = this._table(["Contribution", "Realized P&L (reporting ccy)", "Unrealized P&L (reporting ccy)"]);
    this.exposureTable = this._table(["Currency", "Cash", "In reporting currency"]);
    this.notes = el("ul", "hint analysis-notes");
    this.seqTable = this._table(["Fill", "Time (UTC)", "What", "Price basis", "Realized P&L", "Cumulative P&L"]);

    root.append(
      el("h3", "subhead", "Equity and P&L"),
      this.summary, this.disclosure,
      this.equityCanvas, this.equityCaption,
      el("h3", "subhead", "Results by pair"),
      this.pairTable,
      el("h3", "subhead", "Contribution of spot, options and hedges"),
      this.contribTable,
      el("h3", "subhead", "Currency exposure"),
      this.exposureTable,
      el("h3", "subhead", "Trade sequence and cumulative P&L"),
      this.seqTable,
      this.notes,
    );
  }

  _table(headers) {
    const t = el("table", "data-table");
    const thead = el("thead");
    const hr = el("tr");
    for (const h of headers) hr.appendChild(el("th", null, h));
    thead.appendChild(hr);
    t.append(thead, el("tbody"));
    return t;
  }

  _rows(table, rows, emptyText) {
    const tbody = table.querySelector("tbody");
    tbody.textContent = "";
    if (!rows.length) {
      const tr = el("tr");
      const td = el("td", "hint", emptyText);
      td.colSpan = table.querySelectorAll("th").length;
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    for (const r of rows) tbody.appendChild(r);
  }

  render(nowMs = Date.now()) {
    const pf = this.store.active();
    if (!pf) {
      this.summary.textContent = "";
      this.summary.appendChild(el("p", "hint",
        "No portfolio selected — create one above. Analysis starts from the first simulated trade."));
      this.disclosure.textContent = "";
      this.equityCaption.textContent = "";
      this._prepCanvas();
      this._rows(this.pairTable, [], "No portfolio selected.");
      this._rows(this.contribTable, [], "No portfolio selected.");
      this._rows(this.exposureTable, [], "No portfolio selected.");
      this._rows(this.seqTable, [], "No portfolio selected.");
      this.notes.textContent = "";
      return;
    }

    const a = computeAnalytics(pf, this.quotes, this.book, nowMs);
    const rep = a.reportingCurrency;
    const eq = computeEquity(pf, this.quotes, this.book, nowMs, a.disclosuresUsed);
    const recon = a.reconciliation;

    // --- summary cards ---
    this.summary.textContent = "";
    const cards = [
      ["Equity", eq.ok ? formatMoney(eq.equity) : `unavailable — need quote for ${eq.missing.join(", ")}`],
      ["Realized P&L", formatReporting(a.totals.realized, rep)],
      ["Unrealized spot P&L (observed bid/ask)", formatReporting(a.totals.unrealizedSpot, rep)],
      ["Unrealized option P&L (model-derived)", formatReporting(a.totals.unrealizedOptions, rep)],
      ["Spread cost paid", formatReporting(a.totals.spreadCost, rep)],
      ["Marking-basis adjustment", recon.ok
        ? `${signed(recon.adjustment, rep)} — equity at mid vs spot P&L at exit side`
        : "unavailable (see notes)"],
    ];
    for (const [label, value] of cards) {
      const c = el("div", "analysis-card");
      c.append(el("span", "label", label), el("span", "value", value));
      this.summary.appendChild(c);
    }
    const basis = a.conversionDisclosures.length
      ? `Reporting currency ${rep}; conversion at live feed mid — ${a.conversionDisclosures.join(" · ")}.`
      : `Reporting currency ${rep}; no conversion needed yet.`;
    this.disclosure.textContent = basis
      + " Option figures are Garman–Kohlhagen model marks, never observed quotes.";

    // --- equity curve ---
    this._drawEquity(pf, rep);

    // --- per-pair table ---
    const rows = [];
    for (const [symbol, r] of Object.entries(a.perPair).sort()) {
      const ccy = r.quoteCurrency;
      const tr = el("tr");
      for (const cell of [
        symbol,
        signed(r.realized.spot, ccy),
        signed(r.realized.hedge, ccy),
        `${signed(r.realized.option, ccy)} (model)`,
        r.unrealizedSpot === null ? "— (no quote)" : signed(r.unrealizedSpot, ccy),
        `${signed(r.unrealizedOptions, ccy)} (model)${r.unpricedLots ? ` · ${r.unpricedLots} lot(s) unpriced` : ""}`,
        formatMoney(money(r.spreadCost, ccy)),
      ]) tr.appendChild(el("td", null, cell));
      rows.push(tr);
    }
    this._rows(this.pairTable, rows, "No fills or positions yet — the record is empty until the first trade.");

    // --- contribution table ---
    const crows = [];
    const labels = {
      spot: "Spot trades",
      hedge: "Delta hedges (spot)",
      option: "Options (model-derived)",
    };
    for (const g of ["spot", "hedge", "option"]) {
      const tr = el("tr");
      const known = a.contributionKnown[g];
      tr.appendChild(el("td", null, labels[g]));
      tr.appendChild(el("td", null, known ? signed(a.contributions[g], rep) : "unavailable (conversion)"));
      let unreal;
      if (g === "spot") {
        unreal = (a.totals.unrealizedSpot.ok ? signed(a.totals.unrealizedSpot.amount, rep) : "unavailable")
          + " — net positions at exit side; hedge fills share the same average cost";
      } else if (g === "hedge") {
        unreal = "included in the spot row (net positions share average cost)";
      } else {
        unreal = (a.totals.unrealizedOptions.ok ? signed(a.totals.unrealizedOptions.amount, rep) : "unavailable")
          + " (model-derived)";
      }
      tr.appendChild(el("td", null, unreal));
      crows.push(tr);
    }
    this._rows(this.contribTable, crows, "No fills yet.");

    // --- currency exposure ---
    const erows = [];
    for (const c of a.cashExposure) {
      const tr = el("tr");
      tr.appendChild(el("td", null, c.currency));
      tr.appendChild(el("td", null, formatMoney(money(c.amount, c.currency))));
      tr.appendChild(el("td", null, c.reporting === null
        ? `unavailable — no quote for ${c.neededSymbol ?? "conversion"}`
        : formatMoney(money(c.reporting, rep))));
      erows.push(tr);
    }
    this._rows(this.exposureTable, erows, "No cash balances yet.");

    // --- trade sequence ---
    const srows = [];
    for (const t of a.tradeSequence) {
      const pair = PAIRS[t.symbol];
      const tr = el("tr");
      const what = t.kind === "option" || t.kind === "option_expiry"
        ? `${t.side} ${t.qty.amount.toLocaleString("en-US")} ${t.qty.currency} ${t.symbol} option${t.kind === "option_expiry" ? " expiry" : ""}`
        : `${t.side} ${t.qty.amount.toLocaleString("en-US")} ${t.qty.currency} ${t.symbol}${t.group === "hedge" ? " (hedge)" : ""}`;
      const basisText = t.modelDerived
        ? `${t.price.toFixed(pair.priceDecimals + 2)} ${t.priceCurrency} — model-derived (GK)`
        : `${t.price.toFixed(pair.priceDecimals)} ${t.priceCurrency} — observed ${t.side === "buy" ? "ask" : "bid"}`;
      for (const cell of [
        t.fillId, fmtTs(t.filledAtMs), what, basisText,
        t.realizedQuote ? signed(t.realizedQuote, t.priceCurrency) : "—",
        t.cumulativeReporting === null ? "unavailable" : signed(t.cumulativeReporting, rep),
      ]) tr.appendChild(el("td", null, cell));
      srows.push(tr);
    }
    this._rows(this.seqTable, srows, "No fills yet — the trade sequence builds from the first execution.");

    // --- notes ---
    this.notes.textContent = "";
    for (const u of a.unavailable) this.notes.appendChild(el("li", null, `Not converted: ${u}.`));
    const note = el("li", null,
      "Unrealized spot P&L is measured on net positions at the exit side of the observed market; "
      + "realized contributions per group are exact from the blotter. Option P&L is model-derived "
      + "(Garman–Kohlhagen) at the user's stated assumptions.");
    this.notes.appendChild(note);
    const reconNote = recon.ok
      ? `Equity change since start ${signed(recon.equityChange, rep)} = realized + unrealized cards `
        + `${signed(recon.pnlSum, rep)} plus the marking-basis adjustment ${signed(recon.adjustment, rep)}. `
        + "Equity values cash at the observed mid while the unrealized spot card marks at the exit side "
        + "of the bid/ask; the adjustment is the half-spread still carried on open spot positions."
      : "The equity-to-P&L reconciliation is unavailable while an equity or P&L component cannot be "
        + "converted (see above); nothing is estimated to fill the gap.";
    this.notes.appendChild(el("li", null, reconNote));
  }

  _prepCanvas() {
    const canvas = this.equityCanvas;
    const w = canvas.parentElement?.clientWidth || canvas.clientWidth || 600;
    const dpr = globalThis.devicePixelRatio || 1;
    canvas.width = Math.max(200, Math.floor(w * dpr));
    canvas.style.width = "100%";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, canvas.height);
    return { ctx, w, h: canvas.height };
  }

  _drawEquity(pf, rep) {
    const { ctx, w, h } = this._prepCanvas();
    const samples = pf.equityHistory;
    if (!samples.length) {
      this.equityCaption.textContent =
        "Equity over time: no samples yet. Samples are recorded on every fill and once a minute while the terminal runs, and persist across restarts.";
      return;
    }
    const padL = 4, padR = 76, padT = 8, padB = 18;
    const iw = w - padL - padR;
    const ih = h - padT - padB;
    const values = samples.map((s) => s.amount);
    let min = Math.min(...values, pf.startingEquity.amount);
    let max = Math.max(...values, pf.startingEquity.amount);
    if (!(max > min)) { min -= Math.abs(min) * 0.001 + 1e-9; max += Math.abs(max) * 0.001 + 1e-9; }
    const t0 = samples[0].t;
    const t1 = samples[samples.length - 1].t;
    const span = Math.max(1, t1 - t0);
    const y = (v) => padT + ih * (1 - (v - min) / (max - min));
    const x = (t) => padL + iw * ((t - t0) / span);

    ctx.strokeStyle = "#2a3642";
    ctx.fillStyle = "#8b9aa8";
    ctx.font = "11px system-ui, sans-serif";
    for (let g = 0; g <= 3; g++) {
      const v = min + (max - min) * (g / 3);
      ctx.beginPath(); ctx.moveTo(padL, y(v)); ctx.lineTo(padL + iw, y(v)); ctx.stroke();
      ctx.fillText(formatMoney(money(v, rep), 0), padL + iw + 6, y(v) + 4);
    }
    // starting equity reference line
    ctx.strokeStyle = "#8b9aa8";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(padL, y(pf.startingEquity.amount)); ctx.lineTo(padL + iw, y(pf.startingEquity.amount)); ctx.stroke();
    ctx.setLineDash([]);

    ctx.strokeStyle = "#5aa9e6";
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    samples.forEach((s, i) => {
      if (i === 0) ctx.moveTo(x(s.t), y(s.amount)); else ctx.lineTo(x(s.t), y(s.amount));
    });
    ctx.stroke();
    ctx.lineWidth = 1;

    this.equityCaption.textContent =
      `Equity over time in ${rep} — ${samples.length} sample(s), first ${fmtTs(samples[0].t)}, latest ${fmtTs(t1)}. `
      + `Dashed line: starting equity ${formatMoney(pf.startingEquity)}. Equity is converted cash plus model value of open option lots.`;
  }
}
