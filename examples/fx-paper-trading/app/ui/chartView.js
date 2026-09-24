// Chart view: candlestick chart per symbol with selectable timeframes from the
// feed's OHLC candles, plus SMA, EMA, RSI and MACD indicator panels computed by
// app/domain/indicators.js. Pure canvas rendering — no chart library, no CDN.
//
// Indicators are display analytics only. Nothing here can place, modify or
// fill an order: the chart module has no reference to the execution engine or
// the option book, and the panel says so next to the toggles.

import { PAIRS } from "../domain/units.js";
import { smaSeries, emaSeries, rsiSeries, macdSeries } from "../domain/indicators.js";

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const PRICE_H = 280;
const SUB_H = 96;

export class ChartView {
  constructor(root, { candleStore, symbols, intervals, defaultInterval }) {
    this.root = root;
    this.candles = candleStore;
    this.symbols = symbols;
    this.intervals = intervals;

    const controls = el("div", "chart-controls");
    this.symbolSel = el("select");
    for (const s of symbols) this.symbolSel.appendChild(new Option(s, s));
    this.intervalSel = el("select");
    for (const iv of intervals) {
      this.intervalSel.appendChild(new Option(iv, iv, iv === defaultInterval, iv === defaultInterval));
    }
    const symLab = el("label", null, "Symbol ");
    symLab.appendChild(this.symbolSel);
    const ivLab = el("label", null, " Timeframe ");
    ivLab.appendChild(this.intervalSel);
    this.toggles = {};
    const toggleWrap = el("span", "chart-toggles");
    for (const [key, label] of [["sma", "SMA 20"], ["ema", "EMA 50"], ["rsi", "RSI 14"], ["macd", "MACD 12·26·9"]]) {
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = key === "sma" || key === "ema";
      cb.addEventListener("change", () => this.draw());
      const lab = el("label", null, " " + label);
      lab.prepend(cb);
      toggleWrap.appendChild(lab);
      this.toggles[key] = cb;
    }
    const reloadBtn = el("button", null, "Reload");
    reloadBtn.type = "button";
    reloadBtn.addEventListener("click", () => this.load(true));

    controls.append(symLab, ivLab, toggleWrap, reloadBtn);

    this.status = el("p", "hint",
      "Indicators are computed from the feed's historical candles and are display-only: a displayed signal never places or modifies an order.");
    this.legend = el("p", "chart-legend", "");

    this.priceCanvas = el("canvas", "chart-canvas");
    this.priceCanvas.height = PRICE_H;
    this.rsiWrap = el("div", "chart-sub");
    this.rsiWrap.append(el("h4", null, "RSI 14"), this._subCanvas("rsi"));
    this.macdWrap = el("div", "chart-sub");
    this.macdWrap.append(el("h4", null, "MACD 12·26·9"), this._subCanvas("macd"));

    root.append(controls, this.legend, this.priceCanvas, this.rsiWrap, this.macdWrap, this.status);

    this.symbolSel.addEventListener("change", () => this.load());
    this.intervalSel.addEventListener("change", () => this.load());

    this.data = null; // { symbol, interval, bars, fetchedAtMs }
  }

  _subCanvas(name) {
    const c = el("canvas", "chart-canvas");
    c.height = SUB_H;
    c.dataset.sub = name;
    return c;
  }

  async load(force = false) {
    const symbol = this.symbolSel.value;
    const interval = this.intervalSel.value;
    this.status.textContent = `Loading ${symbol} ${interval} candles…`;
    const res = await this.candles.load(symbol, interval, { force });
    if (!res.ok) {
      this.data = null;
      this.status.textContent = `Candle load failed for ${symbol} ${interval}: ${res.error} (${res.detail}). `
        + "No bars are synthesized; the chart shows nothing until the feed answers.";
      this.draw();
      return;
    }
    this.data = res;
    this.draw();
  }

