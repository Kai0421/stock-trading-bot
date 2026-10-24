# Upgrading the live bot (also while trades are open)

This page explains, in plain English, how a new version of the bot goes live, what protects
open trades during the switch, and what is **not** switched on yet.

## Why upgrading mid-trade is safer than it sounds

The bot is not one long-running program. Every five minutes Windows starts a fresh run. It reads
its memory (the journal in `data/bot.db` plus what the broker says it holds), acts, and exits.
So there is no hidden state to carry across an upgrade. The only real risks are:

1. swapping files while a run is half-way through, and
2. a new version treating an already-open position differently from the old one.

Stops are placed at the broker (Alpaca), so they keep protecting the shares even if the bot is
switched off for a moment.

## The two ways to upgrade

| | When it is allowed | How it switches |
|---|---|---|
| **Flat upgrade** (today) | account holds nothing and has no open orders | `git pull` in the live folder, between runs |
| **Hot upgrade** (`--holding`) | positions are open, **every one has its stop at the broker**, no buy order is working | an atomic pointer change between release folders |

Both only run inside the safe window: at least 30 minutes after the open and 30 minutes before
the close (so clear of the stop swap, the day's buys, the end-of-day jobs and the overnight buy),
on a trading day, with the last scheduled run healthy and no safety flag set.

## What a hot upgrade does, step by step

`uv run python scripts/deploy_live.py go --holding` (run it from an up-to-date checkout of
`main`, not from the live folder):

1. **Check** the conditions above. If anything is off it says why in plain English and changes
   nothing. A blocked check does not post to Slack.
2. **Post** "Bot upgrade starting" to Slack, naming the positions and share counts.
3. **Build** the new version in its own folder under `releases/`, sharing the one `data/`,
   `logs/`, `memory/` and `.env` (links, never copies; `.env` is copied and checked identical).
   The new version's code must import and its key tests must pass.
4. **Shadow tick.** The same snapshot of the broker's positions and orders, plus a copy of the
   journal, is run through the *current* version's decision logic and the *new* version's
   (protect a position, exit, move a stop, cancel a stale buy). Every broker write is blocked in
   this mode. If the two versions would do anything differently for today's open trades, the
   upgrade stops and nothing changes.
5. **Switch, under the bot's own run lock.** The lock is the same one every bot run takes, so no
   run happens during the switch and the switch never starts while a run is going. Inside it the
   journal is backed up, each open position is stamped with the rules version it was opened under,
   and the `current` pointer moves. A run that finds the lock busy skips harmlessly until the
   next five-minute tick.
6. **Verify.** Wait for the next scheduled run and check that it finished cleanly, no safety flag
   is set, the health report is OK, `TRADING_MODE` is unchanged and every position still has its
   stop. If anything is off the pointer moves back **by itself** and Slack gets a CRITICAL note.
7. **Post** "Bot upgrade complete: all green".

### Rules travel with the trade

Each open position is stamped (in the journal's key/value table, so the database layout does not
change and old code reads it unchanged) with the rules version it was opened under. The helper
`bot.release.rules_version_of` answers "which rules does this position follow?". When a future
version changes an exit rule, it registers a new version, applies it to **new entries only**, and
open positions keep the exit rules they were opened under until they close. Today there is only
one version (`r1`), so nothing behaves differently yet.

## Why not run two bots side by side?

It sounds safe: keep the old bot for the open trades, start the new one for new trades, retire the
old one when it is flat. But both would talk to the **same Alpaca account**. Each bot reconciles
what it believes it holds against the broker, so each would see the other's orders and positions
as strangers: it could cancel or "repair" the other's stops, double-buy, or flag everything as
unprotected. Making that safe means changing how the bot reconciles, which is a riskier change than
the upgrade it is meant to protect. The release folders give the same result (new rules for new
trades, old rules for open ones) with one bot at a time.

## Files

| File | Purpose |
|---|---|
| `src/bot/release.py` | switch window, release pointer, run lock, version stamps, shared-folder links |
| `src/bot/shadowtick.py` | the read-only comparison and the "is it safe to upgrade now" checks |
| `src/bot/deploy.py` | `check` / `go`: the whole guarded flow, with automatic rollback |
| `scripts/deploy_live.py` | the real wiring: Task Scheduler, Alpaca (reads only), Slack, health report |
| `scripts/shadow_tick.py` | runs the shadow tick with another release's decision logic |
| `scripts/run_current.cmd` | launcher that runs whichever release `current.txt` names |

None of these are imported by the order path (`engine`, `decide`, `execute`, `gather`,
`config`); a test enforces it, and the recorded engine traces are unchanged.

## Moving the live bot to the release layout (also with trades open)

Until this is done, only flat upgrades are possible (`deploy_live.py go`, the in-place layout).
The one-time move can run with positions open, in the 15:00-20:30 UK window, with the owner's
go-ahead. It is built so there is still exactly **one journal and one run lock**:

1. Create the layout root (for example `%USERPROFILE%\Development\StockTradingBot-releases`)
   and link it to the live folder:
   `uv run python scripts/deploy_live.py layout --root <root>`. `<root>\shared\data`, `logs` and
   `memory` become links to the live folder's own folders (nothing is copied or moved), and the
   live `.env` is copied beside them (the live `.env` is never edited). Back up the journal first.
2. Run `uv run python scripts/deploy_live.py go --holding --first --root <root> --source <checkout>`
   from an up-to-date checkout of `main`. With no release yet, the old live folder stands in as
   "current": the shadow tick runs the old and the new decision logic on the real open positions
   and stops if they would be treated differently. Under the run lock the journal is backed up,
   positions are stamped with the rules they were opened under, `run_current.cmd` is copied to the
   root and the `tick` task is pointed at it.
3. The script waits for the next tick and checks it ran cleanly, no safety flag is set, the health
   report is OK, `TRADING_MODE` is unchanged and every open position still has its stop at the
   broker and its journal row. If anything is off the `tick` task is pointed back at the old live
   folder by itself and Slack gets a CRITICAL note.
4. The other scheduled tasks keep running from the old live folder; they see the same journal
   through the links, so nothing goes stale. They can follow later.

Roll back by pointing the `tick` task at `scripts\run_job.cmd tick` in the old live folder again.
A change to the live `.env` after the move has to be copied to `<root>\shared\.env` too.

## Older releases

`ReleaseSwitcher.prune(keep=3)` removes old release folders. It unlinks `data`, `logs` and
`memory` first, so shared state is never touched, and never removes the current or previous one.
