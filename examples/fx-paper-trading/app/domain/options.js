// Garman–Kohlhagen model for European vanilla FX options: theoretical premium
// and Greeks. Pure functions, no feed access — the caller supplies the spot
// (always the observed feed mid) and the user's volatility and rate
// assumptions. Everything priced here is model-derived: there is no options
// quote feed, so these values must never be presented as observed market
// bid/ask.
//
// Conventions (pair BASE/QUOTE, e.g. EURUSD = EUR base, USD quote):
//  - premium is per unit of base currency, expressed in the quote currency;
//  - rateDomestic is the quote-currency rate, rateForeign the base-currency
//    rate (continuously compounded, per year);
//  - delta is the spot delta per unit of base notional, so a position delta in
//    base-currency units is qtySign * notional * delta;
//  - vega is per 1.00 of volatility (divide by 100 for one vol point);
//  - theta and the rhos are per year (divide by 365 for one day).

// Abramowitz & Stegun 7.1.26 erf approximation (|error| <= 1.5e-7), enough for
// a simulation terminal; the deterministic checks pin premiums against an
// independent erf-based reference.
function erf(x) {
  const sign = x < 0 ? -1 : 1;
  const ax = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * ax);
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
    - 0.284496736) * t + 0.254829592) * t * Math.exp(-ax * ax);
  return sign * y;
}

export function normCdf(x) {
  return 0.5 * (1 + erf(x / Math.SQRT2));
}

export function normPdf(x) {
  return Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
}

export const OPTION_CP = ["call", "put"];

// cp: "call" | "put"; spot/strike > 0; yearsToExpiry >= 0; volatility > 0 when
// yearsToExpiry > 0; rates finite. Returns null-valued Greeks at expiry, where
// only intrinsic value survives.
export function garmanKohlhagen({
  cp, spot, strike, yearsToExpiry, volatility, rateDomestic, rateForeign,
}) {
  if (!OPTION_CP.includes(cp)) throw new Error(`bad option type: ${cp}`);
  if (!(spot > 0)) throw new Error(`spot must be positive, got ${spot}`);
  if (!(strike > 0)) throw new Error(`strike must be positive, got ${strike}`);
  if (!(yearsToExpiry >= 0)) throw new Error(`negative time to expiry: ${yearsToExpiry}`);
  if (!Number.isFinite(rateDomestic) || !Number.isFinite(rateForeign)) {
    throw new Error("rate assumptions must be finite numbers");
  }

  const T = yearsToExpiry;
  if (T === 0 || volatility === 0) {
    // Expired (or degenerate): intrinsic value, delta is the exercise slope.
    const intrinsic = cp === "call" ? Math.max(spot - strike, 0) : Math.max(strike - spot, 0);
    const delta = cp === "call" ? (spot > strike ? 1 : 0) : (spot < strike ? -1 : 0);
    return {
      price: intrinsic, delta, gamma: 0, vega: 0, theta: 0,
      rhoDomestic: 0, rhoForeign: 0, d1: null, d2: null, expired: true,
    };
  }
  if (!(volatility > 0)) throw new Error(`volatility must be positive, got ${volatility}`);

  const sqrtT = Math.sqrt(T);
  const dfD = Math.exp(-rateDomestic * T);
  const dfF = Math.exp(-rateForeign * T);
  const d1 = (Math.log(spot / strike) + (rateDomestic - rateForeign + 0.5 * volatility * volatility) * T)
    / (volatility * sqrtT);
  const d2 = d1 - volatility * sqrtT;

  const gamma = dfF * normPdf(d1) / (spot * volatility * sqrtT);
  const vega = spot * dfF * normPdf(d1) * sqrtT;
  const thetaTime = -spot * dfF * normPdf(d1) * volatility / (2 * sqrtT);

  if (cp === "call") {
    return {
      price: spot * dfF * normCdf(d1) - strike * dfD * normCdf(d2),
      delta: dfF * normCdf(d1),
      gamma, vega,
      theta: thetaTime - rateDomestic * strike * dfD * normCdf(d2)
        + rateForeign * spot * dfF * normCdf(d1),
      rhoDomestic: strike * T * dfD * normCdf(d2),
      rhoForeign: -spot * T * dfF * normCdf(d1),
      d1, d2, expired: false,
    };
  }
  return {
    price: strike * dfD * normCdf(-d2) - spot * dfF * normCdf(-d1),
    delta: -dfF * normCdf(-d1),
    gamma, vega,
    theta: thetaTime + rateDomestic * strike * dfD * normCdf(-d2)
      - rateForeign * spot * dfF * normCdf(-d1),
    rhoDomestic: -strike * T * dfD * normCdf(-d2),
    rhoForeign: spot * T * dfF * normCdf(-d1),
    d1, d2, expired: false,
  };
}