  // Redraw from the cached bars. Indicators are recomputed from the same closes
  // the candles show, so chart and indicator panels can never disagree.
  draw() {
    const showRsi = this.toggles.rsi.checked;
    const showMacd = this.toggles.macd.checked;
    this.rsiWrap.style.display = showRsi ? "" : "none";
    this.macdWrap.style.display = showMacd ? "" : "none";

    if (!this.data || !this.data.bars.length) {
      for (const c of [this.priceCanvas, this.rsiWrap.querySelector("canvas"), this.macdWrap.querySelector("canvas")]) {
        this._prep(c);
      }
      this.legend.textContent = "";
      return;
    }
    const bars = this.data.bars;
    const closes = bars.map((b) => b.close);
    const pair = PAIRS[this.data.symbol];
    const d = pair.priceDecimals;

    const sma = this.toggles.sma.checked ? smaSeries(closes, 20) : null;
    const ema = this.toggles.ema.checked ? emaSeries(closes, 50) : null;
    this._drawPrice(bars, { sma, ema }, d);

    const rsi = showRsi ? rsiSeries(closes, 14) : null;
    const macdRes = showMacd ? macdSeries(closes, 12, 26, 9) : null;
    if (showRsi) this._drawRsi(this.rsiWrap.querySelector("canvas"), rsi);
    if (showMacd) {
      this._drawMacd(this.macdWrap.querySelector("canvas"), macdRes.macd, macdRes.signal, macdRes.histogram);
    }

    const last = bars[bars.length - 1];
    const lastOf = (arr) => {
      if (!arr) return null;
      for (let i = arr.length - 1; i >= 0; i--) if (arr[i] !== null) return arr[i];
      return null;
    };
    const bits = [
      `${this.data.symbol} ${this.data.interval} — ${bars.length} bars`,
      `last close ${last.close.toFixed(d)} @ ${new Date(last.openTimeMs).toISOString().replace("T", " ").slice(0, 19)}Z`,
    ];
    const lv = (name, v) => bits.push(`${name} ${v === null ? "n/a (insufficient bars)" : v.toFixed(d)}`);
    if (sma) lv("SMA20", lastOf(sma));
    if (ema) lv("EMA50", lastOf(ema));
    if (rsi) bits.push(`RSI14 ${lastOf(rsi)?.toFixed(1) ?? "n/a (insufficient bars)"}`);
    if (macdRes) {
      const m = lastOf(macdRes.macd);
      const s = lastOf(macdRes.signal);
      bits.push(`MACD ${m === null ? "n/a" : m.toFixed(d)} / signal ${s === null ? "n/a" : s.toFixed(d)}`);
    }
    this.legend.textContent = bits.join("  ·  ");
    this.status.textContent =
      `Candles fetched ${new Date(this.data.fetchedAtMs).toISOString().slice(11, 19)}Z from the biquote feed; `
      + "the open bar (still forming) is included and marked by the feed.";
  }

