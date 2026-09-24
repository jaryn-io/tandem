// Technical indicators computed from feed candles: SMA, EMA, RSI (Wilder) and
// MACD. Pure functions over close-price arrays — display analytics only. An
// indicator output is never connected to order execution: nothing in this
// module can place, modify or fill an order.
//
// All functions return arrays aligned with the input, padded with null until
// enough data exists. Reference values in checks/indicators-analytics.check.mjs
// are pinned against an independent Python implementation.

export function smaSeries(closes, period) {
  const out = new Array(closes.length).fill(null);
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= period) sum -= closes[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

// EMA seeded with the SMA of the first `period` closes, then the standard
// k = 2/(period+1) recursion.
export function emaSeries(closes, period) {
  const out = new Array(closes.length).fill(null);
  if (closes.length < period) return out;
  let prev = 0;
  for (let i = 0; i < period; i++) prev += closes[i];
  prev /= period;
  out[period - 1] = prev;
  const k = 2 / (period + 1);
  for (let i = period; i < closes.length; i++) {
    prev = closes[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

// Wilder's RSI: first average gain/loss is the SMA of the first `period`
// changes, then Wilder smoothing. A period with zero average loss reads 100.
export function rsiSeries(closes, period = 14) {
  const out = new Array(closes.length).fill(null);
  if (closes.length < period + 1) return out;
  let avgGain = 0;
  let avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const ch = closes[i] - closes[i - 1];
    if (ch > 0) avgGain += ch; else avgLoss -= ch;
  }
  avgGain /= period;
  avgLoss /= period;
  out[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  for (let i = period + 1; i < closes.length; i++) {
    const ch = closes[i] - closes[i - 1];
    avgGain = (avgGain * (period - 1) + Math.max(ch, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-ch, 0)) / period;
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return out;
}

// MACD: macd = EMA(fast) - EMA(slow); signal = EMA(signalPeriod) of the macd
// values (seeded by their SMA); histogram = macd - signal. All three arrays
// stay aligned with the input closes.
export function macdSeries(closes, fast = 12, slow = 26, signalPeriod = 9) {
  const len = closes.length;
  const emaFast = emaSeries(closes, fast);
  const emaSlow = emaSeries(closes, slow);
  const macd = new Array(len).fill(null);
  for (let i = 0; i < len; i++) {
    if (emaFast[i] !== null && emaSlow[i] !== null) macd[i] = emaFast[i] - emaSlow[i];
  }
  const macdValues = macd.filter((v) => v !== null);
  const signalValues = emaSeries(macdValues, signalPeriod);
  const signal = new Array(len).fill(null);
  const histogram = new Array(len).fill(null);
  const offset = len - macdValues.length; // index of the first non-null macd
  for (let j = 0; j < signalValues.length; j++) {
    if (signalValues[j] !== null) {
      signal[offset + j] = signalValues[j];
      histogram[offset + j] = macd[offset + j] - signalValues[j];
    }
  }
  return { macd, signal, histogram };
}
