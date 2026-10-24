# Intraday mode

The bot has two trading modes, chosen with one setting:

```bash
TRADING_MODE=swing      # default: daily bars, positions held for days (docs/03, docs/12)
TRADING_MODE=intraday   # 5-minute bars, every position closed by 15:50
```

`STRATEGY` must name a strategy of the chosen mode; the bot refuses to start otherwise
(`TRADING_MODE=intraday` with no `STRATEGY` picks `orb_breakout`). Both modes share
everything that is not the trading decision: the risk limits in `RiskConfig`, the
walk-forward probability gate and its evidence files, `approve_buys` sizing and cash
checks (fresh account fetch before every order, settled cash only), OTO entries with a
broker-side stop, the −3% daily halt, the −10% drawdown pause, `DRY_RUN` and `PAPER`.

## How it fits together

```
TRADING_MODE ──► Settings ──► Engine._schedule
                                 │
             swing ──────────────┼────────────── intraday
   premarket 08:30 plan          │      premarket 08:30 evidence check, heartbeat
   entries 09:45 OTO buys        │      scan every 5 min 09:35–15:45:
   manage 10:00–15:30 stops      │        exits/stop raises for today's positions,
   eod 16:15 strategy exits      │        cancel entries unfilled after 10 min,
                                 │        new gated entries on the last completed bar
                                 │      flatten 15:50 (+ retries): cancel buys, sell
                                 │        everything opened today
                                 │      eod 16:15 equity, kill switches, memory
```

- Swing strategies live in `bot.strategies` (daily bars, `Strategy`); intraday strategies
  in `bot.intraday` (5-minute bars, `IntradayStrategy`). The intraday jobs are in
  `bot.intraday.live`, mixed into `bot.engine.Engine`; in intraday mode the swing jobs
  `entries` and `manage` do nothing, and in swing mode `scan` and `flatten` do nothing.
- Live 5-minute bars come from Alpaca's free real-time IEX feed (`LIVE_DATA_FEED=iex`).
  The strategies use prices only: IEX volume is a small slice of the market. Backtests
  use the consolidated SIP feed, so live highs and lows can differ slightly.
- Daily context (yesterday's close, SMA(50)/SMA(200), SPY's regime) always comes from
  sessions *before* today; a test checks no strategy looks ahead.

### Intraday-only rules

- Flat by `flatten_at` (15:50; 10 minutes before an early close). Nothing is held overnight.
- At most one entry per symbol per session (client order id `{date}-{symbol}-ientry`).
- Entry limit = last IEX trade + 0.1%; unfilled entries are cancelled after 10 minutes.
- No new entries below $25,000 equity (the pattern-day-trader rule); on early-close days;
  after the daily-loss halt, which intraday mode also checks during the session.
- Positions opened before today (left over from swing mode, or manual) are never sold by
  `flatten`: they keep their stop and raise a critical alert. **Switch modes only when
  flat.**
- The Mac must be awake 09:30–16:00 ET: if it sleeps through 15:50, positions keep their
  broker stops but are not flattened until the next tick.

## Strategies

All long only, 5-minute bars, one entry per symbol per day, price-based only.

| Strategy | Idea | Exit |
|---|---|---|
| `orb_breakout` (default) | First 5-minute close above the 30-minute opening range, stock above its 50-day SMA, SPY above its 200-day SMA, range under 0.8 daily ATR; entries 10:00–12:00 | Stop at the range low (max 3 bar-ATRs), breakeven at +1R then a 2 ATR trail; flat 15:50 |
| `gap_fill` | Gap down 0.5–3% in a stock above its 200-day SMA, then the first close back above the day's open; entries 09:45–11:00 | Close at or above yesterday's close; stop under the day's low; flat 15:50 |
| `intraday_reversion` | RSI(2) < 5 and 1.5 ATR under the 20-bar EMA in a daily uptrend; entries 10:30–14:30 | Close above the EMA, RSI(2) > 70, or 12 bars (1 hour); 2.5 ATR stop; flat 15:50 |
| `late_momentum` | SPY/QQQ only: first half hour (from yesterday's close) up more than 0.25% | Buy 15:30, sell 15:55; 3 ATR stop |

Research basis, in short:

- **Opening-range breakout:** Zarattini & Aziz (2023) report a profitable 5-minute ORB on
  US "stocks in play", but their selection uses relative volume, which our free IEX feed
  can't measure reliably; this version replaces it with a daily-trend filter. ORB win
  rates are typically 30–45% with a few large winners, so it can only pass the
  `expectancy` gate, never the 60% win-rate gate.
- **Gap fill:** small opening gaps in liquid large caps often retrace toward the previous
  close, especially against the prevailing trend; waiting for a reclaim of the open avoids
  catching gaps that keep falling. High win rate, small average win.
- **Intraday mean reversion:** the daily RSI(2) effect (Connors) applied to 5-minute bars
  inside a daily uptrend. Short holding time, high win rate, sensitive to costs.
- **Intraday momentum:** Gao, Han, Li & Zhou (2018) found SPY's first half-hour return
  predicts its last half-hour return (1993–2013). Later studies find the effect weaker
  after costs; it trades at most twice a day.

Parameters are hypotheses and have not been tuned (memory/decisions.md, 2026-10-06).

### Auction-geometry strategies (`bot.intraday.auction`)

Adapted from Alqaydi (2026), "Harvesting the Value Area: Auction Geometry as a Durable
Intraday Edge in Nasdaq-100 Futures" (SSRN 7546200). The paper trades NQ/MNQ futures
long and short; here only the long side is kept and each stock or ETF gets its own
profile. Levels come from the previous session's 5-minute bars: the POC (the price that
traded the most volume) and the value area (70% of the volume around it). The paper's
"absorption" filter is dropped; its own null test credits it with about 5% of the edge.

