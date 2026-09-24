// Currency domain model: explicit units on every quantity, pair metadata, and
// reporting-currency conversion driven exclusively by observed feed quotes.
// No exchange rate is ever manufactured or silently extrapolated here.

export const CURRENCIES = ["EUR", "USD", "GBP", "JPY"];

// Static metadata for the supported instruments. `priceDecimals` reflects the
// feed convention (JPY-quoted pairs carry 3 decimals; others 5).
export const PAIRS = {
  EURUSD: { symbol: "EURUSD", base: "EUR", quote: "USD", priceDecimals: 5, pip: 0.0001 },
  GBPUSD: { symbol: "GBPUSD", base: "GBP", quote: "USD", priceDecimals: 5, pip: 0.0001 },
  USDJPY: { symbol: "USDJPY", base: "USD", quote: "JPY", priceDecimals: 3, pip: 0.01 },
};

export function pairOf(symbol) {
  const pair = PAIRS[symbol];
  if (!pair) throw new Error(`unknown instrument: ${symbol}`);
  return pair;
}

// A quantity with an explicit currency unit. All portfolio amounts, notionals
// and P&L figures in the application are carried as Money, never bare numbers.
export function money(amount, currency) {
  if (typeof amount !== "number" || !Number.isFinite(amount)) {
    throw new Error(`invalid amount for ${currency}: ${amount}`);
  }
  if (!CURRENCIES.includes(currency)) {
    throw new Error(`unsupported currency unit: ${currency}`);
  }
  return Object.freeze({ amount, currency });
}

export function formatMoney(m, decimals = 2) {
  return `${m.amount.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })} ${m.currency}`;
}

// Build the quote-lookup view used for conversion. `quoteStore` must expose
// latest(symbol) -> normalized quote | null and ageSeconds(symbol, nowMs).
//
// Returns one of:
//   { ok: true, result: Money, rate, rateSymbol, rateTimestampMs, inverted, legs }
//   { ok: false, reason, neededSymbol }
// A missing or unavailable quote yields ok:false with the pair that would be
// needed — the caller must surface this instead of guessing a rate.
//
// `legs` lists every observed quote the conversion used, each
// { rateSymbol, rate, rateTimestampMs, inverted, factor, from, to }. When no
// direct pair exists, the conversion crosses through USD — every supported
// pair trades against USD, so both legs are observed feed quotes and both are
// disclosed; no cross rate is ever manufactured.
export function convertMoney(m, toCurrency, quoteStore, nowMs = Date.now()) {
  if (!CURRENCIES.includes(toCurrency)) {
    return { ok: false, reason: "unsupported_target_currency", neededSymbol: null };
  }
  if (m.currency === toCurrency) {
    return {
      ok: true,
      result: money(m.amount, toCurrency),
      rate: 1,
      rateSymbol: null,
      rateTimestampMs: null,
      inverted: false,
      legs: [],
    };
  }
  const single = conversionLeg(m.currency, toCurrency, quoteStore);
  if (single.ok) {
    return {
      ok: true,
      result: money(m.amount * single.factor, toCurrency),
      rate: single.rate,
      rateSymbol: single.rateSymbol,
      rateTimestampMs: single.rateTimestampMs,
      inverted: single.inverted,
      legs: [single],
    };
  }
  if (single.reason === "no_quote_available") {
    return { ok: false, reason: single.reason, neededSymbol: single.neededSymbol };
  }
  // No direct pair: cross through USD (e.g. EUR -> USD -> GBP). Both legs
  // must resolve to observed quotes; a missing leg refuses the conversion.
  const leg1 = conversionLeg(m.currency, "USD", quoteStore);
  if (!leg1.ok) return { ok: false, reason: leg1.reason, neededSymbol: leg1.neededSymbol };
  const leg2 = conversionLeg("USD", toCurrency, quoteStore);
  if (!leg2.ok) return { ok: false, reason: leg2.reason, neededSymbol: leg2.neededSymbol };
  return {
    ok: true,
    result: money(m.amount * leg1.factor * leg2.factor, toCurrency),
    rate: leg1.factor * leg2.factor, // effective combined rate; see legs
    rateSymbol: null,                // no single pair — the legs are the disclosure
    rateTimestampMs: null,
    inverted: false,
    crossedVia: "USD",
    legs: [leg1, leg2],
  };
}

// One conversion leg between two different currencies via a directly observed
// pair (or its inverse). Never manufactures a rate.
function conversionLeg(fromCurrency, toCurrency, quoteStore) {
  const direct = `${fromCurrency}${toCurrency}`;
  const inverse = `${toCurrency}${fromCurrency}`;
  if (PAIRS[direct]) {
    const q = quoteStore.latest(direct);
    if (!q) return { ok: false, reason: "no_quote_available", neededSymbol: direct };
    return {
      ok: true, from: fromCurrency, to: toCurrency, factor: q.mid,
      rate: q.mid, rateSymbol: direct, rateTimestampMs: q.timestampMs, inverted: false,
    };
  }
  if (PAIRS[inverse]) {
    const q = quoteStore.latest(inverse);
    if (!q) return { ok: false, reason: "no_quote_available", neededSymbol: inverse };
    return {
      ok: true, from: fromCurrency, to: toCurrency, factor: 1 / q.mid,
      rate: q.mid, rateSymbol: inverse, rateTimestampMs: q.timestampMs, inverted: true,
    };
  }
  return { ok: false, reason: "no_conversion_pair", neededSymbol: null };
}

// Disclosure for one conversion leg: the rate actually used, the pair it came
// from, and how old that quote was.
export function legDisclosure(leg, quoteStore, nowMs = Date.now()) {
  const pair = PAIRS[leg.rateSymbol];
  const age = quoteStore.ageSeconds(leg.rateSymbol, nowMs);
  const ageText = age === null ? "age unknown" : `${age.toFixed(0)}s old`;
  const applied = leg.inverted ? `applied as 1/${leg.rate}` : "applied directly";
  return `${leg.rateSymbol} mid = ${leg.rate.toFixed(pair.priceDecimals)}`
    + ` (${new Date(leg.rateTimestampMs).toISOString()}, ${ageText}, ${applied})`;
}

// Format the disclosure line required next to any converted figure. A crossed
// conversion names both legs and their timestamps.
export function conversionDisclosure(conv, quoteStore, nowMs = Date.now()) {
  if (!conv.ok) return `conversion unavailable (${conv.reason})`;
  const legs = conv.legs ?? [];
  if (!legs.length) return "same currency, rate 1";
  const text = legs.map((leg) => legDisclosure(leg, quoteStore, nowMs)).join(" · then · ");
  return legs.length > 1 ? `crossed via USD: ${text}` : text;
}
