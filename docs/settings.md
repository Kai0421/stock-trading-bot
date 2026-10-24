# Settings reference

Every option the bot reads, in one place. Set them in `.env` at the repo root (on the Mac)
or as GitHub repository variables/secrets (Actions). A line in `.env` overrides the
default; a real environment variable overrides `.env`. The bot refuses to start if a
value is invalid or would loosen a hard rule (AGENTS.md).

After changing `.env`, nothing needs restarting: the scheduler (Task
Scheduler on Windows) runs the bot every 5 minutes and each run reads `.env` fresh. Change one thing at a time, and run a job with
`DRY_RUN=true` first when you change the mode or strategy.

## Host

| Setting | Default | Values | What it does |
|---|---|---|---|
| `BOT_HOST` | (this machine) | `mac`, `windows` | The machine that runs the bot. `uv run python -m bot.host install` installs that host's scheduler (Task Scheduler on Windows; the Mac launchd files are not in this copy) and `uninstall` removes it. When set, every engine run on a different OS stops with an error before touching the broker, so a copied `.env` can't make the wrong machine trade. Leave empty on GitHub Actions. |

## Safety switches

| Setting | Default | Values | What it does |
|---|---|---|---|
| `PAPER` | `true` | `true` only | Paper account. `false` is refused: live trading needs the owner's explicit approval and a code change. |
| `DRY_RUN` | `true` | `true` / `false` | `true`: every job runs and logs what it would do, but no order reaches Alpaca. `false`: paper orders are sent. |
| `PAPER_TRIAL` | `false` | `true` / `false` | Trade a swing strategy whose backtest verdict is not ACCEPTED (owner, 2026-10-05). Per-setup win-rate checks still apply. |
| `AUTO_BACKTEST` | `true` | `true` / `false` | Swing mode: every Saturday at 11:00 ET, rerun the standard backtest when the gate's evidence is 70+ days old, so it never reaches the 90-day limit. The new verdict decides trading as a manual run would (decisions.md 2026-10-24). |
| `SENTIMENT_EXIT` | `false` | `true` / `false` | Swing mode only: sell a held stock when a fresh Stocktwits snapshot is a `sell_warning` (docs/20-sentiment.md). Selling only; needs the collector job. |
| `SENTIMENT_MAX_AGE_MIN` | `90` | whole minutes, >= 1 | Snapshots older than this are ignored (a stale file sells nothing). |

## What and how the bot trades

| Setting | Default | Values | What it does |
|---|---|---|---|
| `TRADING_MODE` | `swing` | `swing`, `intraday`, `overnight` | **swing**: daily bars, holds for days (docs/03). **intraday**: 5-minute bars, flat by 15:50 (docs/17). **overnight**: buys just before the close, sells at the next open (docs/19-overnight-hold.md); paper only, no win-rate gate (owner waiver 2026-10-08). One mode at a time. |
| `STRATEGY` | swing: `trend_pullback`; intraday: `orb_breakout`; overnight: `overnight_hold` | see below | The strategy the chosen mode trades. Must belong to that mode. |
| `UNIVERSE` | `sp100` | `sp100`, `sp500` | Stock list for swing mode: the S&P 100 plus sector ETFs, or the S&P 500 plus the same ETFs. Needs backtest results for the same list (`--universe`). |
| `GATE_MODE` | `win_rate` | `win_rate`, `expectancy` | **win_rate**: a setup needs 100+ past trades with a 95% lower bound of the win rate of at least 60% and positive average result. **expectancy**: no win-rate test, but the lower bound of the average result must be above 0, risk is capped at 0.5% per trade and backtest drawdown must be under 6%. |
| `EARNINGS_POLICY` | `flag` | `flag`, `strict` | `flag`: trade when the earnings date is unknown and mark it. `strict`: skip those setups. |
| `ALTDATA_FEATURES` | (none) | comma list of `news_shock_veto`, `insider_heavy_selling_30d`, `insider_cluster_buy_90d`, `insider_ceo_cfo_buy_30d`, `activist_13d_60d` | Alternative data from news and SEC filings (docs/10). The first two veto setups; the others are recorded for research. Off until backtested. |
| `STRATEGY_CANDIDATES` | (all) | comma list of swing strategies | Which strategies `python -m bot.compare` tests and suggests from. Never switches `STRATEGY` by itself. |

Strategies by mode:

- **swing**: `trend_pullback`, `confluence_reversion`, `rsi2_reversion`, `ibs_reversion`,
  `high_momentum`, `analyst_desk`, `kronos_rank` (opt-in experiment; docs/12, docs/14).
- **intraday**: `orb_breakout`, `orb_in_play`, `gap_fill`, `intraday_reversion`,
  `late_momentum`, `noise_breakout`, `vwap_trend`, `ib_failure`, `va_rejection`,
  `va_rotation` (docs/17).
