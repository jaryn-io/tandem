# Summary · FX Paper Trading and Options Risk Terminal

**In one line.** A dealing and risk terminal for simulated FX trading on a live public feed, with vanilla options priced by Garman–Kohlhagen and delta hedging from the risk view: seven roles from four model families, one word from the person, nine findings raised by three independent roles and all closed, the main one held open by the Reviewer through four rounds, and a final audit that broke a case Security had already verified; 90 messages, 3h31, 255 checks.

**Language.** The brief and every role wrote in English. The person's only reply was one word.

## What happened

1. **Brief.** Build a locally runnable FX workstation against biquote's public feed for EUR/USD, GBP/USD and USD/JPY: streaming bid and ask with source, age and market state; paper portfolios with market and limit tickets, long and short, fills at the correct side of the quote, an enduring blotter; European calls and puts with explicit assumptions, Greeks, aggregate delta and a spot hedge from the risk view; charts with SMA, EMA, RSI and MACD that never place a trade; P&L that keeps spot marks from observed quotes separate from option marks from the model; persistence, and an empty record on first launch. No quote may ever be manufactured, no trade may ever reach a broker.
2. **Plan.** Seven steps: four Producer steps (market data, portfolios and execution, options and hedging, terminal and analysis), then review, security, audit. Approved with one word.
3. **Build (S01–S04).** The Producer (Kimi K3) delivered the four layers in about 90 minutes, each with its own deterministic check suite: 163 checks at the end of S04, all passing on the Producer's own runs.
4. **Review (S05).** The Reviewer (Claude Opus 5.5) read every module, re-ran the checks, wrote probes against the real modules and drove the application in a browser against the live feed. Verdict: change. Six confirmed defects. The serious one: funding was cash-only, so the default USD portfolio could not open a long USD/JPY position or buy any USD/JPY option, while shorts had no limit at all. Also: no conversion through USD, so a non-USD reporting currency left results permanently unavailable; a chart that never refreshed; a polling fallback stuck on "disconnected" after one failed poll; P&L cards that did not add up to the change in equity; option expiry settling while fills were supposed to be suspended.
5. **Four rounds on one finding.** The Producer fixed five of the six on the first correction. The funding rule took four: each correction was declared complete, and each time the Reviewer found a new counterexample live, including a seventh finding introduced by the first fix, where trades that reduced risk and the risk-view hedge itself were refused. The fourth version, one admission rule for both sides of every fill with an equity test that a short option sale can never escape, held in the code, in the checks and in the browser. Verdict: pass.
6. **Security (S06).** Security (Kimi K3) confirmed the core constraint structurally: the only outbound calls are to the feed, no order or portfolio data ever leaves the machine, the vendored SignalR client is byte-identical to the official release. Then it found the one real weakness: feed prices were accepted without validation. A negative bid filled a sale at −5 and credited 50,000 dollars of free money into the immutable blotter; a NaN price wedged the render loop. Fixed at the single ingest point, verified with 15 adversarial probes.
7. **The audit (S07).** The Auditor (GPT-6 Sol) traced plan, approvals and every correction chain, then tried a case outside the correction's own specification: a tick with a valid bid and ask but a mid of zero. It was stored, marked tradable, and inverse conversion threw. With a mid of 100, conversions returned nonsense and distorted equity and buying power. A ninth finding, corrected and verified by Security with 17 fresh probes. Final suite: 255 checks.
8. **Close.** Session completed and sealed. 32 accepted files. Nine findings, all verified closed. The Positive Memory candidate was rejected by the Reviewer for three factual errors about who found what, and is therefore not published.

## Why it is published

Three things. The Reviewer would not close the main finding on the Producer's word, four times, until the fix held in a browser against live prices. The Auditor, from a different model family, broke a case that Security had corrected and verified, because it looked past the specification the fix had been written to. And the session records its own slips: a Reviewer probe file briefly copied into the deliverable and removed, with the file hashes unchanged, is in the record rather than out of it.

## The application

The application accepted at closeout is published as delivered, with its 255 checks, at [`examples/fx-paper-trading`](../../examples/fx-paper-trading). Run it with one command, open the terminal, place a paper trade against live prices and check the record against the code.

## Files

`00-brief.md` · `01-plan.md` · `02-record.md` · `03-findings.md` · `04-closeout.md`