  _prep(canvas) {
    const w = canvas.parentElement?.clientWidth || canvas.clientWidth || 600;
    const dpr = globalThis.devicePixelRatio || 1;
    canvas.width = Math.max(200, Math.floor(w * dpr));
    canvas.style.width = "100%";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, canvas.height);
    return { ctx, w, h: canvas.height };
  }

  _x(i, n, w) { return n <= 1 ? w / 2 : (i + 0.5) * (w / n); }

  _drawPrice(bars, overlays, decimals) {
    const { ctx, w, h } = this._prep(this.priceCanvas);
    const padL = 4, padR = 64, padT = 8, padB = 18;
    const iw = w - padL - padR;
    const ih = h - padT - padB;
    let min = Infinity, max = -Infinity;
    for (const b of bars) { min = Math.min(min, b.low); max = Math.max(max, b.high); }
    if (!(max > min)) { min -= 1e-6; max += 1e-6; }
    const y = (v) => padT + ih * (1 - (v - min) / (max - min));

    // grid + price labels
    ctx.strokeStyle = "#2a3642";
    ctx.fillStyle = "#8b9aa8";
    ctx.font = "11px system-ui, sans-serif";
    for (let g = 0; g <= 4; g++) {
      const v = min + (max - min) * (g / 4);
      ctx.beginPath();
      ctx.moveTo(padL, y(v));
      ctx.lineTo(padL + iw, y(v));
      ctx.stroke();
      ctx.fillText(v.toFixed(decimals), padL + iw + 6, y(v) + 4);
    }

    const n = bars.length;
    const bodyW = Math.max(1, Math.floor((iw / n) * 0.6));
    for (let i = 0; i < n; i++) {
      const b = bars[i];
      const x = padL + this._x(i, n, iw);
      const up = b.close >= b.open;
      ctx.strokeStyle = up ? "#35c48d" : "#ef5d6b";
      ctx.fillStyle = up ? "#35c48d" : "#ef5d6b";
      ctx.beginPath();
      ctx.moveTo(x, y(b.high));
      ctx.lineTo(x, y(b.low));
      ctx.stroke();
      const top = y(Math.max(b.open, b.close));
      const bot = y(Math.min(b.open, b.close));
      ctx.fillRect(x - bodyW / 2, top, bodyW, Math.max(1, bot - top));
    }

    const line = (series, color) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < n; i++) {
        const v = series[i];
        if (v === null) continue;
        const x = padL + this._x(i, n, iw);
        if (!started) { ctx.moveTo(x, y(v)); started = true; } else ctx.lineTo(x, y(v));
      }
      ctx.stroke();
      ctx.lineWidth = 1;
    };
    if (overlays.sma) line(overlays.sma, "#e0a63c");
    if (overlays.ema) line(overlays.ema, "#5aa9e6");

    // time range label
    ctx.fillStyle = "#8b9aa8";
    ctx.fillText(new Date(bars[0].openTimeMs).toISOString().slice(0, 16).replace("T", " ") + "Z", padL, h - 4);
    const end = new Date(bars[n - 1].openTimeMs).toISOString().slice(0, 16).replace("T", " ") + "Z";
    ctx.fillText(end, padL + iw - ctx.measureText(end).width, h - 4);
  }

  _drawRsi(canvas, rsi) {
    const { ctx, w, h } = this._prep(canvas);
    const padL = 4, padR = 40, padT = 6, padB = 6;
    const iw = w - padL - padR;
    const ih = h - padT - padB;
    const y = (v) => padT + ih * (1 - v / 100);
    ctx.strokeStyle = "#2a3642";
    for (const lvl of [30, 70]) {
      ctx.beginPath(); ctx.moveTo(padL, y(lvl)); ctx.lineTo(padL + iw, y(lvl)); ctx.stroke();
      ctx.fillStyle = "#8b9aa8"; ctx.font = "10px system-ui, sans-serif";
      ctx.fillText(String(lvl), padL + iw + 6, y(lvl) + 3);
    }
    ctx.strokeStyle = "#c792ea";
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < rsi.length; i++) {
      if (rsi[i] === null) continue;
      const x = padL + this._x(i, rsi.length, iw);
      if (!started) { ctx.moveTo(x, y(rsi[i])); started = true; } else ctx.lineTo(x, y(rsi[i]));
    }
    ctx.stroke();
  }

  _drawMacd(canvas, macd, signal, histogram) {
    const { ctx, w, h } = this._prep(canvas);
    const padL = 4, padR = 4, padT = 6, padB = 6;
    const iw = w - padL - padR;
    const ih = h - padT - padB;
    let min = Infinity, max = -Infinity;
    for (const arr of [macd, signal, histogram]) {
      for (const v of arr) {
        if (v === null) continue;
        min = Math.min(min, v); max = Math.max(max, v);
      }
    }
    if (!(max > min)) { ctx.fillStyle = "#8b9aa8"; ctx.fillText("insufficient bars for MACD", padL, h / 2); return; }
    const y = (v) => padT + ih * (1 - (v - min) / (max - min));
    const n = macd.length;
    // histogram
    const barW = Math.max(1, Math.floor((iw / n) * 0.6));
    for (let i = 0; i < n; i++) {
      const v = histogram[i];
      if (v === null) continue;
      const x = padL + this._x(i, n, iw);
      ctx.fillStyle = v >= 0 ? "rgba(53,196,141,0.6)" : "rgba(239,93,107,0.6)";
      const y0 = y(0), y1 = y(v);
      ctx.fillRect(x - barW / 2, Math.min(y0, y1), barW, Math.max(1, Math.abs(y1 - y0)));
    }
    const line = (series, color) => {
      ctx.strokeStyle = color;
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < n; i++) {
        const v = series[i];
        if (v === null) continue;
        const x = padL + this._x(i, n, iw);
        if (!started) { ctx.moveTo(x, y(v)); started = true; } else ctx.lineTo(x, y(v));
      }
      ctx.stroke();
    };
    line(macd, "#5aa9e6");
    line(signal, "#e0a63c");
  }
}
