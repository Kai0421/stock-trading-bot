# Strategy: Trend Pullback (daily bars, long only)

> **Current paper experiment (2026-10-04):** entries as below, exits = `mean_reversion_full` (sell everything once in profit and the close is above the SMA(5), or at +1R; 10-day time stop). See `memory/decisions.md`.

Buy short-term pullbacks in established uptrends, only when the broad market is healthy, with fixed-fraction risk and broker-side stops.

> All parameters are **starting hypotheses**. They must be validated by backtest and walk-forward testing before use, and changes recorded in `memory/decisions.md`.

## Hard rules (non-negotiable, enforced in code)

See [Trading fundamentals → Hard rules](01-trading-fundamentals.md#hard-rules--never-ever-do-risky-trades).

- **NEVER EVER do risky trades.**
- **Long shares and plain ETFs only.** No options, CFDs, futures, forex, crypto, short selling, or leveraged or inverse ETFs.
- **No leverage.** Cash only, no margin. Total position value never exceeds settled cash.
- **Every trade needs an estimated success probability of at least 60%** (target; 65% until 2026-10-07). The hard floor is **60%**; only the owner can change it, in a reviewed commit (lowered from 70% / 65% on 2026-10-04 and to 60% / 60% on 2026-10-07 for a paper experiment).
- **Every position has a broker-side stop loss** from the moment it fills.
- **Only spend money you actually have.** Every order's cost must be ≤ tradable cash, checked right before it is sent.

## Universe

- S&P 100 constituents + liquid, **unleveraged** ETFs (SPY, QQQ, sector SPDRs)
- Price > $10, 20-day average dollar volume > $50M
- **Excluded**: leveraged or inverse ETFs (e.g. TQQQ, SQQQ, SPXL, UVXY), ETNs, volatility products, OTC and penny stocks. The asset whitelist is checked again before every order.

## Market regime filter

- No new long entries unless **SPY close > SPY 200-day SMA**.
- Existing positions continue to be managed normally.

## Entry criteria (all must be true, evaluated on the daily close)

1. **Uptrend**: close > 200 SMA and 50 SMA > 200 SMA
2. **Pullback**: RSI(2) < 10 within the last 3 days, OR low touched the 20 EMA within the last 3 days
3. **Trigger**: today's close > previous day's high (pullback resolving)
4. **No earnings** in the next 5 trading days
5. **Probability gate**: the setup's estimated success probability must pass the [success-probability gate](#success-probability-gate-min-60) below
6. **Rank** passing candidates by success probability, then by 6-month momentum (126-day return); take the top N while slots are free

## Success-probability gate (min 60%)

**"Success"** means the trade closes with a **net profit after costs**: commission plus 10 bps round-trip slippage. Breakeven or worse counts as a failure.

How the probability is estimated, without guesswork:

1. Every candidate gets a **setup signature**:
   - market regime: SPY trend and volatility bucket
   - trend strength bucket
   - pullback depth (RSI(2) bucket)
   - distance from the 20 EMA in ATRs
2. The backtester measures the **historical win rate** of all past trades with the same signature, using **out-of-sample / walk-forward results only**.
3. Gate thresholds:

| Check | Requirement |
|---|---|
| Historical matching trades | n ≥ 100 (fewer = not enough evidence = **no trade**) |
| Win rate (point estimate) | ≥ **60%** (project threshold; lowered from 65% on 2026-10-07) |
| Win rate, 95% Wilson lower bound | ≥ **60%** (hard floor; owner-only change; 62% until 2026-10-07) |
| Expectancy of those trades | > 0 after costs (a high win rate with large losses is still rejected) |

- The thresholds live in config, but the code **refuses to start** if `MIN_WIN_PROB_FLOOR < 0.60`. Thresholds can be raised, never lowered below the floor.
- If live or paper results for a signature fall below the floor (60%) over a rolling window of 30 or more trades, that signature is **disabled** automatically and a Slack WARN is sent.
- A probability is a historical estimate, not a guarantee. The stop loss and position sizing still apply to every trade.

Orders are placed the next session at ~09:45 ET (avoid the opening 15 minutes) as **bracket orders with a limit entry** (limit = last price + 0.2%), so the stop exists at the broker from the moment of fill and the maximum cost is known before the order is sent. Unfilled entries are cancelled at 11:00 ET.

## Stop loss

- **Initial stop**: entry − 2 × ATR(14). This defines R.
- At **+1R**: move stop to breakeven.
- After that: trail at the higher of (close − 3 × ATR) and (20 EMA − small buffer). Stops only move up.

## Exits

| Exit | Rule |
|---|---|
| Partial profit | Sell 50% at +2R |
| Trailing stop | Remaining shares exit on the trailing stop |
| Trend break | Daily close < 50 SMA → exit next session |
| Time stop | After 15 trading days, exit if < +0.5R |
| Stop hit | Broker-side stop fills automatically |

High win rates usually come from **taking profits early** and **cutting failed setups quickly**. If the exits above don't reach the success-probability gate in backtests, the backtester also tests these candidate exits:
- Mean-reversion first target: sell 50% when the close is above the 5-day SMA, or at +1R.
- A tighter time stop.

Whichever exit set is chosen must pass both the probability gate and the positive-expectancy check out-of-sample, and the choice is recorded in `memory/decisions.md`.

## Position sizing & risk limits

| Control | Value |
|---|---|
| Risk per trade | 1% of equity |
| Max open positions | 6 |
| Max per sector | 2 |
| Account type | **Cash only**. No margin and no short selling. Alpaca accounts are margin accounts by default, so the bot sets `max_margin_multiplier=1` and `no_shorting=true` and verifies them, and always sizes from cash, never `buying_power` |
| Tradable cash | `min(cash, non_marginable_buying_power) − unsettled proceeds − open buy orders − 2% reserve`, re-fetched before every order. See [Available-cash check](01-trading-fundamentals.md#available-cash-check-never-spend-more-than-you-have) |
| Order cost | Limit entries only; shares × limit price must be ≤ tradable cash; whole shares rounded down |
| Max position size | ≤ 20% of equity per position |
| Gross exposure | ≤ 100% of **settled cash**. Leverage is never used |
| Instruments | Whitelisted US common stocks and unleveraged ETFs only. Any other asset class is rejected before an order is sent |
| Success probability | ≥ 60% target; ≥ 60% hard floor (Wilson lower bound) |
| Daily loss kill switch | −3% equity → no new orders today, alert |
| Drawdown kill switch | −10% from peak equity → pause bot, alert, manual resume |
| Trading windows | No orders in first 15 min or last 10 min of session |
| Early-close days | No new entries |

## Alternative-data features (candidates, must earn their place)

Public trades by notable investors and news are added as **features or vetoes** inside the setup signature. They are never stand-alone triggers. Details are in [10-alternative-data.md](10-alternative-data.md).

| Feature | Role |
|---|---|
| Insider cluster buying (SEC Form 4, ≥ 2 officers/directors, last 90 days) | Positive flag, ranking boost |
| CEO/CFO open-market buy (last 30 days) | Positive flag |
| New activist 13D (last 60 days) | Positive flag |
| Heavy insider selling (last 30 days) | Possible veto |
| Major-news shock (Alpaca/Benzinga news, last 2 days) | Veto: no new entry |
| 13F fund conviction (quarterly) | Slow ranking feature |
| Congress purchases (STOCK Act, ≤ 45-day delay) | Optional experiment |

A feature is kept only if the out-of-sample backtest **with** it still passes the probability gate and **improves expectancy**. All of them use filing timestamps, never trade dates, to avoid look-ahead bias. StockTwits is excluded because its API is closed to new registrations.

## Data needed

- Daily OHLCV bars (Alpaca market data; IEX feed on free plan)
- Earnings calendar (source to be decided; fallback: skip filter in backtest, flag in live)
- Sector classification for the universe
- SEC EDGAR filings (Form 4, 13D/G, 13F): free, no key, ≤ 10 requests per second with a declared User-Agent
- Alpaca News API (Benzinga): included with free Basic market data (200 calls per minute)

## Backtest acceptance criteria (before paper trading)

- **Out-of-sample win rate ≥ 60%**, with the 95% Wilson lower bound ≥ 60%. Below this the strategy is rejected, whatever its other metrics
- Positive expectancy out-of-sample after 10 bps round-trip slippage
- Max drawdown < 20%
- ≥ 100 trades in sample
- Results stable when each parameter is moved ±20%. *Paper experiment (2026-10-04): stability is advisory (`--stability-advisory`); the verdict then reads `ACCEPTED (stability advisory)`.*
