# StockTradingBot

Author: Kai0421

A deterministic, paper-trading-first trading bot for US equities (swing, intraday or overnight mode), built on the Alpaca API, run every 5 minutes by Windows Task Scheduler, with Slack alerts and a persistent memory layer for AI agents (Devin CLI) that review, backtest, and research — but never place orders.

> **Status:** roadmap steps 1–9 implemented and unit-tested; running on the Alpaca paper account (step 10). Paper trading only.
>
> **About this copy:** a shared snapshot of the bot's code, without its git history, trading data or run logs. The Mac (launchd) setup files are not included; the bot is set up on Windows. Folders the bot writes to (`memory/backtests/`, `memory/journal/`, `memory/packs/`, `memory/weekly/`, `reports/`, `data/`, `logs/`) are created on the first run.
>
> **Disclaimer:** This is a personal research project, not financial advice. No strategy is guaranteed to be profitable. Run on paper for months before risking real capital.

## Hard rules — NEVER EVER DO RISKY TRADES

- **No options, no CFDs, no futures, no crypto, no short selling, no leverage or margin, no leveraged/inverse ETFs.** Long US stocks and plain ETFs only, sized against settled cash.
- **Every trade needs a success probability ≥ 60%**, and it can **never go below 60%** (95% lower bound on out-of-sample win rate). Positive expectancy is also required. *(Lowered from 70% / 65% by the owner on 2026-10-04 for a paper-trading experiment, and to 60% / 60% on 2026-10-07; see `memory/decisions.md`.)*
- **Every position has a broker-side stop loss.**
- **Only spend money you actually have.** Tradable cash is checked right before every order; an order's cost can never exceed it. Alpaca's margin-inflated `buying_power` is never used.