- **overnight**: `overnight_hold` only. Its rules are fixed in code: up to 3 of MU, NVDA,
  AMD, AVGO, MRVL, QCOM, AMAT, LRCX, KLAC a night, ranked by their last 20 nights, 5% of
  equity each. The 2-per-sector limit means at most 2 a night in practice, since they're
  all chip stocks.

Swing and intraday strategies only trade setups that pass the backtest gate, which needs a
backtest results file: `python -m bot.backtest --strategy NAME [--universe sp500]`.

`kronos_rank` settings (only read when `STRATEGY=kronos_rank`; docs/12-strategies.md):

| Setting | Default | Options | What it does |
|---|---|---|---|
| `KRONOS_FORECASTS` | `data/kronos/forecasts.csv` | a file path | The forecast cache the strategy reads and `python -m bot.kronos` writes. |
| `KRONOS_MIN_P_UP` | `0.6` | 0.5 to 1 | Buy only when at least this share of Kronos' sampled paths end above today's close. |
| `KRONOS_MIN_FRET` | `0.01` | 0 or more | Buy only when the mean forecast return over 5 days is at least this (0.01 = +1%). |
| `KRONOS_SRC` | `~/Development/Kronos` | a folder | Kronos repo checkout used by the forecast job. |

Changing a threshold changes the strategy, so backtest again before trading it.

## Position size and risk

These can only be made stricter than the ceiling. Looser values stop the bot from starting.

| Setting | Default | Ceiling | What it does |
|---|---|---|---|
| `RISK_PER_TRADE` | `0.01` | `0.01` | Fraction of equity lost if a stop is hit. `0.0025` = 0.25%. |
| `MAX_POSITION_PCT` | `0.20` | `0.20` | Largest single position as a fraction of equity. |
| `MAX_OPEN_POSITIONS` | `6` | `6` | Most positions held at once. |

Fixed in code (not settings): at most 2 positions per sector, a 2% cash reserve, a 3%
daily-loss kill switch (no new buys that day), a 10% drawdown pause (manual resume with
`python -m bot.engine resume`), 10 bps assumed round-trip slippage, and a 60% win-rate
floor for swing and intraday modes.

## Data

| Setting | Default | What it does |
|---|---|---|
| `LIVE_DATA_FEED` | `iex` | Real-time quote feed. `iex` is free; `sip` needs a paid Alpaca plan. |
| `SEC_USER_AGENT` | (none) | Name and email sent to SEC EDGAR (required by the SEC for filings data). |
| `QUIVER_API_KEY` | (none) | Listed in `.env.example`; not read by the bot yet. |
| `BOT_DB` | `data/bot.db` | The bot's SQLite journal. |

## Accounts and alerts (secrets: never commit them)

| Setting | What it does |
|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca paper account keys. |
| `SLACK_WEBHOOK_URL` or `SLACK_BOT_TOKEN` | Where alerts go (docs/06). |
| `SLACK_CHANNEL` | Slack channel for alerts (default `#trading-bot`). |
| `CLAUDE_ROUTINE_DAILY_URL`, `CLAUDE_ROUTINE_DAILY_TOKEN` | Trigger for the daily AI review (docs/07). |
| `CLAUDE_ROUTINE_WEEKLY_URL`, `CLAUDE_ROUTINE_WEEKLY_TOKEN` | Trigger for the weekly AI review. |
| `CLAUDE_USAGE_SNAPSHOT` | File the usage watcher reads before firing AI reviews (docs/08). |

## Command-line options for research

These don't change what the bot trades. Backtests write reports to `memory/backtests/`.

| Command | Useful options |
|---|---|
| `python -m bot.backtest` | `--start`, `--end` (required); `--strategy NAME`; `--universe sp100\|sp500`; `--stability` (±20% parameter check); `--stability-advisory`; `--set name=value` (try a parameter); `--no-gate`; `--history-start` (held-out test); `--evidence-out PATH` |
| `python -m bot.compare` | `--start`, `--end`; `--strategies a,b`; `--universe` |
| `python -m bot.intraday.backtest` | intraday strategies on 5-minute bars (docs/17) |
| `python -m bot.engine JOB` | run one job by hand: `premarket`, `entries`, `manage`, `eod`, `scan`, `flatten`, `open_sell`, `open_check`, `close_buy`, `tick`, `status`, `resume` |

## Common changes

- **Trade the S&P 500 instead of the S&P 100 (swing):** `UNIVERSE=sp500`.
- **Switch strategy (swing):** backtest it first, then `STRATEGY=name` with no open positions.
- **Overnight trading:** `TRADING_MODE=overnight` (the bot then stops taking swing setups).
- **Back to swing:** remove `TRADING_MODE` or set `TRADING_MODE=swing`, with no open positions.
- **Pause sending orders without stopping the bot:** `DRY_RUN=true`.
