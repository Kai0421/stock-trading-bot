# Overnight hold (`TRADING_MODE=overnight`, paper only)

Buy just before the 16:00 close, sell at the next 09:30 open. Rules: `src/bot/overnight_hold.py`;
engine jobs: `src/bot/overnight_live.py`. The owner chose to paper-trade it on 2026-10-08
and waived the win-rate gate for this mode only (memory/decisions.md). It is not a swing
or intraday strategy: only `TRADING_MODE=overnight` selects it.

## How the bot trades it

| Job (New York time) | What happens |
|---|---|
| `premarket` 08:30 | reconcile, check stops, heartbeat listing what will be sold at the open |
| `open_sell` 09:31-09:34 | for each position bought before today, once the market is open: cancel its stop and sell at market |
| `open_check` 09:35 | book the fills; sell at market anything still held from before today (backup) |
| `close_buy` 15:50-15:56 | pick from bars through yesterday, then a DAY limit buy (limit 1% over the last price) with its stop (5% under the limit) attached, for each pick, after a fresh account fetch and the usual `approve_buys` cash and sizing checks; unfilled buys expire at the close; none before an early close |
| `eod` 16:15 | book the fills and swap each new position's DAY stop for a GTC one, then equity, kill switches, memory |

- Why not a limit-on-close order: on 2026-10-09 Alpaca paper let both limit-on-close buys
  (AMD, NVDA) expire unfilled although each closed below its limit. A marketable DAY limit
  fills on paper, a few minutes and a few cents from the closing price.
- Stops trigger only in regular hours, so the GTC stop placed after the close protects from
  09:30 on and stays until the 09:31 market sale replaces it. Neither can protect the gap
  at the open itself.
- Why not a market-on-open sell: on 2026-10-13 Alpaca's open-auction sell for NVDA was
  accepted at 09:02 and expired unfilled (their docs mark open-auction orders as needing
  sales enablement). The bot noticed at 09:35 and sold at 09:37. The sale is now a plain
  market order at 09:31-09:34, with the 09:35 check as the backup.
- Sizing: 0.25% risk against a 5% stop gives 5% of equity per stock, capped by
  `MAX_POSITION_PCT`. The 2-per-sector limit allows 2 chip stocks a night.
- Kill switches, the cash-only lock, the instrument checks and `DRY_RUN` behave as in
  the other modes. The overnight shadow log (`scripts/research/overnight_shadow.py`)
  keeps recording independently.

## Rules (the "Bot rules" preset of the Overnight Hold Lab dashboard)

| Setting | Default |
|---|---|
| Universe | MU, NVDA, AMD, AVGO, MRVL, QCOM, AMAT, LRCX, KLAC |
| Nights | every night (`night_filter`: also `down`, `up`, `trend` = close > SMA(50), `notfri`) |
| Positions | up to 3 a night, ranked by the average overnight return of the previous 20 nights |
| Size | 5% of equity per stock, total capped at 98% (2% cash reserve) |
| Cost | 10 bps per round trip |

```python
from bot.overnight_hold import OvernightConfig, run_overnight_backtest

res = run_overnight_backtest(bars_by_symbol, OvernightConfig())
res.metrics  # trades, win_rate, win_rate_lower_95, avg_return, cagr, max_drawdown, passes_gate
```

## Backtest findings (2026-10-07, Alpaca SIP daily bars, Oct 2021 to Oct 2026)

- Bot-rules preset: about +2.4%/yr, -12% max drawdown, about 50% of nights win.
  **Fails the 62% win-rate floor** by a wide margin.
- MU alone, whole account: +87% after 0.1% a night, -47% after 0.2%, against about
  +1,399% for simply holding MU. Results are very sensitive to costs, since the edge per
  night is a few basis points and the close/open spreads eat it.
- 2021-2026 was an unusually strong period for chip stocks, and the universe is today's
  large caps (survivorship bias), so these numbers flatter the strategy.

## Why it breaks the hard rules

- **Win rate:** about 50% against the 62% floor (95% Wilson lower bound, n >= 100).
- **No overnight stop:** a broker-side stop only triggers once trading resumes, after the
  open has already gapped through it. "Every position must have a broker-side stop from
  the moment it fills" cannot protect this trade, which is all gap risk.

Sources: the "After-hours gainers that fade at the open" research thread, the Overnight
Hold Lab dashboard, and `data/research/gap_fade.py` (on the owner's Mac, not in git).

## Walk-forward stock selection (2026-10-08)

To remove the hand-picked list, `OvernightConfig(select_top=N)` lets only the top N of
`symbols` trade each month, ranked by their average overnight return over the previous
`select_lookback` (252) nights that ended before that month. `scripts/research/
overnight_walkforward.py` compares the fixed chip list with the top 9 and top 20 of the
bot's universe for every night filter, plus a 20 bps cost run, on cached daily bars, and
reports a simulated $100,000 account over the last 12 months. The universe is still
today's stocks, so survivorship bias remains. Research only: nothing here places orders.

## Shadow log (2026-10-08)

A forward-only record at real prices, with no orders: after each close
`scripts/research/overnight_shadow.py run` logs what "universe top9" (walk-forward) and
"chips fixed" would buy, and after the next open it logs the sale and the return after
10 bps. `... report` prints the track record against the gate (100+ settled trades,
lower bound >= 60%, positive average). On Windows, `scripts\install_windows_task.ps1 -OvernightShadow` schedules it; the log is
`data/research/overnight_shadow.csv`. It reads market data only and never imports or
touches the order path.
