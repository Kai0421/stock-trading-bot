# Council of Ricks (sentiment buy council)

Branch `council-of-ricks`, started from `sentiment-shadow` (it needs `bot.sentiment`).

**Status (2026-10-11):** the voting core is built and tested in `bot.ricks` (pure functions, no
I/O, nothing imports it yet). Not wired into the bot, not shadow-logged, no data yet.
The owner confirmed the third member is Skeptic Rick.
Owner's idea (2026-10-11): three members vote on whether a stock becomes a buy plan, an odd
number so the vote can break a tie. Buying only; the sell side is `SENTIMENT_EXIT`
(docs/20-sentiment.md).

## What the council is allowed to be

- **Deterministic Python voters.** The order path stays code (AGENTS.md: LLM agents never place,
  modify or cancel orders). A model may read text in the collector job and write numbers to a
  file; a voter only reads those numbers and applies fixed rules.
- **A candidate generator, not a gate bypass.** A council "buy" becomes a plan like any other
  setup. It still needs the per-signature evidence gate (estimated win probability >= 60%, 95%
  Wilson lower bound, n >= 100, positive expectancy), a broker-side stop, the cash check, sizing
  caps and the kill switches. Until the council has a logged track record there is no evidence
  for its signature, so the gate blocks it: that is the intended order of work.
- **Shadow first.** Log every vote and the price afterwards (like the Stocktwits log) and
  judge it offline before it can plan a trade.

## The three members

Each returns a degree from -1 (strongly against) to +1 (strongly for), and a reason string.

1. **Crowd Rick (sentiment).** Stocktwits score, message-volume surge and the tagged-post net from
   `bot.sentiment`; discounts spam-heavy or tiny samples. Degree grows with how far the score
   sits above neutral and how unusual the volume is.
2. **Rick C127 (price and volume).** The stock's own daily bars: trend (price vs SMA50/200),
   short-term momentum, relative volume and distance from a recent high. Degree is positive
   when sentiment is being confirmed by price, negative when price is breaking down.
3. **Skeptic Rick (risk and regime), confirmed by the owner.** Looks for reasons *not* to buy: SPY below its
   200-day SMA, stock already extended (gap or move over about 2.5 ATR), earnings inside the
   holding period, thin liquidity, sector already full, or hype with no price response (the
   classic pump pattern). Chosen so the three views differ: crowd, tape, context. It is the
   member most likely to say no, which suits a strategy whose sell side is already cautious.

## Vote rule (proposed)

- A member votes **for** at degree >= +0.3, **against** at <= -0.3, otherwise **abstains**.
- Buy plan needs at least 2 **for** votes, **no** member at <= -0.7 (a strong objection is a
  veto) and a mean degree >= +0.3. Abstain counts as not-for, so ties cannot buy.
- The combined degree decides priority among several buys, not size: sizing stays with
  `approve_buys` and the risk caps.
- Every vote is logged: member, degree, reason, price, time, outcome later.

## Build order

1. ~~Voter interface and tally; the three members~~ done in `bot.ricks` (32 tests).
2. Feed it: a function that builds the Skeptic's `RiskContext` from data the bot already loads
   (SPY bars, ATR, average dollar volume, the book's sector counts) plus an earnings-date source.
3. Shadow log across the universe: every vote, the price afterwards. 4-6 weeks, then an offline
   report: do council buys beat the average stock after costs, and does each Rick add something?
4. Only then: a strategy that plans from council buys, gated like any other.

## Open questions for the owner

- The vote rule (2 of 3 for, veto at -0.7, mean >= +0.3) and every weight inside the Ricks are
  guesses; the shadow log tunes them offline, never the live bot.
- Earnings dates: the Skeptic treats an unknown date as a mild red flag. A real source is needed.
- Horizon: intraday, swing, or both? Stocktwits reacts fast, so the first test should be 1-5 days.

## Shadow log and self-correction (built on branch `sentiment-lab`, nothing scheduled)

- `bot.ricks_shadow` + `scripts/research/council_shadow.py`: once a day after 16:20 ET the Ricks
  are convened on every S&P 100 stock using the collector's fresh snapshots; one row per stock
  goes to `data/research/council_shadow.csv`, and older rows get their 1- and 5-day returns
  (entry at the next open). `report` shows buys against the average stock and each Rick on his
  own (ablation). A day with no fresh snapshot is not recorded.
- Skeptic inputs now come from bars: SPY vs its 200-day average, today's move in ATRs, 20-day
  dollar volume, sector count. The earnings date is still unknown (he is penalised for that).
- The council cannot buy unless Crowd Rick is for (`require_crowd`): price and risk alone are
  not a sentiment signal.
- `ricks_shadow.demotion` is the rule for a future capped paper trial: switch the council off
  after a losing streak of 5, a 5R drawdown, a negative average over 20+ trades, or stale
  sentiment. It can only turn the council off, never on.
- Proposed (NOT applied) owner wording for AGENTS.md if a trial is ever wanted: "Council trial
  (opt-in `GATE_MODE=council_trial`, paper only): council candidates may skip the win-rate test;
  stops, cash checks, sizing (max 1 council position, 0.25% risk), kill switches and the
  demotion rule still apply. Never carries over to live trading." The hard rule is unchanged
  until the owner commits that change.
