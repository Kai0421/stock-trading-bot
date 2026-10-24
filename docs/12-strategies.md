# Pluggable strategies

The trading decision (which stocks to buy, where the stop goes, when to sell) is isolated
behind one interface, `bot.strategies.Strategy`. Everything around it is shared and
identical for every strategy: data loading, the walk-forward probability gate, position
sizing, cash checks, kill switches, order placement, fills and costs in the backtester.
That makes strategies swappable and comparable on equal terms.

```
bars ──► Strategy.features ──► Strategy.entry_tables ──► gate (bot.risk / evidence)
                                   setup, signature, score        │
                                                                  ▼
open position ◄── Strategy.stop_for (entry stop)  ◄──  sizing + cash checks + orders
      │
      └── daily close ──► Strategy.manage ──► exits next open, stop only moves up
```

These are swing strategies (`TRADING_MODE=swing`, the default). Intraday strategies on
5-minute bars have their own interface and registry: see docs/17-intraday.md.

## Registered strategies

| Name | Idea | Exits |
|---|---|---|
| `trend_pullback` (default) | Uptrend pullback with a close above the previous high (docs/03-strategy.md) | ATR stop, breakeven/trail, exit set from `StrategyConfig.exit_set` |
| `rsi2_reversion` | RSI(2) dip while above the 200-day SMA | Close above SMA(5), 10-day time stop, fixed 2.5 ATR stop |
| `analyst_desk` | The analyst desk's technical and market analysts upgrade a stock to Overweight (docs/13-analyst-desk.md) | Technical score below -0.2, 20-day time stop, fixed 2.5 ATR stop |
| `ibs_reversion` | Close near the day's low (IBS < 0.2) on a down day in an uptrend (docs/14-research-strategies.md) | Close above the previous high, IBS > 0.8, 5-day time stop, fixed 3 ATR stop |
| `confluence_reversion` | 2+ of four oversold votes (RSI(2), IBS, losing streak, lower Bollinger band) in an uptrend | Close above SMA(5) or the previous high, 7-day time stop, fixed 3 ATR stop |
| `high_momentum` | 6-month winner breaking out within 5% of its 52-week high | Close below SMA(50), 60-day time stop, 2.5 ATR stop trailed at 3 ATR |
| `kronos_rank` (opt-in experiment) | Kronos model forecasts a rise over 5 days: P(up) ≥ 0.6 and forecast return ≥ 1%, from a forecast cache (below) | 5-day time stop, fixed 2.5 ATR stop |

Not registered (research only): `overnight_hold`, buy at the close and sell at the next
open (docs/19-overnight-hold.md). It fails the hard rules and the bot cannot select it.

### Kronos rank (opt-in experiment)

`kronos_rank` trades only when `STRATEGY=kronos_rank`; no other strategy reads its code or
data, so setting `STRATEGY` back unplugs it. It reads forecasts from
`data/kronos/forecasts.csv` and never runs the model itself. A bar with no cached forecast is
never a setup. The cache is filled by a separate job that needs torch and a checkout of
https://github.com/shiyu-coder/Kronos (pinned commit 67b630e), in the optional `kronos` group:

```bash
uv sync --group kronos                     # torch, einops, huggingface-hub, safetensors
export KRONOS_SRC=~/Development/Kronos      # default
uv run --group kronos python -m bot.kronos --universe sp100 --start 2025-09-02 --end 2026-10-09
```

Each forecast takes about 2 s on an Apple Silicon GPU, so stay on the S&P 100 (about 100
forecasts a night, roughly 4 minutes; a year of history is about 15 hours). Only test from
2025-09 on: Kronos was published in Aug 2025 on undated training data, so earlier backtests
may be contaminated. To trade it (paper): fill the cache, backtest from 2025-09-02 to write
its evidence, then run the job after every close, before the next premarket. Offline tests
found no reliable edge (memory/decisions.md, 2026-10-09), so expect the gate to block most or
all of its setups.

Settings (`.env`, docs/settings.md): `KRONOS_FORECASTS` (cache path), `KRONOS_MIN_P_UP`
(default 0.6), `KRONOS_MIN_FRET` (default 0.01) and `KRONOS_SRC` (the Kronos checkout). The
backtester, compare, evolve, shadow and the engine all read the same values.

## Comparing strategies

```bash
uv run python -m bot.compare --start 2016-01-01 --end 2026-09-30
uv run python -m bot.compare --start 2016-01-01 --end 2026-09-30 --strategies trend_pullback,rsi2_reversion
```

Or on GitHub: Actions → **Compare strategies** → Run workflow.

