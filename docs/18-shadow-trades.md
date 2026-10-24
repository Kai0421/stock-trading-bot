# Shadow trades (grading every signal)

The engine logs every setup it sees in the `signals` table of `data/bot.db`, with the
gate's estimated win probability and whether the setup was taken or why it was blocked.
`bot.shadow` replays each of those signals on real daily bars with the strategy's own
entry, stop and exits (the same mechanics as the backtester's `simulate_signals`) and
stores the result in a `shadow_trades` table. Idea borrowed from bennyjo/phil, which grades
every forecast it makes, bet or not.

It answers two questions much sooner than real fills can:

- **Calibration.** Does a setup the gate rated 70% really win about 70% of the time going
  forward? The report buckets signals by predicted win probability and shows the
  realised win rate and mean R per bucket.
- **What the rules blocked.** Taken signals next to each rejection reason (failed gate,
  no slots, already held, ...). If blocked setups keep beating taken ones, a rule is
  costing money; if they lose, it's saving money.

It works with `DRY_RUN=true` too, because planned-but-not-sent entries are still logged.

## Run it

```bash
uv run python -m bot.shadow --settle            # grade new signals, print the report
uv run python -m bot.shadow --out memory/shadow.md   # report only, no data keys needed
```

`--settle` needs the Alpaca data keys (same as the backtester) and grades on bars
through yesterday. Each signal is graded once, when its shadow trade has closed.

## Limits

- Analysis only: it never places, changes or cancels orders, and it doesn't feed the
  live gate (that still uses real `trades`).
- 100 nominal shares per signal and the backtester's slippage; no position limits or
  cash, so it measures setup quality, not portfolio P&L.
- Daily (swing) signals only. Signals tagged `mode=intraday` are skipped.
- The ATR recorded with the signal defines 1R. After a stock split the cached bars are
  re-adjusted but that ATR is not, so a signal spanning a split can be graded wrongly.
- Same-day signals are correlated; treat anything under about 100 settled trades as noise.

## In the daily job

The engine's `eod` job grades new signals on bars through the previous session and adds a
compact summary (all-time calibration and taken vs blocked) to the daily review pack.
A failure there is logged as an anomaly and never stops the EOD job.
