# Automation: Scheduling & Subagents

## Principle

The order path is **deterministic Python**. LLM agents are non-deterministic, cost money per call, and can fail in surprising ways — they are used for development, backtesting, review and research only. They never submit orders.

## Scheduler: launchd (macOS) vs cron

| | cron | launchd |
|---|---|---|
| Available on macOS | Yes (`crontab -e`) | Yes (native) |
| Runs missed jobs after sleep | No | Yes (`StartCalendarInterval` fires on wake) |
| Timezone | Machine local time | Machine local time |
| Logs | Mail / redirect | `StandardOutPath` / `StandardErrorPath` |

**This copy runs on Windows.** The Mac launchd files are not included. The same `scripts/run_job.sh <job>` wrapper works with cron on a Mac or Linux machine. On Windows, `scripts/install_windows_task.ps1` registers a Task Scheduler task that runs `scripts/run_job.cmd tick` every 5 minutes. `BOT_HOST=windows` and `python -m bot.host install` install it (README → Running on Windows).

Because schedulers use local time, every job converts to **America/New_York** and checks the Alpaca clock itself. A job firing at the wrong time (DST change, catch-up after wake) simply no-ops or runs its idempotent logic.

Recommended: keep the machine awake and plugged in during market hours. Broker-side stops protect positions even if it is off.

## Job schedule (ET, trading days only)

| Time | Job | Does |
|---|---|---|
| 08:30 | `premarket` | Check calendar, refresh bars and [alternative data](10-alternative-data.md) (Form 4, 13D, news), compute signals, build order plan, reconcile |
| 09:45 | `entries` | Check kill switches, submit bracket orders for planned entries |
| every 30 min 10:00–15:30 | `manage` | Reconcile, update trailing stops, partial exits, unprotected-position check |
| 16:15 | `eod` | Snapshot equity, close-out bookkeeping, export `memory/state.json`, Slack summary, heartbeat check |
| 17:52 | `usage_watch` | Read the Claude usage snapshot, send low-usage alerts, and decide whether the AI review may run (no Claude call) |
| 18:07 | `review` (optional) | Only if the usage gate passes: AI trade-reviewer (Devin, or a Claude routine fired via its API trigger) writes the daily journal and posts a summary |

Weekly (Saturday, ET). See [09-weekly-report.md](09-weekly-report.md):

| Time | Job | Does |
|---|---|---|
| 09:00 | `weekly_report` | Plain Python: metrics, charts and HTML dashboard; posts charts and key stats to Slack |
| 09:52 | `usage_watch` | Usage alerts + gate for the weekly review |
| 10:07 | `weekly_review` (optional, gated) | AI review and up to 3 improvement proposals in `memory/weekly/`, posted as a Slack thread reply |
| 11:00 | `refresh_evidence` (`AUTO_BACKTEST`, on by default) | Swing mode: rerun the backtest when its evidence is 70+ days old; alert with the new verdict |

Every job: acquire a file lock → check clock → reconcile DB with broker → act → record in `runs` table → alert on failure.

## Idempotency & safety

- Deterministic `client_order_id` (e.g. `{date}-{symbol}-{intent}`) so reruns never duplicate orders.
- `DRY_RUN=true` mode logs intended orders without sending.
- `PAPER=true` enforced unless explicitly overridden.

## Where Devin subagents fit

- Built-in profiles: `subagent_explore` (read-only research, cheaper default model) and `subagent_general` (full tools, same model as parent).
- Subagents do **not** inherit conversation history → give them file paths and use `memory/` as shared context.
- Background subagents auto-deny unapproved tools; good for parallel backtest sweeps once permissions are granted.

Planned uses:

| Use | How |
|---|---|
| Parallel backtests | One subagent per parameter set / universe, each writes `memory/backtests/<id>.md` |
| Code review | Read-only reviewer subagent before merging strategy changes |
| Nightly review | `devin -p --prompt-file prompts/review.md --respect-workspace-trust false` from launchd, restricted permissions |
| Strategy research | Explore subagent with web search, writes "proposed" entries to `memory/decisions.md` |

Unattended `devin -p` runs should use a restrictive permission config (allow writes only under `memory/`, deny broker scripts) rather than bypass mode.
