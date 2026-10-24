# Project Rules

## Hard trading rules (never weaken; see docs/01-trading-fundamentals.md)
- NEVER EVER do risky trades. If in doubt, don't trade.
- Long US common stocks and unleveraged ETFs only. No options, CFDs, futures, forex, crypto, short selling, or leveraged/inverse ETFs.
- No leverage or margin. Size positions against settled cash only.
- Only spend money you have: before every order, re-fetch the account and require cost ≤ tradable cash = min(cash, non_marginable_buying_power) − unsettled proceeds − open buy orders − 2% reserve. Never use Alpaca `buying_power` (it includes margin).
- Every trade needs an estimated success probability ≥ 60% (target; lowered from 65% by the owner on 2026-10-07). The hard floor of 60% (95% Wilson lower bound, n ≥ 100, out-of-sample) can only be changed by the owner in a reviewed commit (lowered from 65% to 62% on 2026-10-04 and to 60% on 2026-10-07 for a paper experiment), and positive expectancy is also required. Alternative set by the owner (2026-10-05, opt-in `GATE_MODE=expectancy`): no win-rate test, but the 95% lower bound of mean R must be > 0 (n ≥ 100), risk ≤ 0.5% per trade and backtest drawdown < 6%.
- Paper trial set by the owner (2026-10-05, opt-in `PAPER_TRIAL=true`, paper only): the bot may trade a strategy whose backtest verdict is not ACCEPTED. Every per-signature gate check above, stops, cash checks and kill switches still apply. Never carries over to live trading.
- Every position must have a broker-side stop loss from the moment it fills.
- Overnight mode set by the owner (2026-10-08, opt-in `TRADING_MODE=overnight`, paper only): buys just before the close and sells at the next open with no win-rate gate; its stop is attached to the buy and is swapped for the open sell before 09:28 (a stop can't protect the opening gap). Cash checks, sizing limits and kill switches still apply. Never carries over to live trading.

## General

- Python 3.12, managed with `uv`; tests with `pytest` (write tests first).
- Paper trading only. Never set live trading or remove `PAPER=true` without explicit user approval.
- LLM agents must never place, modify or cancel orders. The order path is deterministic Python only.
- Order path layers (docs/19-layers.md): data `bot.gather` (reads only) -> decision `bot.decide` (pure; strategies live here) -> execution `bot.execute` (the only broker writes).
- Never commit secrets; they live in `.env` (see `.env.example`).
- Design docs live in `docs/`. Strategy parameter changes require an entry in `memory/decisions.md`.
- Market times are America/New_York; use the Alpaca clock/calendar as the source of truth for open/closed.
- Before analysis read `memory/README.md` and `memory/state.json`; follow its write protocol.
- Keep this file ≤ 60 lines; put long workflows in skills. AI reviews read `memory/packs/` only (see docs/11-token-efficiency.md).
