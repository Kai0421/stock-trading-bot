# Strategy evolution (self-improvement)

`python -m bot.evolve` tunes a strategy generation by generation, and a new generation
only takes over when it beats the previous one on data neither has seen. It is the
walk-forward, champion/challenger process the research supports; genetic searches and
re-optimising on the whole history mostly find noise (Allen & Karjalainen 1999; Sullivan,
Timmermann & White 1999; Bailey & López de Prado).

## One generation per test year

```
          training: trades that exited before Y (minus a 10-day embargo)
                 │
champion ──► champion + ±20% nudges + random challengers ──► best on training = challenger
   │                                                                     │
   └──────────── both scored on year Y (unseen) ◄────────────────────────┘
                                │
          challenger wins by the margin with ≥ 30 trades? ── yes ──► champion for Y + 1
                                │ no
                                └──► champion stays
```

- **Score:** the active gate's conservative statistic on signal trades (every setup,
  ungated): the 95% Wilson lower bound of the win rate (`GATE_MODE=win_rate`; negative
  expectancy ranks last) or the 95% lower bound of mean R (`GATE_MODE=expectancy`).
- **Generation 0** is the strategy's defaults. The champion trading year `Y` was chosen
  with data from before `Y` only, so the chained lineage is fully out-of-sample.
- **Mutations** move only the strategy's `stability_params`, by the same ±20% the stability
  check uses, so a new generation stays in the region where the strategy should be robust.
- **Trials are counted.** The report shows how many parameter sets were tried and a
  t-statistic; with many trials, a t-statistic below 3 is weak evidence.

## Reading the report

`memory/backtests/evolve-<date>-<hash>.md` compares the same unseen years three ways:

| Approach | Meaning |
|---|---|
| Static defaults | Never change the parameters |
| Re-optimise every year | Always trade the newest training-set winner |
| Champion lineage | Switch only after a challenger wins an unseen year |

Self-improvement is working when the lineage beats the static defaults. It then lists the
final champion and the `--set` overrides to backtest it.

## Adopting a champion

The report never changes what the bot trades. To use a champion:

1. `uv run python -m bot.backtest --strategy NAME --set k=v ... --stability`
2. Add a `memory/decisions.md` entry (it is a strategy parameter change).
3. Change the strategy's defaults in code in a reviewed PR (only Trend Pullback reads
   parameters from settings).

## Running it

```bash
uv run python -m bot.evolve --start 2016-01-01 --end 2026-09-30 --strategies ibs_reversion
```

`--first-test-year` (default: start year + 3), `--challengers` (8) and `--margin` (0) tune
the process. On GitHub: Actions → **Compare strategies** with the evolve option ticked.