Details: [Trading fundamentals → Hard rules](docs/01-trading-fundamentals.md#hard-rules--never-ever-do-risky-trades).

## Decisions

| Area | Choice |
|---|---|
| Broker / API | Alpaca (paper account) via `alpaca-py` |
| Style | One mode at a time (`TRADING_MODE`): swing on daily bars (default), intraday on 5-minute bars, or overnight (buy at the close, sell at the open) |
| Host | `BOT_HOST=windows`: a Windows laptop (Task Scheduler) |
| Account | Paper only for now (cash / PDT guards kept configurable) |
| Alerts | Slack Free plan (webhook + bot token for charts); macOS notification fallback; two-way Slack app optional later |
| Reporting | Weekly dashboard every Saturday (charts, trade review, improvement proposals) |
| Extra signals | SEC insider buys (Form 4), activist 13D, news vetoes; each must pass the backtest gate. StockTwits excluded (API closed) |
| Memory | SQLite bot state + git-versioned markdown agent memory |
| Language | Python 3.12, managed with `uv`, tested with `pytest` |

## Documentation

1. [Trading fundamentals](docs/01-trading-fundamentals.md) — how profit is made, edges, risk math, rules that matter
2. [NYSE hours & holidays](docs/02-market-hours-holidays.md) — sessions, 2026–2027 holidays and early closes
3. [Strategy: Trend Pullback](docs/03-strategy.md) — entry, stop, exit and risk rules
4. [Automation: scheduling & subagents](docs/04-automation.md) — Task Scheduler/cron, job schedule, where AI fits
5. [Persistent memory](docs/05-memory.md) — bot state DB + agent memory structure
6. [Alerts & notifications](docs/06-alerts.md) — Slack (free), the separate notifier package, channels by category, the self-check, Telegram/Discord/ntfy alternatives
7. [Agent runtimes: local vs remote](docs/07-agent-runtimes.md) — Claude Code Routines, Desktop tasks, Devin local/cloud
8. [Claude Pro usage limits & routines](docs/08-claude-pro-usage-limits.md) — verified limits and rules so AI usage never affects trading
9. [Weekly dashboard report](docs/09-weekly-report.md) — charts, trade review, improvement proposals every weekend
10. [Alternative data](docs/10-alternative-data.md) — StockTwits, insider/Congress/hedge-fund trades, news, and how they plug into the strategy
11. [Token efficiency](docs/11-token-efficiency.md) — review packs, tight prompts, caching and model rules so every AI run stays cheap
12. [Pluggable strategies](docs/12-strategies.md) — the strategy interface, comparing strategies, adding a new one
13. [Analyst desk](docs/13-analyst-desk.md) — rule-based analyst ratings and the `analyst_desk` strategy
14. [Research strategies](docs/14-research-strategies.md) — `ibs_reversion`, `confluence_reversion` and `high_momentum`
15. [Strategy evolution](docs/15-evolution.md) — walk-forward generations that must beat the previous one on unseen data
17. [Intraday mode](docs/17-intraday.md) — `TRADING_MODE=intraday`: 5-minute strategies that are flat every night
18. [Dashboard](docs/18-dashboard.md) and [shadow trades](docs/18-shadow-trades.md)
19. [Order-path layers](docs/19-layers.md) — gather → decide → execute
20. [Overnight mode](docs/19-overnight-hold.md) — `TRADING_MODE=overnight`: buy just before the close, sell at the next open (paper only)
21. [Upgrading the live bot](docs/24-hot-upgrade.md) — flat-account upgrades, upgrades while holding, automatic rollback
22. **[Settings reference](docs/settings.md)** — every configuration option, its default and allowed values

## Architecture at a glance

```
   Task Scheduler, every 5 min (ET-guarded jobs)
                      │
   ┌──────────────────▼──────────────────┐
   │  Python trading engine (no LLM)     │
   │  calendar → data → strategy → risk  │──► Alpaca (paper)
   │            → broker → journal       │
   └───────┬──────────────────┬──────────┘
           │                  │
      data/bot.db        notifier ──► Slack (macOS fallback)   (separate package, see below)
           │
   report.py (Sat) ──► reports/weekly/ ──► Slack charts
           │
   memory/state.json ◄── export at EOD
           │
   Devin agents (review / backtest / research) — read & write memory/ only
```

## Notifications are a separate package

`src/notifier/` sends every Slack message for the trading bot and the Council of Ricks. It does
not import anything from `src/bot/` (a test enforces this), has its own settings and its own
state file, and never raises into the caller, so it can be changed, tested and deployed without
touching the trading code. The bot only decides *what to say* (`bot/notify.py`, `bot/health.py`,
the council's `bot/ricks_*.py`); the notifier decides *where it goes and whether it looks right*.

| Category | Channel setting | What goes there |
|---|---|---|
| `bot` | `SLACK_CHANNEL` (`#trading-bot`) | plans, buys, sells, stops, trading alerts, deployments |
| `council` | `COUNCIL_SLACK_CHANNEL` (`#council-of-ricks`) | pretend trades, picks, upgrade notices |
| `general` | `GENERAL_SLACK_CHANNEL` (`#general`) | health reports, weekly digests, scorecards, milestones |

An unset channel falls back to `SLACK_CHANNEL`. Before sending, a message is read by the
self-check: reports that look broken (`None`, `nan`, unfilled braces, raw internal codes, a win
chance over 100%) are held back with a notice to `#general`; warnings and critical alerts are
never held back, they are sent with the problem written on them. Lists are bullets, one per
line, and rows are aligned tables (`notifier/layout.py`). See [docs/06-alerts.md](docs/06-alerts.md).

**Optional queue (`NOTIFY_VIA_OUTBOX=true`, off by default).** The bot only writes each message to a
small queue file and a separate task (`python -m notifier drain`, one minute apart) sends it, so
Slack being slow, or the notifier being changed or broken, cannot delay a trading job. A critical
alert still unsent after 3 minutes is sent directly by the bot's next job.


```
StockTradingBot/
  pyproject.toml
  .env.example
  src/notifier/      # the notifier: sending, channels, checks, layout. Imports nothing from bot/
    core.py          # Slack bot token / webhook / macOS fallback, Dispatcher (routes, dedupe, self-check)
    config.py        # its own settings: tokens and the three channels, read from .env / environment
    state.py         # its own small SQLite file (de-duplication, thread ids); never the trading journal
    checks.py        # holds back or flags messages that look broken
    layout.py        # bullets (one per line) and aligned tables
    __main__.py      # python -m notifier --category general --title T "text"
  src/bot/
    config.py        # params, risk limits
    calendar.py      # market open/close, holidays (Alpaca + fallback)
    data.py          # bars via Alpaca market data, caching
    indicators.py    # SMA / EMA / RSI / ATR
    strategy.py      # Trend Pullback rules (pure functions)
    strategies/      # Strategy interface + registry of swing strategies (docs/12, docs/14)
    compare.py       # backtest several strategies side by side
    risk.py          # sizing, limits, kill switches
    broker.py        # Alpaca wrapper, bracket orders
    engine.py        # job entrypoints: premarket, entries, manage, eod
    gather.py        # data layer: every read (docs/19-layers.md)
    decide.py        # decision layer: pure functions
    execute.py       # execution layer: the only broker writes
    intraday/        # TRADING_MODE=intraday (docs/17)
    overnight_hold.py, overnight_live.py  # TRADING_MODE=overnight (docs/19-overnight-hold.md)
    journal.py       # SQLite trade log
    memory.py        # export bot.db -> memory/state.json
    notify.py        # the bot's message wording (plan/sale tables, reasons); sends through notifier/
    report.py        # weekly metrics, charts, HTML dashboard
    pack.py          # size-capped review packs for AI runs (token efficiency)
    usage.py         # Claude usage snapshot watcher: low-usage alerts + AI-job gate
    altdata/         # EDGAR Form 4 / 13D / 13F, Alpaca news, optional Quiver
  dashboard/app.py   # optional local Streamlit dashboard
  prompts/           # portable agent prompts: daily_review.md, weekly_review.md
  CLAUDE.md          # imports AGENTS.md + compact instructions (for Claude sessions)
  reports/           # generated weekly reports (git-ignored)
  backtest/
  memory/            # agent memory (see docs/05-memory.md)
  scripts/run_job.sh
  scripts/claude_statusline.sh   # Claude Code status line; writes the usage snapshot
  tests/
  docs/
```

## Roadmap

1. Scaffold project, config, calendar and indicators (test-first)
2. Strategy and risk modules with tests
3. Backtester; 10+ years of daily data; walk-forward / out-of-sample validation
4. Alpaca paper broker + engine jobs; dry-run mode
5. SQLite journal, memory export, Slack notifications, Claude usage watcher (`usage.py`)
6. Scheduling (every 5 minutes), heartbeat checks
7. Weekly dashboard report (charts, HTML, Slack post)
8. Alternative data (EDGAR Form 4 / 13D, news veto), backtested against the probability gate
9. Agent wiring: review packs (`pack.py`, size-capped and tested), prompts, SessionStart hook, skills, custom subagents
10. ≥ 2–3 months of paper trading before any live capital

## Configuration

Every setting (trading mode, strategy, dry run, universe, risk limits, data feeds, secrets) is documented once, in **[docs/settings.md](docs/settings.md)**, with its default, allowed values and common changes. Copy `.env.example` to `.env` and change only what you need.

## Setup

```bash
cp .env.example .env   # fill in Alpaca paper keys (+ Slack, SEC user agent as needed)
uv sync
uv run pytest
```

1. **Backtest** (downloads ~11 years of daily bars into `data/bars/`, writes `memory/backtests/<id>.md` and the live gate's evidence `memory/evidence/signatures.json`). In swing and intraday mode the bot refuses all entries until a backtest is ACCEPTED; overnight mode has no backtest gate:
   ```bash
   uv run python -m bot.backtest --start 2016-01-01 --end 2026-09-30 --stability
   ```
   Or run it on GitHub: Actions → **Backtest** → Run workflow (keys from the `ALPACA_API_KEY` environment).
   After that the bot keeps its evidence fresh by itself: every Saturday at 11:00 ET the `refresh_evidence` job reruns this backtest once the evidence is 70 days old, before the gate's 90-day limit (`AUTO_BACKTEST=false` turns it off; see [docs/settings.md](docs/settings.md)).
   To compare strategies first: `uv run python -m bot.compare --start 2016-01-01 --end 2026-09-30` (see [docs/12](docs/12-strategies.md)).
2. **Dry run** (`DRY_RUN=true`, the default): jobs read the paper account and log intended orders without sending them:
   ```bash
   scripts/run_job.sh premarket
   scripts/run_job.sh status
   ```
3. **Schedule** (runs the scheduler every 5 minutes; logs in `logs/`): set `BOT_HOST=windows` in `.env` and follow **Running on Windows** below. `uv run python -m bot.host uninstall` removes the task.
4. **Paper orders**: set `DRY_RUN=false` in `.env` only after the dry run looks right. `PAPER` must stay `true`.

### Running on Windows

Use **one** machine at a time: each keeps its own `data/bot.db`, and with `BOT_HOST` set any other machine refuses to run.

1. On Windows, install [uv](https://docs.astral.sh/uv/) and git, run `setx PYTHONUTF8 1` once (Python then reads and writes the bot's files as UTF-8; open a new terminal afterwards), clone the repo, then in the repo folder: `uv sync` and `uv run pytest`.
2. Copy `.env.example` to `.env`, fill in your keys and set `BOT_HOST=windows`.
3. Dry run: `scripts\run_job.cmd status`, then read `logs\status.log`.
4. From PowerShell opened with **Run as administrator** (the task then runs hidden, even when the screen is locked):
   ```powershell
   uv run python -m bot.host install
   ```
   Without admin rights, use `powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Interactive` instead: it only runs while you are signed in and flashes a window each run.
5. Keep the laptop plugged in and set it to never sleep while plugged in (Settings > System > Power), or ticks are missed while it sleeps; the engine skips missed windows rather than trading late.

Windows has no desktop-notification fallback, so set `SLACK_WEBHOOK_URL` to get alerts.

### Running on GitHub Actions instead of a local scheduler

`.github/workflows/engine.yml` can run the same engine on GitHub's machines, but only when started by hand from the Actions tab (its schedule was removed on 2026-10-06 because runs started 1-3 hours late). Use **either** Actions **or** a local scheduler, never both (`uv run python -m bot.host uninstall`).

- State (`bot.db` as SQL, `memory/state.json`, `memory/packs/`) is kept on the `bot-state` branch and restored each run.
- Secrets in the GitHub environment `ALPACA_API_KEY`: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `SLACK_WEBHOOK_URL`, `SLACK_BOT_TOKEN`, `CLAUDE_ROUTINE_DAILY_URL`, `CLAUDE_ROUTINE_DAILY_TOKEN`, `CLAUDE_ROUTINE_WEEKLY_URL`, `CLAUDE_ROUTINE_WEEKLY_TOKEN`.
- Variables (optional): `DRY_RUN` (default `true`), `RISK_PER_TRADE`, `MAX_POSITION_PCT`, `MAX_OPEN_POSITIONS`, `EARNINGS_POLICY`, `SLACK_CHANNEL` (meanings in [docs/settings.md](docs/settings.md)).
- Manual run: Actions → **Engine** → Run workflow → command (`status`, `tick`, a job, or `resume`).
- Scheduled runs can start 5–15 minutes late; the job windows allow for it. There's no Claude usage snapshot there (it only exists on a Mac running Claude Code), so the usage watcher stays silent and AI reviews simply run; a rejected review is reported as skipped.

Manual commands: `scripts/run_job.sh <premarket|entries|manage|eod|weekly_report|status|resume>` (`scripts\run_job.cmd` on Windows; `resume` clears a drawdown pause).
