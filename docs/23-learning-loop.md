# The learning loop: learning from past mistakes, in shadow

The trading bot and the Council of Ricks are fixed rules. They do not learn by themselves.
The learning loop is how we learn *for* them, safely: it studies finished trades, finds
situations where losses cluster, and tests "what if we had skipped those" on later trades. It
only ever **proposes**. It never changes the live bot, never places an order, and nothing in the
order path (`engine`, `decide`, `execute`, `gather`, `config`) imports it. A test enforces that.

Code: `src/bot/learning.py` (pure functions), `scripts/research/learning_loop.py` (reads the
journal read-only, writes reports), `scripts/research/learning_demo.py` (proof on long history).
Not scheduled. Run it by hand, or schedule it only after the owner approves.

## What it does each time

1. **Post-mortem of every finished trade** (live bot and council practice). One plain sentence
   each, for example: "PLTR: stopped out on 15 Oct for a loss of $1,014 (75 shares, 1.02 times
   the amount risked). The price kept falling afterwards, so the stop did its job." It also
   records how far the trade went for and against us, whether the price came back after a
   stop, the market trend, the opening gap, the volatility and the warning flags.
   Written to `data/research/postmortems.csv`.
2. **Pattern miner.** Looks for situations where losses cluster: sector, market trend, day
   bought, volatility, opening gap, setup type. It *fits on the earlier 60%* of trades and
   *checks on the later 40%* it never saw. A multiple-testing guard (Benjamini-Hochberg, 10%)
   stops a lucky cluster from being called a lesson, because with 70 situations tried, a few
   look bad by luck alone. Fewer than 30 finished trades: it just says "too early".
3. **Challengers.** A candidate change such as "skip sector X" runs beside the current rules on
   the same trades. It is judged only on the later trades, and only when it would have skipped
   at least 20 of them, those skipped trades were clearly losers, it keeps at least half the
   trades, and it improves the average by at least 0.05 times the amount risked. Rules are
   fixed in advance, never tuned on the trades they are judged on.
4. **Weekly digest** in plain English (`learning_loop.py --post`, not sent by default).

## The safety rule: it can only become more careful

| Kind of idea | What the loop may do |
|---|---|
| Skip a sector, setup or situation; require more Ricks to agree; pause | Recommend it in shadow reports. Still **not applied** to live without you. |
| Wider stops, bigger size, looser entries, a new exit, anything unfamiliar | **Proposal only**: needs your reviewed commit and an entry in `memory/decisions.md`. |
| Lower the win-rate gate, remove a stop, raise risk, leverage, order placement | **Refused.** Hard rules in AGENTS.md; only the owner can change them in a reviewed commit. |

Every result carries `applied_to_live = False`. There is no code path that sets it to True.

## Limits, in plain English

- **Small samples.** The bot has made 2 live trades and the council practice account a handful.
  Nothing is learnable yet. The bot's own rule is 100+ trades before a result means anything;
  the loop uses the same bar before trusting a pattern.
- **Overfitting.** Look at enough situations and something always looks bad by chance. That is
  why patterns must hold on later trades and survive the multiple-testing guard. Even then, a
  "lesson" is a hypothesis, not a fact.
- **Real but tiny is not worth acting on.** On 9,000 historical signals, "high volatility" lost
  slightly more often and was confirmed on later trades, but cost only about 0.03 times the
  amount risked. The margin rule correctly refused to propose skipping it.
- **The market changes.** A pattern that held for years can stop. Changes are scored per version
  (below) so a bad one is caught early, and the council demotion rule can still switch it off.
- **It cannot invent a strategy.** It finds where an existing one loses. It will not turn a
  strategy with no edge into one with an edge.

## Change log (shared with the council)

Same convention as `docs/22-council-changelog.md`: any change that comes out of this loop gets a
dated entry (`### <id> (<date>, proposed)`, what, why, evidence, `Approved by:`), the rules
version is bumped, and each version is scored separately from its first trade. Old results are
never edited. `learning.changelog_entry()` writes the entry text.