| Strategy | Idea (paper section) | Exit |
|---|---|---|
| `va_rotation` | Open inside yesterday's value area below the POC; an up bar closing between VAL and POC (80% rule, s5) | Stop under VAL; sell at the POC |
| `va_rejection` | A probe under yesterday's VAL, then an up bar closing back inside within 3 bars (IARM, s4) | Stop under the probe low; sell at the POC |
| `ib_failure` | After 10:30 a probe under the first-hour low, then an up bar closing back inside (IB auction failure, s7) | Stop under the probe low; sell at the first-hour midpoint |

All three need reward at least 1.2x risk, risk between 0.5 and 3 bar ATRs, entries
10:00 (10:30 for `ib_failure`) to 13:30, flat 15:50. Caveats: the paper's results are
self-published, for one futures market, with tiny tick-sized stops, and its risk section
is built around a prop-firm trailing drawdown. Live, the profile would come from the
free feed's volume (IEX), which is a thin slice of the market; the backtest uses SIP.

### Published strategies (`bot.intraday.published`, 2026-10-07)

The three best-documented intraday results found in a web search, each with costs in the
original study. All are reproduced long-only, without leverage, at the bot's sizing.

| Strategy | Source and claim | This version |
|---|---|---|
| `noise_breakout` | Zarattini, Aziz & Barbon (2024), SPY 2007–2024: 19.6%/yr, Sharpe 1.33, long and short, up to 4x leverage | SPY/QQQ. At each half hour from 10:00, buy a close above max(open, yesterday's close) × (1 + average move from the open at that time over 14 days); sell at a half hour below max(that band, VWAP); flat 15:55 |
| `vwap_trend` | Zarattini & Aziz (2023), QQQ 2018–2023: Sharpe 2.1, 9.4% max drawdown, long above VWAP, short below, 1-minute bars | QQQ/SPY. Buy the first 5-minute close above VWAP, sell on a close below; one entry a day |
| `orb_in_play` | Zarattini, Barbon & Aziz (2024), top 20 "stocks in play" from ~7,000 US stocks, 2016–2023: Sharpe 2.81 | Our 116 large caps. Up first 5-minute bar with opening volume at least its 14-day average: buy the next bar, stop 10% of the daily ATR below, hold to 15:55 |

The papers charged roughly $0.0035–0.0045 per share (under 1 bp on SPY/QQQ); the bot's
backtests charge 10 bps. Each report now prints the average result per trade before and
after costs so the cost effect is visible.

## Backtesting

```bash
uv run python -m bot.intraday.backtest --start 2016-01-01 --end 2026-09-30
uv run python -m bot.intraday.backtest --start 2020-01-01 --end 2026-09-30 --strategies gap_fill --stability
```

Or on GitHub: Actions → **Compare strategies** → mode `intraday`. It fetches 5-minute SIP
bars one symbol at a time (Alpaca's free plan has minute history back to 2016), writes
`memory/backtests/intraday-<id>.md` and evidence for each strategy to
`memory/evidence/<strategy>.json`, which is what the live gate reads.

Fills: entry at the next bar's open plus slippage, stops at the stop price or the gap
open, strategy exits at the next bar's open, flatten at the 15:50 bar's open. Sale
proceeds are not reused the same day (settled cash only). Max drawdown is measured on
end-of-day equity.

## Results (2026-10-06)

Compare workflow run 37530787997, report `intraday-20261006-8b636075`: 2016-01-01 to
2026-09-30, SIP 5-minute bars for 116 symbols (median 2,701 sessions each; a few newer
listings start later, the latest in 2020), live sizing (0.25% risk, 3 positions, 5% per
stock), 10 bps round-trip costs, win-rate gate.

| Strategy | Setups | Win rate | Mean R | Worst / best year (mean R) | Gated trades | Verdict |
|---|---|---|---|---|---|---|
| `orb_breakout` | 56,739 | 42.6% | −0.18R | −0.26R / −0.10R | 0 | REJECTED |
| `gap_fill` | 4,310 | 45.1% | −0.17R | −0.29R / +0.02R (2016 only) | 0 | REJECTED |
| `intraday_reversion` | 76,251 | 39.7% | −0.26R | −0.32R / −0.21R | 0 | REJECTED |
| `late_momentum` | 1,507 | 22.6% | −0.48R | −0.69R / −0.29R | 0 | REJECTED |

Every strategy loses money after costs in essentially every year, and no setup type
(signature) comes near the 62% gate, so the gated portfolio never traded. The main
reason is cost versus stop size: a 5-minute stop on a large cap is often 0.3–0.5% of
price, so 10 bps of round-trip slippage alone is about 0.2R per trade, roughly the whole
loss. Before costs these setups are close to break-even, i.e. no edge.

2022 has far fewer setups than other years (the SPY regime filter was mostly off), which
is the filter working, not missing data.

**Recommendation:** no paper trial of any of these four. Keep swing mode. The switch is
still useful for testing future intraday ideas, which need either a larger expected move
per trade (wider targets, fewer trades) or a filter that lifts the win rate well above
the current ~45%.

### Auction-geometry strategies (2026-10-06)

Compare workflow run 37543041538, report `intraday-20261006-2efd9e27`, same data, sizing,
costs and gate as above.

| Strategy | Setups | Win rate | Mean R | Worst / best year (mean R) | Gated trades | Verdict |
|---|---|---|---|---|---|---|
| `va_rotation` | 4,087 | 34.6% | −0.32R | −0.45R / −0.19R | 0 | REJECTED |
| `va_rejection` | 50,252 | 32.5% | −0.39R | −0.47R / −0.32R | 0 | REJECTED |
| `ib_failure` | 86,690 | 30.3% | −0.39R | −0.48R / −0.31R | 0 | REJECTED |

The paper's NQ futures results do not carry over to long-only stocks and ETFs: most trades
hit the stop before the POC or range midpoint (e.g. `va_rejection`: 32,645 stops, 14,442
targets). The best setup type, a target close to the entry in the morning, still wins
only 37–39% at −0.24R or worse. **Recommendation:** no paper trial; stay on swing.

### Published strategies: results (2026-10-07)

Compare workflow run 37573539508, report `intraday-20261007-53380e80`, same data, sizing,
costs and gate as above.

| Strategy | Setups | Win rate | Mean R after / before costs | Per trade after / before costs | Median stop | Gated trades | Verdict |
|---|---|---|---|---|---|---|---|
| `noise_breakout` | 1,738 | 26.0% | −0.36R / +0.18R | −0.057% / +0.043% | 0.20% | 0 | REJECTED |
| `vwap_trend` | 5,312 | 16.3% | −0.55R / +0.06R | −0.086% / +0.013% | 0.17% | 0 | REJECTED |
| `orb_in_play` | 43,639 | 10.2% | −0.48R / +0.06R | −0.087% / +0.013% | 0.20% | 0 | REJECTED |

All three lose in every year after costs. Before costs each is slightly positive, so the
published effects are real but small: a long-only trade earns 0.01–0.04% of price on
average, while 10 bps of round-trip costs take 0.10%. The papers make money with tighter
costs (under 1 bp), the short side and up to 4x leverage, none of which the bot may use.
`noise_breakout` has the only gross edge worth noting (+0.18R); its best setup type
(midday, gap up, 142 trades) is about break-even after costs at a 37% win rate, far from
the 62% gate.

Swing comparison at the same live sizing (2016 to Sept 2026, $100k): `confluence_reversion`
553 trades, 67.3% wins, +0.041R, +5.5% total (~0.5%/yr), 1.4% max drawdown, ACCEPTED;
holding SPY +347% (~15%/yr), 33.8% max drawdown. **Recommendation:** no paper trial of
any of the ten intraday strategies tested so far; stay on swing.

## Switching to intraday

1. Backtest the strategy (above) and commit its evidence file, or copy it from the
   workflow artifact into `memory/evidence/`.
2. Add a `memory/decisions.md` entry.
3. With no open positions, set `TRADING_MODE=intraday` and `STRATEGY=<name>` in the Mac's
   `.env`. A strategy whose backtest isn't ACCEPTED also needs `PAPER_TRIAL=true`.
4. Switching back: set `TRADING_MODE=swing` after the 15:50 flatten.
