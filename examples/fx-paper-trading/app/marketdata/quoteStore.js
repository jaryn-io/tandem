// Quote lifecycle: normalization of biquote ticks, per-symbol last-known state,
// connection tracking, and the fill-eligibility gate.
//
// Rules implemented (from the brief):
//  - Never manufacture or silently extrapolate a market quote. Only ticks that
//    actually arrived from the feed are stored, with their own timestamps.
//  - The last displayed price is always retained, whatever happens afterwards.
//  - Simulated fills are suspended when the market is closed, the feed is
//    unavailable, or an active-market quote is older than maxQuoteAgeSeconds.

export const CONNECTION_STATES = [
  "connecting",    // initial SignalR/poll bootstrap in progress
  "streaming",     // live ticks arriving via SignalR
  "polling",       // REST fallback active (quotes still arrive)
  "reconnecting",  // stream lost, automatic reconnect in progress
  "disconnected",  // no quotes arriving; last display retained
  "failed",        // connection could not be established at all
];

// Normalized quote shape carried everywhere downstream:
// { symbol, bid, ask, mid, spread, source, timestampMs, receivedAtMs,
//   marketState, feedStale, feedQuoteAgeSeconds, direction, dayHigh, dayLow }
export function normalizeTick(raw, receivedAtMs = Date.now()) {
  if (!raw || typeof raw.symbol !== "string") {
    throw new Error("malformed tick: missing symbol");
  }
  const timestampMs = Date.parse(raw.timestamp);
  if (!Number.isFinite(timestampMs)) {
    throw new Error(`malformed tick for ${raw.symbol}: unparseable timestamp`);
  }
  const bid = Number(raw.bid);
  const ask = Number(raw.ask);
  const mid = Number(raw.mid);
  const spread = Number(raw.spread);
  if (!Number.isFinite(bid) || !Number.isFinite(ask)
      || !Number.isFinite(mid) || !Number.isFinite(spread)) {
    throw new Error(`malformed tick for ${raw.symbol}: non-finite price`);
  }
  if (bid <= 0 || ask <= 0) {
    throw new Error(`malformed tick for ${raw.symbol}: non-positive price`);
  }
  if (bid > ask) {
    throw new Error(`malformed tick for ${raw.symbol}: crossed book (bid > ask)`);
  }
  if (mid <= 0) {
    throw new Error(`malformed tick for ${raw.symbol}: non-positive mid`);
  }
  if (mid < bid || mid > ask) {
    throw new Error(`malformed tick for ${raw.symbol}: mid outside bid/ask`);
  }
  return {
    symbol: raw.symbol,
    bid,
    ask,
    mid,
    spread,
    source: String(raw.source ?? "unknown"),
    timestampMs,               // exchange-side tick time (UTC)
    receivedAtMs,              // local arrival time
    marketState: raw.marketState === "closed" ? "closed" : "open",
    feedStale: Boolean(raw.stale),
    feedQuoteAgeSeconds: Number.isFinite(raw.quoteAgeSeconds) ? raw.quoteAgeSeconds : null,
    direction: raw.direction ?? "FLAT",
    dayHigh: Number(raw.high),
    dayLow: Number(raw.low),
  };
}

export class QuoteStore {
  constructor({ maxQuoteAgeSeconds = 30 } = {}) {
    this.maxQuoteAgeSeconds = maxQuoteAgeSeconds;
    this._quotes = new Map();       // symbol -> normalized quote (last known)
    this._connectionState = "connecting";
    this._lastTickAtMs = null;      // last arrival of any tick
  }

  setConnectionState(state) {
    if (!CONNECTION_STATES.includes(state)) throw new Error(`bad state: ${state}`);
    this._connectionState = state;
  }

  get connectionState() { return this._connectionState; }
  get lastTickAtMs() { return this._lastTickAtMs; }

  // Store a real tick. Returns the normalized quote.
  ingest(rawTick, receivedAtMs = Date.now()) {
    const q = normalizeTick(rawTick, receivedAtMs);
    this._quotes.set(q.symbol, q);
    this._lastTickAtMs = receivedAtMs;
    return q;
  }

  latest(symbol) {
    return this._quotes.get(symbol) ?? null;
  }

  symbols() {
    return [...this._quotes.keys()];
  }

  // Local quote age in seconds, measured from the exchange timestamp.
  // Negative skew (clock slightly ahead) is clamped to 0; a quote timestamped
  // in the future beyond a small tolerance is treated as untrustworthy.
  ageSeconds(symbol, nowMs = Date.now()) {
    const q = this._quotes.get(symbol);
    if (!q) return null;
    const age = (nowMs - q.timestampMs) / 1000;
    if (age < -5) return null; // server clock ahead of us: age unknowable
    return Math.max(0, age);
  }

  // The fill gate. Pure and deterministic so execution code (S02) and tests
  // share exactly one definition of "may a simulated fill happen now".
  //
  // Returns { tradable: boolean, reasons: string[] } with reasons drawn from:
  //   no_quote | market_closed | feed_unavailable | quote_stale
  fillEligibility(symbol, nowMs = Date.now()) {
    const reasons = [];
    const q = this._quotes.get(symbol);

    if (!q) reasons.push("no_quote");

    if (q && q.marketState !== "open") reasons.push("market_closed");

    // Quotes arriving means the feed is available, regardless of transport.
    const transportAlive =
      this._connectionState === "streaming" || this._connectionState === "polling";
    if (!transportAlive) reasons.push("feed_unavailable");

    if (q && q.marketState === "open") {
      const age = this.ageSeconds(symbol, nowMs);
      // Age unknowable (clock skew) is treated as stale: we cannot prove
      // freshness, and the brief forbids assuming it.
      if (age === null || age > this.maxQuoteAgeSeconds) reasons.push("quote_stale");
      if (q.feedStale) reasons.push("quote_stale");
    }

    return { tradable: reasons.length === 0, reasons: [...new Set(reasons)] };
  }
}
