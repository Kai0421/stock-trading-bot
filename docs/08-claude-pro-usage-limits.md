# Claude Pro Usage Limits & Routines

Verified 2026-10-04 against Anthropic's own pages only (third-party summaries disagreed on details). Anthropic states that limits and prices may change, so re-check the sources before relying on this.

## Sources

| # | Page |
|---|---|
| S1 | [What is the Pro plan?](https://support.claude.com/en/articles/8325606-what-is-the-pro-plan) |
| S2 | [Use Claude Code with your Pro or Max plan](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan) |
| S3 | [Manage usage credits for paid Claude plans](https://support.claude.com/en/articles/12429409-manage-usage-credits-for-paid-claude-plans) |
| S4 | [What is a limit reset?](https://support.claude.com/en/articles/17007452-what-is-a-limit-reset) |
| S5 | [Claude Fable models on your plan](https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan) |
| S6 | [Routines — Usage and limits](https://code.claude.com/docs/en/routines#usage-and-limits) |
| S7 | [Models, usage, and limits in Claude Code](https://support.claude.com/en/articles/14552983-models-usage-and-limits-in-claude-code) |

## Verified facts (Pro plan)

| Topic | What Anthropic says | Source |
|---|---|---|
| Exact allowance | **No fixed number is published.** Usage "will vary based on message length, … the length of your current conversation, and the model or feature you use." | S1 |
| Session limit | "Your session-based usage limit will reset every five hours." | S1 |
| Weekly limit | "Pro plans also have a weekly usage limit that applies across all models. Weekly limits reset at a fixed time each week that is assigned to your account." The reset time is shown in **Settings > Usage**. | S1 |
| Other caps | Anthropic "may limit your usage in other ways, such as weekly and monthly caps or model and feature usage, at our discretion." | S1 |
| Shared pool | "Usage limits … are shared across Claude and Claude Code": chat, Claude Code CLI, IDE and Desktop all draw from one pool. | S2 |
| Routines | "Routines draw down subscription usage the same way interactive sessions do." | S6 |
| When the limit is hit (no credits) | "Without usage credits, additional runs are rejected until your usage window resets." | S6 |
| Usage credits | Optional pay-as-you-go at standard API rates. Requires a prepaid balance. Monthly spend cap and auto-reload are configurable. Can be disabled at any time. | S3 |
| Opus | "Uses meaningfully more of your quota." Sonnet is recommended for most work, Haiku for high-volume scripted runs. | S7 |
| **Fable models on Pro** | "Fable 5 and Fable 5.1 aren't included in your plan's usage limits": on Pro they **always bill usage credits**. | S5 |
| Limit resets | One-off offers that refill the session or weekly limit. Redeemed via web or Desktop only; they expire. | S4 |
| API key override | If `ANTHROPIC_API_KEY` is set, Claude Code bills the API instead of the subscription. | S2 |
| Monitoring | **Settings > Usage** shows session and weekly progress bars. In Claude Code, run `/status`. | S1, S2 |

### Routine run caps (separate from subscription usage, no overage) — S6

| Action | Limit |
|---|---|
| Scheduled runs (incl. one-off) | 100 per hour per account (waits until reset) |
| Run now + API fires + re-arming a one-off | 30 per hour per routine (fails until reset) |
| Run now | 100 per hour per account |
| API fires | 100 per hour per account |
| Minimum schedule interval | 1 hour |

Also from S6: routines go on hold while the subscription is paused, and turn off after 72 hours without a GitHub connection.

### Not published (do not assume)

- Message or token counts per 5-hour session or per week on Pro.
- Whether the 5-hour window starts at your first message. S3 only says it resets "every five hours once you reach it".
- How much one routine run costs in usage. This depends on the model, prompt size and the number of files read.

## Why this cannot affect trading

1. **No Claude in the order path.** `premarket`, `entries`, `manage` and `eod` are plain Python run by launchd. They call Alpaca and Slack, never Claude. Running out of Claude usage has zero effect on orders, stops or alerts.
2. **Stops live at the broker.** Even if every scheduled job failed, bracket and stop orders at Alpaca keep protecting positions.
3. **AI jobs are optional extras.** The nightly review, weekly compaction and research write to `memory/` only. If a run is rejected for usage, the bot carries on unchanged.

## Rules for using routines on Pro

| Rule | Why |
|---|---|
| Pin **Sonnet** (or Haiku) in the routine's model selector; never Fable; avoid Opus | Fable bills credits from the first token on Pro; Opus drains quota fastest |
| Leave **usage credits disabled**, or set a small monthly spend cap with auto-reload off | Prevents surprise charges; a rejected run is acceptable |
| Schedule at most: weekday review 18:07 ET (5 runs/week) and one weekly compaction (Sat 10:07 ET) | Off-market hours, avoids :00 delay, small weekly footprint |
| Keep prompts tight: read only the size-capped review pack (`memory/packs/`), per [11-token-efficiency.md](11-token-efficiency.md) | Context size drives usage (S7) |
| No connectors in routines; post to Slack via `python -m bot.notify` | Connector tool listings add context to every run |
| Do heavy interactive Claude Code work away from the review time, or accept the review may be skipped | Same shared pool (S2) |
| No `ANTHROPIC_API_KEY` in your shell when using a subscription login | Avoids accidental API billing (S2) |
| Check **Settings > Usage** weekly during the first month and note the weekly reset time here | Real consumption per run is unpublished; measure it |

## Low-usage alerts

### Where usage numbers can come from

| Source | Official? | What it gives | Limitation | Used? |
|---|---|---|---|---|
| Claude Code **statusLine JSON**: `rate_limits.five_hour` / `rate_limits.seven_day` (`used_percentage` 0–100, `resets_at` epoch) ([docs](https://code.claude.com/docs/en/statusline)) | **Yes** | Account plan usage %, for Pro/Max only | Only present after the first response in a Claude Code session **on this Mac**; only refreshes while a session is active | **Yes** (primary) |
| Headless `claude -p --output-format stream-json` → `rate_limit_event` | Yes | `status` + `resetsAt` per window, with no percentage | Only during runs we start locally | Yes (secondary, for local runs) |
| Routine outcome: expected journal/review file missing | n/a | "Run was likely rejected" | After the fact | Yes (already in the heartbeat) |
| `GET api.anthropic.com/api/oauth/usage` with the stored OAuth token | **No (undocumented)** | Exact percentages | Unsupported and may break; a script would have to read your login token | **No** |
| Settings > Usage (web / Desktop), `/usage` in Claude Code | Yes | Full view | Manual | Weekly manual check |

### How it works

1. **Snapshot**: [`scripts/claude_statusline.sh`](../scripts/claude_statusline.sh) is set as your Claude Code status line. Each time it renders, it shows `ctx % | 5h % | 7d %` and writes `~/.local/state/stocktradingbot/claude_usage.json` (atomic write; only the two usage windows and a timestamp, no tokens or secrets). Install it in `~/.claude/settings.json`:
   ```json
   { "statusLine": { "type": "command", "command": "~/Development/StockTradingBot/scripts/claude_statusline.sh", "refreshInterval": 300 } }
   ```
2. **Watcher**: `src/bot/usage.py` (`usage_watch` job) reads the snapshot. It runs in `eod` (16:15), 15 minutes before each AI job, and in the Saturday report. It never calls Claude, so it costs **zero tokens**.
3. **Alerts**, deduplicated: each threshold fires once per window, keyed by `resets_at`.

| Condition | Level | Channels |
|---|---|---|
| 7-day ≥ 50% | INFO (once per week) | Slack |
| 7-day ≥ 75% | **WARN**: "Claude weekly usage 75%, resets <day time>" | Slack |
| 7-day ≥ 90% | **WARN**: AI reviews paused until reset | Slack |
| 5-hour ≥ 80% and an AI job is due before the 5-hour reset | WARN: "review may be skipped" | Slack |
| Snapshot older than 48 h | INFO in EOD/weekly report: "Claude usage unknown, open Claude Code or check Settings > Usage" | Slack |
| No snapshot at all (e.g. engine on GitHub Actions) | Silent; reviews run, and a rejected run is reported as "AI review skipped" | — |
| AI run rejected (missing output, or `rate_limit_event` status not `allowed`) | INFO: "AI review skipped (usage limit)" | Slack |

These are never CRITICAL, because Claude usage cannot affect trading.

4. **Pre-flight gate**: before starting any AI job, the watcher checks the snapshot. If the 7-day window is ≥ 90% or the 5-hour window is ≥ 85%, the job is **skipped** and an INFO alert says so, saving the remaining quota for your own work.
   - To apply the gate to **cloud routines** too, give routines an **API trigger** and no schedule. The local launchd job then fires them (`POST …/fire`) only after the gate passes. A routine on its own cloud schedule cannot be gated.
   - The routine token is stored in `.env` as `CLAUDE_ROUTINE_DAILY_URL` / `CLAUDE_ROUTINE_DAILY_TOKEN` (and `_WEEKLY_`). Never commit them.
5. **Devin**: `/usage` and `/session-stats` show quota interactively, but there is no documented machine-readable export. The watcher only detects Devin quota errors from a failed `devin -p` run (non-zero exit or "Quota exhausted" text) and sends an INFO alert. Check `/usage` manually.

All numbers are a snapshot. If you mostly use claude.ai on the web, the snapshot only updates the next time you use Claude Code on this Mac. The staleness alert covers this.

## Detecting a skipped AI run

- The `eod` heartbeat check also looks for `memory/journal/<today>.md` by 20:00 ET (or for the routine's PR or branch).
- If it's missing, send a Slack **INFO** "AI review skipped (likely usage limit)". It is never WARN or CRITICAL, because trading is unaffected.
- A green run status does not mean success (S6). The heartbeat checks for the output file, not the status.

## Fallback order if Pro limits become a problem

1. Trim prompts and switch to Haiku.
2. Run the review locally with Devin (`devin -p`), which is billed separately from Claude.
3. Enable usage credits with a low monthly cap.
4. Upgrade to Max 5x.
