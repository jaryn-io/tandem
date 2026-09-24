// Deterministic checks for the S01 quote lifecycle and conversion inputs.
// Run: node checks/quote-lifecycle.check.mjs
// Synthetic ticks stand in for the feed so every branch of the fill gate is
// exercised without network access. Live-feed behaviour is verified separately
// in the browser against the real biquote endpoint.

import { QuoteStore, normalizeTick } from "../app/marketdata/quoteStore.js";
import { money, convertMoney, conversionDisclosure } from "../app/domain/units.js";

const NOW = Date.parse("2026-09-24T12:00:00Z");
let failures = 0;

function check(name, cond) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
  if (!cond) failures++;
}

function tick(overrides = {}) {
  return {
    symbol: "EURUSD",
    bid: 1.13916, ask: 1.13922, mid: 1.13919, spread: 0.00006,
    timestamp: "2026-09-24T11:59:59Z",
    source: "MetaTrader 5 (Broker 1)",
    marketState: "open", stale: false, quoteAgeSeconds: 1,
    direction: "FLAT", high: 1.14, low: 1.13,
    ...overrides,
  };
}

const OLD_TIMESTAMP = "2026-09-24T11:59:15Z"; // 45s before NOW

// 1. Fresh open-market quote while streaming -> tradable.
{
  const s = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  s.setConnectionState("streaming");
  s.ingest(tick(), NOW);
  const g = s.fillEligibility("EURUSD", NOW);
  check("fresh open quote while streaming is tradable", g.tradable && g.reasons.length === 0);
}

// 2. Quote older than 30s in an open market -> suspended as quote_stale,
//    but the last display quote is still retained.
{
  const s = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  s.setConnectionState("streaming");
  s.ingest(tick({ timestamp: OLD_TIMESTAMP, quoteAgeSeconds: 45 }), NOW);
  const g = s.fillEligibility("EURUSD", NOW);
  check("quote older than 30s suspends fills (quote_stale)",
    !g.tradable && g.reasons.includes("quote_stale"));
  check("stale quote remains the last displayed quote", s.latest("EURUSD")?.bid === 1.13916);
}

// 3. Configurable limit: raising it to 60s re-enables the same quote.
{
  const s = new QuoteStore({ maxQuoteAgeSeconds: 60 });
  s.setConnectionState("streaming");
  s.ingest(tick({ timestamp: OLD_TIMESTAMP, quoteAgeSeconds: 45 }), NOW);
  check("age limit is configurable", s.fillEligibility("EURUSD", NOW).tradable);
}

// 4. Closed market -> suspended as market_closed, last price retained.
{
  const s = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  s.setConnectionState("streaming");
  s.ingest(tick({ marketState: "closed" }), NOW);
  const g = s.fillEligibility("EURUSD", NOW);
  check("closed market suspends fills (market_closed)",
    !g.tradable && g.reasons.includes("market_closed"));
  check("closed market still shows last price", s.latest("EURUSD")?.ask === 1.13922);
}

// 5. Feed unavailable (transport down) -> suspended as feed_unavailable even
//    with a fresh quote in hand.
{
  const s = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  s.setConnectionState("streaming");
  s.ingest(tick(), NOW);
  s.setConnectionState("disconnected");
  const g = s.fillEligibility("EURUSD", NOW);
  check("down transport suspends fills (feed_unavailable)",
    !g.tradable && g.reasons.includes("feed_unavailable"));
}

// 6. No quote at all -> no_quote.
{
  const s = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  s.setConnectionState("streaming");
  check("missing quote suspends fills (no_quote)",
    s.fillEligibility("EURUSD", NOW).reasons.includes("no_quote"));
}

// 7. Future-dated quote (clock skew) cannot be proven fresh -> suspended.
{
  const s = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  s.setConnectionState("streaming");
  s.ingest(tick({ timestamp: "2026-09-24T12:01:00Z" }), NOW);
  const g = s.fillEligibility("EURUSD", NOW);
  check("future-dated quote is treated as unproven (quote_stale)",
    !g.tradable && g.reasons.includes("quote_stale"));
}

// 8. Malformed tick is rejected, never stored.
{
  const s = new QuoteStore();
  let threw = false;
  try { normalizeTick({ symbol: "EURUSD", timestamp: "not-a-date" }); } catch { threw = true; }
  check("unparseable timestamp is rejected", threw);
}

