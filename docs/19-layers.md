# Layers: data, decision, execution

The bot's order path is split into three layers. Each job in `bot.engine` (and the
intraday jobs in `bot.intraday.live`) only wires them together:

```
            gather (data)                decide (decision)               execute (execution)
broker  ─►  account, positions,   ─►   plans, signals, entry     ─►   OTO entries, exits,
data    ─►  open orders, bars,         sizes, exits, protective        stops, cancels;
alt-data─►  prices, alt-data,          stops, stop raises,             journal: orders,
journal ─►  evidence, book             kill switches                   fills, trades
```

| Layer | Module | Does | Never does |
|---|---|---|---|
| Data | `bot.gather` | Reads the broker, market data, alt-data, the evidence file and the journal's book; returns plain snapshots (`Book`, `Fresh`, `Flags`, bar frames) | Writes anything or decides anything |
| Decision | `bot.decide` | Pure functions: snapshots in, decisions out (`EntryPlan`, `SignalRecord`, `EntryOrder`, `ExitIntent`, `ProtectAction`, `StopChange`, `IntradayExit`, `Alert`). Calls the pluggable strategies and the risk rules (`bot.risk`, `bot.evidence`) | Imports the broker client, journal, data loaders or notifier |
| Execution | `bot.execute` | The only broker writes (entries, stops, sells, replaces, cancels) with deterministic client order ids; records orders; books fills (`reconcile`); puts a stop back if an exit sell fails | Decides what to trade |

`tests/test_layers.py` enforces the boundaries (the decision layer imports no I/O, the
data layer calls no write methods, only `bot.execute` calls broker write methods).

## Where the existing pieces sit

- **Pluggable strategies stay pluggable.** `STRATEGY` and `TRADING_MODE` pick the
  strategy exactly as before (`bot.strategies`, `bot.intraday.strategies`,
  docs/12-strategies.md). A strategy is used only inside the decision layer:
  `prepare`/`features` for setups, `limit_price`/`stop_for` for entries, `manage` for
  exits and stops. Adding a strategy needs no engine change.
- **Gates.** The evidence gate (`EvidenceGate`, win-rate or `GATE_MODE=expectancy`) and
  `PAPER_TRIAL` are decision inputs: the data layer loads the evidence file and the
  journal's live outcomes, `decide.plan_entries` / `decide.intraday_candidates` apply the
  gate to every setup.
- **Risk and cash.** `approve_buys` sizing runs in `decide.size_entry`, against a
  `Fresh` account snapshot fetched by the data layer **right before every order**
  (docs/01). Entries therefore run as a small loop per order: fetch → size → send.
  Kill switches (`kill_switch`) are decisions; setting the `halt_today`/`paused` flags is
  bookkeeping the engine does.
- **Dry run.** Unchanged and owned by the execution layer's broker: with `DRY_RUN=true`
  `bot.broker.Broker` sends nothing and returns None; the executor still records the
  order (status `dry_run`) and the engine counts its cost against cash for the rest of
  the job.
- **Intraday mode.** Same split: `scan` gathers today's bars, `decide.intraday_manage`
  and `decide.intraday_candidates` decide, `Executor.sell_all` / `submit_entry` act;
  `flatten` uses `decide.flatten_targets`.
- **Reporting** (`eod` state.json and packs, `bot.report`, `bot.shadow`) reads the journal
  after the fact and is outside the order path.

## Proof that nothing changed

The split is a behaviour-preserving refactor. `tests/golden_engine.py` replays 17
scenario groups (51 engine set-ups: a full swing lifecycle, stop hits, partial exits,
failed sells, dry run, unknown and unprotected positions, every premarket and entry
block, EOD kill switches, the tick scheduler, intraday scan / exit / flatten and its
blocks) and records every broker write and account fetch in order, every alert, the
whole journal and the memory files. `tests/golden/engine_trace.json` was recorded from
main **before** the split; `tests/test_layer_equivalence.py` requires the refactored
engine to produce the identical trace.

Regenerate the golden file only for a reviewed behaviour change:
`uv run python tests/golden_engine.py --write`.

## Known behaviour kept on purpose

In intraday mode, if the fresh account fetch before an entry fails, recording that
signal raises (`int(None)`) and the `scan` job ends with a "Job scan failed" alert, as on
main. The refactor keeps this so the trace stays identical; fixing it is a separate,
small change.
