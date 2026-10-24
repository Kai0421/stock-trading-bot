# The bot's pass/fail test

Written on 2026-10-17, before any result exists, so the verdict cannot be bent to fit the numbers.
The thresholds are the owner's to change in a reviewed commit; the code is `bot/success_test.py`
and `uv run python scripts/research/success_test.py` prints today's status (read-only).

## The rule

Count only finished swing trades (since 2026-10-13), after the costs the journal records.

| Verdict | When |
|---|---|
| **Too early** | fewer than 100 finished trades (and not clearly losing, see next row) |
| **FAIL (early)** | 30 or more finished trades and even the best case (upper end of the 95% range) of the average trade, measured in R, is below zero |
| **PASS** | 100 or more finished trades, **and** the lower end of the 95% range of the average trade is above zero, **and** the account beat holding SPY with the same exposure |
| **FAIL** | 100 or more finished trades and either of the PASS conditions is missing |

"Same exposure" means each day SPY's move is scaled by how invested the bot was at the previous
close, so being mostly in cash is not mistaken for skill or for underperformance.

## What happens on each verdict

- **Too early:** keep running unchanged. No rule changes to chase a number.
- **FAIL or FAIL (early):** stop adding features. The owner decides: retire the bot to a research
  project (money to an index fund), or change one thing and restart the count from zero.
- **PASS:** still paper only. Going live is a separate owner decision and AGENTS.md still applies.

## Why these numbers

- 100 trades is the same sample size the repo already demands for a setup's win rate.
- The early-fail check is deliberately strict (it needs the *upper* bound below zero) so a few bad
  trades cannot end a good strategy, while a strategy that is clearly losing is not allowed to run
  for months.
- Paper fills are generous. A PASS on paper is a reason to test harder, not to go live.

Many ideas were tried before this one, so research wins need a stricter bar than this live test does.
