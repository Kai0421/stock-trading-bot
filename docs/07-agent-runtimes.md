# Agent Runtimes: Local vs Remote (Claude Code Routines & Devin)

Reference for switching where the AI agent jobs (nightly review, backtests, research) run. Researched 2026-10-04; Claude Code Routines are a **research preview**, so limits and APIs may change.

Sources: [Routines](https://code.claude.com/docs/en/routines), [Desktop scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks), Devin CLI docs (`cloud.mdx`, `commands.mdx`).

## Claude Code scheduling options

| | Remote: Routine (cloud) | Local: Desktop scheduled task | `/loop` |
|---|---|---|---|
| Runs on | Anthropic cloud (or org self-hosted env) | Your Mac | Your Mac, inside an open CLI session |
| Machine must be on | No | Yes, app open and awake | Yes |
| Local files | No, fresh `git clone` each run | Yes | Yes |
| Triggers | Schedule, API (`POST .../fire`), GitHub PR/release events | Schedule, manual | Interval in session |
| Minimum interval | **1 hour** | 1 minute | 1 minute |
| Permission prompts | None (fully autonomous) | Per-task permission mode | Inherits session |
| MCP | claude.ai connectors (e.g. Slack) or committed `.mcp.json` | Local config + connectors | Inherits session |
| Missed runs | Runs in cloud, can start a few min late (avoid :00) | One catch-up run for most recent miss (within 7 days) | Lost |
| Create via | claude.ai/code/routines, Desktop → Routines → **Cloud**, CLI `/schedule` | Desktop → Routines → **Local** | `/loop` |
| Plans | Pro, Max, Team, Enterprise (claude.ai login, not API key) | Claude Desktop | Claude Code CLI |

### Remote routines: details that matter

- **What a routine is**: a saved prompt, one or more GitHub repos, a cloud environment, connectors, and triggers. Each run is a full autonomous Claude Code session.
- **Repos**: cloned from the default branch each run. Changes are pushed to `claude/*` branches unless the prompt says otherwise. Protect `main` with branch rules.
- **Environment**: network access level, environment variables, and a cached setup script.
  - The default **Trusted** network only allows package registries and common development domains.
  - Allow-list these for this bot: `paper-api.alpaca.markets`, `data.alpaca.markets`, `hooks.slack.com`.
- **Secrets**: environment variables are visible to anyone using the environment. On Pro/Max, use **API credentials** for keys.
- **Schedule**: times are entered in your local zone. Use `/schedule update` for custom cron expressions (minimum 1 hour). Pick :07 rather than :00 to avoid late starts.
- **API trigger**:
  - Each routine gets its own URL and a bearer token, shown only once.
  - The request body's `text` field arrives as untrusted data. The saved prompt must explicitly tell the routine to act on it.
  - Beta header: `experimental-cc-routine-2026-04-01`.
- **Limits**:
  - 100 scheduled runs per hour per account.
  - 30 Run-now/API fires per hour per routine.
  - Runs use up your subscription usage. See [08-claude-pro-usage-limits.md](08-claude-pro-usage-limits.md).
- **Identity**: commits, PRs and Slack messages appear as *you*.
- **Run status**: green only means the session didn't crash. Read the transcript to confirm the task actually worked.
- **GitHub connection**: if it expires, runs are skipped for up to 72 hours, then the routine turns off.
- **CLI commands**: `/schedule`, `/schedule list`, `/schedule update`, `/schedule run`.

### Local Desktop tasks: details that matter

- Prompt is stored at `~/.claude/scheduled-tasks/<name>/SKILL.md`.
- Each task has its own permission mode. Use **Run now** once and choose "always allow" for each tool so later runs don't stall waiting for approval.
- Optional isolated git worktree per run.
- **Catch-up danger**: a run scheduled for 09:00 can fire at 23:00 after the Mac wakes. Prompts and scripts must check the time and market state.

## Devin equivalents

| | Local | Remote |
|---|---|---|
| Command | `devin -p --prompt-file prompts/review.md` | `devin --cloud -p --prompt-file prompts/review.md` |
| Runs on | Your Mac | Devin Cloud VM (shell, browser, repo clones) |
| Scheduler | launchd / cron (Devin CLI has no built-in scheduler) | Triggered from launchd, cron, or GitHub Actions |
| Files | Local repo, `data/bot.db` | Repo clone only |
| Follow-up | `devin -c` / `-r <id>` | `devin --cloud -r <id or URL>`, `/pickup` to continue locally |
| Unattended flags | `--respect-workspace-trust false`, restrictive permission config | Same, plus Devin account login |

## Implications for this bot

1. **Order path stays deterministic in every mode.** A routine is an LLM session, so it is unsuitable as the scheduler for `premarket`, `entries`, `manage` and `eod`. Those run as plain Python from:
   - Local: launchd (current choice).
   - Remote: GitHub Actions cron or a small VPS with cron.
2. **Agent jobs can move freely.** Nightly review, weekly compaction, backtest sweeps and research are good fits for routines or `devin --cloud`.
3. **State moves with the mode.**
   - Remote runs get a fresh clone with no `data/bot.db`. They read Alpaca (source of truth) and committed `memory/` files.
   - For remote writes to `memory/`, either let the routine open a PR from a `claude/*` branch, or push to a dedicated `memory` branch, or move bot state to a hosted DB (e.g. Postgres or Turso) later.
4. **1-hour minimum** on routines means no 30-minute management loop there. That is fine for agent work. The trading engine's 30-minute `manage` job stays on launchd, GitHub Actions or a VPS.
5. **Slack**:
   - Local: webhook from `notify.py`.
   - Remote routine: either the Slack connector or the same webhook with `hooks.slack.com` allow-listed.
6. **Hybrid trigger**: the local `eod` job can POST to a routine's `/fire` endpoint with the day's summary as `text`, so the review runs in the cloud even if the Mac sleeps afterwards.

## Switching checklist

| Moving to | Steps |
|---|---|
| Local Desktop task | Install Claude Desktop → Routines → New → **Local** → folder = this repo, enable worktree, set permission mode, Run now to pre-approve tools |
| Remote routine | Push repo → claude.ai/code/routines → New → repo `<your-github-user>/StockTradingBot` → custom environment (allow-list Alpaca + Slack, add credentials, setup script `uv sync`) → trim connectors → schedule (weekday, :07) → Run now and read transcript |
| Devin cloud | `devin auth login` → launchd/Actions job runs `devin --cloud -p --prompt-file prompts/<job>.md` |

Prompts for every mode live in `prompts/` (to be created) so they are portable across Claude routines, Desktop tasks and Devin.
