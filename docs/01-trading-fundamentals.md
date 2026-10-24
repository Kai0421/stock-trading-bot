# Trading Fundamentals

## Hard rules — NEVER EVER DO RISKY TRADES

> These rules override everything else in this project: strategy ideas, backtest results, agent suggestions and future changes. They are enforced in code, not just written down.

| # | Rule | Enforcement |
|---|---|---|
| 1 | **NEVER EVER do risky trades.** If in doubt, don't trade. | Every gate below must pass, or the order is not sent |
| 2 | **No options.** | Asset whitelist: US common stock and unleveraged ETFs only |
| 3 | **No CFDs, futures, forex, crypto or other derivatives.** | Same whitelist; the broker wrapper has no code paths for them |
| 4 | **No leverage of any kind.** No margin, no short selling, no leveraged or inverse ETFs. | Cash-only sizing against settled cash; leveraged/inverse ETF blacklist |
| 5 | **Every trade needs a success probability of at least 60%; it can never go below 60%.** (Lowered from 70% / 65% by the owner on 2026-10-04 for a paper experiment, and to 60% / 60% on 2026-10-07.) | [Success-probability gate](03-strategy.md#success-probability-gate-min-60): historical out-of-sample win rate ≥ 60% and 95% lower bound ≥ 60%; the code refuses to start with a floor below 0.60 |
| 6 | **Every position has a stop loss at the broker** from the moment it fills. | Bracket orders only; an unprotected position triggers a CRITICAL alert and an immediate stop order |
| 7 | **Small, fixed risk per trade** (1% of equity), with kill switches on daily loss and drawdown. | Risk module; see [strategy risk limits](03-strategy.md#position-sizing--risk-limits) |
| 8 | **Only spend money you actually have.** Check the tradable cash before every order and never commit more than it. Never use borrowed money (margin), unsettled proceeds, or Alpaca's (leveraged) `buying_power`. | [Available-cash check](#available-cash-check-never-spend-more-than-you-have) below, run immediately before every order |

These rules can only be changed by you, through a reviewed commit. AI agents may never weaken them.

Why so strict:
- Options, CFDs and leverage can lose **more than the amount risked**, sometimes more than the whole account, very quickly.
- A long position in a plain stock, with a stop and no leverage, has a known, bounded loss. A gap through the stop is the one exception, and position sizing keeps that survivable.

## Available-cash check: never spend more than you have

Alpaca opens **every account as a margin account**. For accounts with $2,000 or more equity, its `buying_power` field is **2× (or 4× intraday)** the real money ([Alpaca account docs](https://docs.alpaca.markets/us/docs/account-plans)). The bot therefore **never reads `buying_power`, `regt_buying_power` or `daytrading_buying_power`** for sizing.

### Formula (recomputed from a fresh `GET /v2/account` right before each order)

```
tradable_cash = min(cash, non_marginable_buying_power)
              − unsettled_sale_proceeds      # sells not yet settled (T+1), tracked in bot.db
              − reserved_for_open_buy_orders # qty × limit price of unfilled buy orders
              − cash_reserve                 # always keep 2% of equity in cash
```

- If `tradable_cash ≤ 0`, **no new buys.**
- Orders in one batch are processed one at a time. Each order's cost is subtracted before the next order is checked, so the batch total can never exceed `tradable_cash`.

### Rules

| Check | Rule |
|---|---|
| Order cost cap | Entries are **limit orders** (inside bracket orders). Cost = shares × limit price + estimated fees, and it must be ≤ `tradable_cash`. Market orders are never used for entries, because their cost is unknown |
| Share count | Whole shares only, **rounded down**: `shares = min(risk_size, floor(tradable_cash / limit_price), max_position_size)` |
| Max position size | No single position above 20% of equity |
| Unsettled money | Proceeds from a sale are not spent until they settle (T+1 business day). This avoids good-faith violations and any reliance on credit |
| Selling | Never sell more shares than are held, minus shares already in open sell orders. This means no accidental short |
| Fresh data | The account is re-fetched before **every** order; cached balances are never used. If the fetch fails: no order |
| Negative cash | If `cash < 0` at any reconcile (meaning margin was used), send a **CRITICAL** alert, block new buys, and require manual review |
| Broker-side lock (second layer) | At startup, set the Alpaca account configuration to `max_margin_multiplier = "1"` and `no_shorting = true` (long-only), then read it back. If the read-back fails or doesn't match: **no new buys** and a CRITICAL alert |

### Worked example

| Item | Value |
|---|---|
| `cash` | $10,000 |
| `non_marginable_buying_power` | $10,000 |
| Alpaca `buying_power` (ignored) | $20,000 |
| Unsettled proceeds from yesterday's sale | −$1,500 |
| Open unfilled buy order (20 × $100 limit) | −$2,000 |
| Cash reserve (2% of $10,000 equity) | −$200 |
| **`tradable_cash`** | **$6,300** |

A new signal at a $250 limit can buy at most `floor(6,300 / 250) = 25` shares ($6,250), even if risk-based sizing asks for 40.

Alpaca may already deduct open orders from its own balance figures. If so, subtracting them again is a deliberate double count. It only ever makes the bot spend **less**, never more.

### Required tests (written before the code)

- `buying_power` larger than `cash` (margin account) → sizing uses `cash`.
- Unsettled proceeds, open buy orders and the reserve are each subtracted.
- `tradable_cash ≤ 0` → no buy order is produced.
- A batch of signals whose total exceeds cash → later orders shrink or are dropped, and the total stays ≤ cash.
- `cash < 0` → CRITICAL alert and buys blocked.
- Account fetch error → no order.
- Sell quantity > held quantity → rejected.
- Margin/shorting configuration read-back mismatch → buys blocked.

## How profit is made

- **Long**: buy, sell higher. This is the **only** method used in this project.
- **Short selling**, **options**, **CFDs** and **leverage** are other ways traders try to profit. They are **permanently excluded** here by the hard rules above.
- **Dividends** on held shares are a small bonus, not a strategy goal.
- Time horizons: intraday (flat by close), **swing** (days–weeks, this project), position (months), investing (years).

## Where repeatable edges come from

| Edge | Idea | Typical use |
|---|---|---|
| Momentum / trend | Strong stocks tend to stay strong for months | Filter and rank the universe |
| Short-term mean reversion | Sharp pullbacks inside an uptrend tend to bounce | Entry timing |
| Breakouts | Tight ranges that break with volume tend to continue | Alternative entry (later) |
| Risk management | Small, consistent losses; let winners run | Everything |

Most retail accounts fail because of **sizing and missing exit plans**, not entry signals.

## Core risk math

- **R** = entry price − stop price (risk per share).
- **Position size** = (equity × risk %) / R. At 1% risk on $100k with a $4 stop distance → 250 shares.
- **R-multiple** of a trade = P&L / (R × shares). Track every trade in R.
- **Expectancy** = win% × avg win − loss% × avg loss. Must be > 0 **after** slippage and fees.
- **Win rate and expectancy together**: this project requires a 95% lower bound of at least 60% (target win rate 60%) **and** positive expectancy. A 90% win rate is still a losing strategy if the 10% of losses are big enough, so neither metric is enough on its own.
- **Expectancy mode (opt-in, `GATE_MODE=expectancy`)**: the owner's alternative for strategies that win less often but earn more per win (2026-10-05). It drops the win-rate test and instead requires n ≥ 100 out-of-sample trades with a positive 95% lower bound of the mean R multiple, risks at most 0.5% per trade, and only accepts a backtest whose max drawdown is below 6%. A signature is disabled when its last 30 live trades win more than 15 points below its backtest win rate. The kill switches are unchanged. Default stays `win_rate`.
- **Measuring win rate honestly**: use out-of-sample trades only, with at least 100 trades, and judge by the 95% confidence lower bound, not just the average. 70 wins out of 100 trades gives a lower bound of about 60.4%, which just passes the 60% floor (69 wins would fail). The minimum wins needed to pass are:

| Trades | Wins needed | Win rate | 95% Wilson lower bound |
|---|---|---|---|
| 100 | 70 | 70.0% | 60.4% |
| 200 | 134 | 67.0% | 60.2% |
| 300 | 197 | 65.7% | 60.1% |
| 500 | 322 | 64.4% | 60.1% |

- A 50% drawdown needs +100% to recover → cap drawdowns early (kill switches).

## Validating a strategy

- Backtest on 10+ years covering bull, bear and sideways regimes.
- Split in-sample / out-of-sample; prefer walk-forward optimisation.
- Model slippage (e.g. 5–10 bps per side) and realistic fills (next open, not same close).
- Beware overfitting: few parameters, robust to small parameter changes.
- Avoid survivorship bias (universe should include delisted names where possible).
- Key metrics: CAGR, max drawdown, Sharpe / Sortino, win rate, avg R, expectancy, exposure, trade count.

## Rules that affect a bot

- **T+1 settlement** (since May 2024): in a cash account, buying with unsettled proceeds and selling before settlement causes good-faith violations.
- **Pattern Day Trader (PDT)**: margin accounts under $25k are limited to 3 day trades in 5 business days. FINRA has been reforming this rule; **verify current status before going live**. This project uses a cash account only (no margin), and swing trading mostly avoids it anyway.
- **Wash-sale rule**: losses are disallowed for tax if a substantially identical security is bought within 30 days.
- **Trading halts / LULD bands** and **market-wide circuit breakers** (7% / 13% / 20% S&P 500 declines) can block exits — always keep stops broker-side.