`--resume-after-days N` (the workflow default is 20) restarts trading N trading days after
a 10% drawdown pause, at half size until the old peak is regained. Without it a backtest
stops for good at its first pause, as the live bot does until the owner reviews it, so
most strategies only get measured until their first bad patch. It changes the
measurement only, never the live kill switch, and every pause is listed in the report.

This backtests each strategy on the same bars and writes one table to
`memory/backtests/compare-<date>-<hash>.md`. It never writes the evidence file, so it can't
change what the bot trades.

### Which strategy should the bot use?

`STRATEGY_CANDIDATES` (in `.env` or a GitHub variable) lists the strategies to consider,
for example `STRATEGY_CANDIDATES=trend_pullback,analyst_desk`. Empty means every registered
strategy. `bot.compare` backtests the candidates and suggests one, printed and at the top
of the report:

1. Only candidates that pass acceptance (the 60% Wilson lower bound with n ≥ 100, positive
   expectancy) can be suggested. If none passes, it says to keep the current strategy.
2. Among those, the highest expectancy wins, then the higher win-rate lower bound.
3. If that's the strategy the bot already trades (`STRATEGY`), it says keep it.

A suggestion never changes `STRATEGY` itself; switching follows the steps below.

To tune a strategy's parameters without overfitting, use `python -m bot.evolve`
(docs/15-evolution.md): each generation must beat the previous one on an unseen year.

Choosing the best of several backtests overfits. Treat the winner as a hypothesis: run it
with `--stability`, check a held-out period with `--history-start`, and only then switch.

## Switching the strategy the bot trades

1. Backtest it, which also writes its evidence for the live gate:
   `uv run python -m bot.backtest --strategy rsi2_reversion --start 2016-01-01 --end 2026-09-30 --stability`
2. Add a `memory/decisions.md` entry (the switch is a strategy change).
3. Set `STRATEGY=rsi2_reversion` in `.env` (or the GitHub environment).

The engine refuses entries if the evidence file was written for a different strategy than
`STRATEGY`, so step 1 can't be skipped by accident. Open positions keep being managed by the
strategy that is configured when the end-of-day job runs, so switch with no open positions.

Evidence lives in `memory/evidence/<strategy>.json` when that file exists (the Backtest
workflow writes it there; tick "commit_evidence" to commit it to the branch), otherwise in
`memory/evidence/signatures.json`. Either way it must be fresher than 90 days.

### Paper trial

The owner can trade a strategy that the backtest did not accept, on paper only, to collect
forward results (`memory/decisions.md`, 2026-10-05). Set the GitHub variables
`STRATEGY=<name>`, `PAPER_TRIAL=true` and `DRY_RUN=false`. The strategy-level verdict is the
only check waived: each setup type still needs n ≥ 100 backtest trades passing the gate,
setups are still disabled by poor live results, and stops, cash checks, sizing limits and
kill switches are unchanged. Trial signals carry `"paper_trial": true` in the journal and
the premarket summary says "paper trial". Judge the trial after about 100 closed trades by
comparing live win rate and mean R with the backtest.

## Adding a strategy

1. Create `src/bot/strategies/<name>.py` with a frozen parameter dataclass and a `Strategy`
   subclass (`rsi2_reversion.py` is a compact example). Implement:
   - `default_config()`: the parameter dataclass (validate it in `__post_init__`).
   - `features(bars)`: OHLCV plus your indicators; must include `atr`.
   - `entry_tables(features, benchmark)`: per symbol, `setup` (bool), `signature` (str
     when there is a setup) and `score` (higher ranks first). Row `i` may only use bars
     `<= i`: no look-ahead.
   - `initial_stop(entry, atr)`: the protective stop, which defines 1R.
   - `on_close(pos, bar)`: exits to execute at the next open and the new stop.
   - Optionally `stability_params` (parameters the ±20% check perturbs) and `description`.
2. Register the class in `registry()` in `src/bot/strategies/__init__.py`.
3. Write its tests first (`tests/test_strategies.py` has the patterns, including the
   no-look-ahead check).

The base class enforces the hard rules for every strategy: a stop strictly between 0 and
the entry, stops that only move up, no exit larger than the position, signatures
namespaced by strategy name so evidence can't mix, and long-only buy setups. Position
limits, cash rules and the probability floor are outside the strategy and can't be
loosened by it.

### Signatures

The probability gate needs at least 100 past trades per signature. Keep signatures
coarse (a few buckets of 2 or 3 values each); too many buckets and no signature will
ever reach n ≥ 100, so the strategy never trades.
