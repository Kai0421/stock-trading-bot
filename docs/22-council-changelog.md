# Council of Ricks: rules change log

The Ricks are fixed rules. They do not learn by themselves. "Improving" means we measure
(`scripts/research/council_scorecard.py`), decide on purpose, change a rule, and compare each
version's results separately. Nothing here ever places an order.

## Convention

1. Any change to a Rick's vote rules (`src/bot/ricks.py`), the council thresholds, or the paper
   exit rules (`src/bot/ricks_paper.py`) needs the owner's approval first.
2. In the same commit: bump `RULES_VERSION` in `src/bot/ricks_paper.py` and add an entry below
   (date, version, what changed, why, the evidence, and who approved).
3. The ledger stores the version on every pretend trade, and the scorecard shows results per
   version, so a new version starts its own count toward the 100-trade bar. Old results are never
   edited or deleted.
4. Rule changes are never tuned on the same trades they are judged on: write the rule down first,
   then let new trades arrive.

## Entries

### v1 (2026-10-16, built; not yet running on a schedule)
- Entries: council buy verdicts, bought at the next open, sized like the real bot (1% risk,
  6 positions, 2 per sector, cash only, 2% reserve), 5 bps cost per side.
- Stop: 2 x ATR(14) below the entry, fixed. Checked on the daily low; a gap fills at the open;
  the stop wins a same-day tie.
- Exits: stop, the close of the 5th session, or the next open after the council turns against it
  (a veto, or two Ricks against).
- Trailing stop: written and tested (`trail_stop`) but OFF (`TRAIL_STOP = False`). A one-year test
  of the rule alone (random stocks, 3 a day) showed no clear gain: win rate 45.9% vs 48.8%,
  average return -0.17% vs +0.07% per trade, within the noise. Turning it on would be v2.
- Approved by: pending owner sign-off before the first scheduled run.

### v2 (2026-10-17)
- Skeptic Rick: an unknown earnings date now costs 0.5 (was 0.2). The collector has no earnings
  feed, so every stock had "unknown" and the Skeptic gave a free "for" vote (degree 0.30, exactly
  the bar) on 99% of stocks, which made his vote meaningless (`scripts/research/council_eval.py`
  ablation: removing him changed nothing). Now he is for only when nothing is unknown or wrong.
- Effect on history: replaying the 5 logged days gives the same 8 buys; it can only make the
  council stricter. Trades opened under v1 keep running under v1; new entries are v2 and start
  their own count toward the 100-trade bar.
- Everything else (stops, exits, sizing, trailing stop off) is unchanged from v1.
- Approved by: owner ("upgrade the council"), 2026-10-17.

## Telling the owner

Slack hears about a version change on its own: the first council-paper run after `RULES_VERSION`
moves (from 08:30 ET) posts a standalone "Council of Ricks upgraded to vY" message to the council
channel, and the weekly scorecard (Sundays 18:30 UK, task `council-scorecard`) states the version
in use. Both read `RULES_CHANGES` in `ricks_paper.py`, so a bump needs a plain-English line there
(a test fails without it). A code-only upgrade with no rules change is announced by hand with
`notify --council` when the council folders are moved to the new main.

Council-only messages (`notify --council`) go to `COUNCIL_SLACK_CHANNEL` when it is set, and a
separate council health report (`scripts/council_health.py`, task `council-health`, weekdays 13:45
UK) checks the council's own jobs and data. The bot's health report and the learning-loop digest
keep using `SLACK_CHANNEL`.

## Learning-loop proposals

Ideas from the learning loop (`docs/23-learning-loop.md`) use the same entries, with the
challenger id as the heading (for example `### c-skip-sector-energy`). A tightening idea is a
shadow recommendation; anything that loosens or adds risk also needs a `memory/decisions.md`
entry. The owner approves before any rule changes, and the version is bumped in the same commit.
