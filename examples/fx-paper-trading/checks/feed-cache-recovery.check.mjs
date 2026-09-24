// Deterministic checks for the candle-cache expiry (S05-F3) and the polling
// fallback's fail-then-recover behaviour (S05-F4). fetch is stubbed; no
// network, no timers. Run:
//   node checks/feed-cache-recovery.check.mjs

import { CandleStore } from "../app/marketdata/candles.js";
import { BiquoteFeed } from "../app/marketdata/feed.js";

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

// --- F3: candle cache expires ------------------------------------------------

{
  let fetches = 0;
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    fetches++;
    return {
      ok: true,
      json: async () => ({
        bars: [
          { openTime: "2026-09-24T10:00:00Z", open: 1.13, high: 1.14, low: 1.12, close: 1.139, tickVolume: 10, isOpen: false },
          { openTime: "2026-09-24T11:00:00Z", open: 1.139, high: 1.14, low: 1.138, close: 1.1392, tickVolume: 12, isOpen: true },
        ],
      }),
    };
  };

  let now = NOW;
  const store = new CandleStore(
    { apiBase: "https://example.test", candleIntervals: ["1h"], defaultCandleLimit: 200, candleCacheMaxAgeMs: 60_000 },
    () => now,
  );

  const first = await store.load("EURUSD", "1h");
  check("first load fetches", first.ok && fetches === 1 && first.bars.length === 2);
  const second = await store.load("EURUSD", "1h");
  check("cached load inside the expiry does not refetch", second.ok && fetches === 1);

  now = NOW + 61_000; // past candleCacheMaxAgeMs
  const third = await store.load("EURUSD", "1h");
  check("expired cache refetches on the next load", third.ok && fetches === 2);

  const forced = await store.load("EURUSD", "1h", { force: true });
  check("force always bypasses the cache", forced.ok && fetches === 3);

  globalThis.fetch = realFetch;
}

// --- F4: polling recovers after a failed poll ---------------------------------

{
  let fail = true;
  let ticks = 0;
  const states = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    if (fail) throw new Error("network down");
    return { ok: true, json: async () => ({ EURUSD: tick() }) };
  };

  const feed = new BiquoteFeed({
    symbols: ["EURUSD"],
    callbacks: {
      onTick: () => { ticks++; },
      onConnection: (state) => { states.push(state); },
    },
    config: { apiBase: "https://example.test", pollIntervalMs: 3_600_000 },
  });

  feed._startPolling(); // enters "polling", first poll fails
  await new Promise((r) => setImmediate(r));
  check("failed poll marks the feed disconnected",
    states.join(",") === "polling,disconnected" && ticks === 0);

  fail = false;
  await feed._pollOnce();
  check("successful poll delivers ticks", ticks === 1);
  check("successful poll restores the polling state",
    states[states.length - 1] === "polling");

  await feed._pollOnce();
  check("healthy polling does not re-emit the state",
    states.join(",") === "polling,disconnected,polling" && ticks === 2);

  await feed.stop();
  globalThis.fetch = realFetch;
}

console.log(failures === 0 ? "\nAll checks passed." : `\n${failures} check(s) FAILED.`);
process.exit(failures === 0 ? 0 : 1);
