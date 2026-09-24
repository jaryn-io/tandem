// Historical OHLC candles from the biquote feed, with a small in-memory cache.
// This module is the data source the chart layer (S04) renders from; it only
// loads and shapes bars, it never draws.

import { CONFIG } from "../config.js";

export const CandleError = {
  HTTP: "http_error",
  SHAPE: "unexpected_response",
};

export class CandleStore {
  constructor(config = CONFIG, now = () => Date.now()) {
    this.config = config;
    this.now = now;
    this._cache = new Map(); // `${symbol}|${interval}` -> { fetchedAtMs, bars }
  }

  static key(symbol, interval) { return `${symbol}|${interval}`; }

  // Load bars for a symbol/interval. Returns:
  //   { ok: true, symbol, interval, bars: Bar[], fetchedAtMs }
  //   { ok: false, symbol, interval, error, detail }
  // Bar: { openTimeMs, open, high, low, close, tickVolume, isOpen }
  // Bars are returned oldest-first (the feed answers newest-first).
  //
  // The cache expires after config.candleCacheMaxAgeMs: a cached entry is
  // served only while it is younger than that, so the chart's minute refresh
  // and any later symbol switch refetch instead of freezing on first-load
  // bars. `force` always bypasses the cache.
  async load(symbol, interval, { limit, force = false } = {}) {
    const effLimit = limit ?? this.config.defaultCandleLimit;
    const key = CandleStore.key(symbol, interval);
    const cached = this._cache.get(key);
    const maxAgeMs = this.config.candleCacheMaxAgeMs ?? 60_000;
    if (cached && !force && this.now() - cached.fetchedAtMs < maxAgeMs) {
      return { ok: true, symbol, interval, ...cached };
    }

    if (!this.config.candleIntervals.includes(interval)) {
      return { ok: false, symbol, interval, error: CandleError.SHAPE, detail: `bad interval ${interval}` };
    }
    try {
      const url = `${this.config.apiBase}/api/${encodeURIComponent(symbol)}/ohlc`
        + `?interval=${encodeURIComponent(interval)}&limit=${effLimit}`;
      const res = await fetch(url);
      if (!res.ok) {
        return { ok: false, symbol, interval, error: CandleError.HTTP, detail: `HTTP ${res.status}` };
      }
      const body = await res.json();
      if (!Array.isArray(body?.bars)) {
        return { ok: false, symbol, interval, error: CandleError.SHAPE, detail: "missing bars array" };
      }
      const bars = body.bars
        .map((b) => ({
          openTimeMs: Date.parse(b.openTime),
          open: Number(b.open),
          high: Number(b.high),
          low: Number(b.low),
          close: Number(b.close),
          tickVolume: Number(b.tickVolume ?? 0),
          isOpen: Boolean(b.isOpen),
        }))
        .filter((b) => Number.isFinite(b.openTimeMs))
        .sort((a, b) => a.openTimeMs - b.openTimeMs);
      const entry = { fetchedAtMs: this.now(), bars };
      this._cache.set(key, entry);
      return { ok: true, symbol, interval, ...entry };
    } catch (err) {
      return { ok: false, symbol, interval, error: CandleError.HTTP, detail: String(err?.message ?? err) };
    }
  }

  cached(symbol, interval) {
    const c = this._cache.get(CandleStore.key(symbol, interval));
    return c ? { symbol, interval, ...c } : null;
  }
}