// 9. Conversion uses the observed mid and discloses rate + timestamp; a
//    missing quote refuses conversion instead of inventing a rate.
{
  const s = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  s.setConnectionState("streaming");
  s.ingest(tick(), NOW);
  const fwd = convertMoney(money(10_000, "EUR"), "USD", s, NOW);
  check("EUR->USD uses observed mid", fwd.ok && Math.abs(fwd.result.amount - 11391.9) < 1e-6);
  const inv = convertMoney(money(10_000, "USD"), "EUR", s, NOW);
  check("USD->EUR inverts the observed mid", inv.ok && inv.inverted
    && Math.abs(inv.result.amount - 10_000 / 1.13919) < 1e-6);
  check("disclosure names pair, rate, timestamp and age",
    conversionDisclosure(fwd, s, NOW).includes("EURUSD mid = 1.13919"));

  const empty = new QuoteStore();
  const refused = convertMoney(money(100, "EUR"), "USD", empty, NOW);
  check("conversion without a quote is refused, never assumed",
    !refused.ok && refused.reason === "no_quote_available" && refused.neededSymbol === "EURUSD");
}

// 10. F2: no direct pair -> conversion crosses through USD on two observed
//     legs, both disclosed; a missing leg quote refuses the conversion instead
//     of estimating.
{
  const s = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  s.setConnectionState("streaming");
  s.ingest(tick(), NOW); // EURUSD 1.13919
  s.ingest(tick({
    symbol: "GBPUSD", bid: 1.27294, ask: 1.27306, mid: 1.27300, spread: 0.00012,
  }), NOW);
  const cross = convertMoney(money(10_000, "EUR"), "GBP", s, NOW);
  check("EUR->GBP crosses via USD on observed mids", cross.ok
    && Math.abs(cross.result.amount - 10_000 * 1.13919 / 1.27300) < 1e-6);
  check("cross discloses both legs", cross.legs.length === 2
    && cross.legs[0].rateSymbol === "EURUSD" && cross.legs[1].rateSymbol === "GBPUSD");
  const disc = conversionDisclosure(cross, s, NOW);
  check("disclosure names both pairs and timestamps", disc.includes("EURUSD mid = 1.13919")
    && disc.includes("GBPUSD mid = 1.27300") && disc.includes("2026-09-24T11:59:59"));

  const onlyEur = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  onlyEur.setConnectionState("streaming");
  onlyEur.ingest(tick(), NOW); // EURUSD only, no GBPUSD
  const refused = convertMoney(money(10_000, "EUR"), "GBP", onlyEur, NOW);
  check("cross with a missing leg is refused, never estimated",
    !refused.ok && refused.reason === "no_quote_available" && refused.neededSymbol === "GBPUSD");
}

// 11. S06-F1: degenerate prices (NaN, Infinity, negative, zero, crossed) are
//     rejected exactly like malformed ticks; the last known good quote stays
//     active and the fill gate keeps its semantics.
{
  const s = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  s.setConnectionState("streaming");
  s.ingest(tick(), NOW);

  const degenerate = [
    ["NaN bid", { bid: NaN }],
    ["Infinity ask", { ask: Infinity }],
    ["negative bid", { bid: -5 }],
    ["zero bid", { bid: 0 }],
    ["crossed book (bid > ask)", { bid: 1.2, ask: 1.1 }],
    ["NaN mid", { mid: NaN }],
    ["zero mid", { mid: 0 }],
    ["negative mid", { mid: -1 }],
    ["mid above ask", { mid: 1.2 }],
    ["mid below bid", { mid: 1.1 }],
    ["non-finite spread", { spread: Infinity }],
    ["missing prices", { bid: undefined, ask: undefined, mid: undefined, spread: undefined }],
  ];
  for (const [label, overrides] of degenerate) {
    let threw = false;
    try { s.ingest(tick(overrides), NOW); } catch { threw = true; }
    check(`${label} is rejected, never stored`, threw);
  }
  check("last known good quote stays active after degenerate ticks",
    s.latest("EURUSD")?.bid === 1.13916 && s.latest("EURUSD")?.ask === 1.13922);
  check("fill gate unchanged after degenerate ticks",
    s.fillEligibility("EURUSD", NOW).tradable);
}

// 12. A store that only ever saw a degenerate tick has no quote at all, so a
//     fill against it is suspended as no_quote rather than priced on garbage.
{
  const s = new QuoteStore({ maxQuoteAgeSeconds: 30 });
  s.setConnectionState("streaming");
  let threw = false;
  try { s.ingest(tick({ bid: -5 }), NOW); } catch { threw = true; }
  check("degenerate-only store rejects the tick", threw);
  check("degenerate-only store reports no_quote",
    s.latest("EURUSD") === null
    && s.fillEligibility("EURUSD", NOW).reasons.includes("no_quote"));
}

console.log(failures === 0 ? "\nAll checks passed." : `\n${failures} check(s) FAILED.`);
process.exit(failures === 0 ? 0 : 1);
