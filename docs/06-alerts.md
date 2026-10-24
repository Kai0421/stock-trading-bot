# Alerts & Notifications

Verified 2026-10-04 against official docs (Slack, Telegram, Discord, ntfy, Claude Code).

## The notifier package (2026-10-19)

Sending lives in `src/notifier/`, separate from the trading bot:

- **No dependency on trading code.** `notifier` never imports `bot` (tested in
  `tests/test_notifier_package.py`). The bot depends on it; the notifier does not know the bot exists.
- **Its own settings.** `NotifierConfig` reads `SLACK_BOT_TOKEN`, `SLACK_WEBHOOK_URL`,
  `SLACK_CHANNEL`, `COUNCIL_SLACK_CHANNEL`, `GENERAL_SLACK_CHANNEL` and `NOTIFIER_DB` from `.env`
  or the environment.
- **Its own state.** De-duplication (same message within 30 minutes) and stored thread ids live in
  `data/notifier.db`, never in the trading journal. Inside the bot, `bot.notify.build_dispatcher`
  still uses the journal for de-duplication so the live behaviour does not change.
- **Categories.** `bot`, `council`, `general` each have a channel; an unset one falls back to
  `SLACK_CHANNEL`. The bot token is tried first; the webhook (which always posts to its own fixed
  channel) is only a backup.
- **Self-check.** `notifier/checks.py` reads each message. An INFO report that looks broken is held
  back and a notice goes to `#general`; WARN and CRITICAL are always sent, with the problem written
  on them. Plan-specific checks (slots, sector limits, shares held) live in `bot/notice_check.py`.
- **Layout.** `notifier/layout.py`: bullets one per line, aligned tables in code blocks, bold section
  headings. Wording for the bot and the council stays in their own modules.
- **CLI.** `python -m notifier --category general --level INFO --title "T" "text"`
  (`python -m bot.notify` still works and uses the trading journal).

- **Optional queue.** With `NOTIFY_VIA_OUTBOX=true` the engine's notifier is an `OutboxDispatcher`:
  `notify` writes the message to `data/notifier_outbox.db` and returns at once; the scheduled task
  `notifier-drain` (`scripts/run_notifier_drain.cmd`, every minute, reads the live bot's queue
  through `NOTIFIER_OUTBOX`) sends it, retrying up to 5 times. Messages with attachments, and any
  message when the queue file cannot be written, are sent directly. A CRITICAL message still queued
  after 3 minutes is sent directly at the start of the bot's next job (`rescue`), so a stopped drain
  task cannot silence a safety alert. Turn it on only together with the drain task.

Changing the notifier means editing `src/notifier/` only; the saved engine traces in
`tests/golden/` prove the bot's own messages did not change.

## Is Slack free?

Yes. The **Slack Free plan ($0)** is enough for this bot. Its limits ([slack.com/pricing](https://slack.com/pricing), [usage limits](https://slack.com/help/articles/115002422943-Usage-limits-for-free-workspaces)):

| Limit | Impact on the bot |
|---|---|
| Up to **10 apps/integrations** | We use 1 (our own app) |
| **90 days** of visible message and file history; data older than 1 year is deleted | Fine, because the permanent record is `data/bot.db`, `memory/` and `reports/`, not Slack |
| "High-volume message limit" for spam prevention | We send roughly 10–20 messages a day, far below any concern |

**Important:** incoming webhooks can only post **text and Block Kit**. They **cannot upload files** such as chart images. To post the weekly charts to Slack, the same Slack app also needs a **bot token** with the `files:write` and `chat:write` scopes, used through `files_upload_v2` in `slack_sdk`. This is still free.

## Other alert channels (all free for our volume)

| Channel | Cost | Phone push | Images / files | Setup | Notes |
|---|---|---|---|---|---|
| **Slack** (webhook + bot token) | Free plan | Yes (Slack app) | Yes, via bot token | Medium | Primary channel; good formatting and threads |
| **Telegram bot** | Free | Yes | Yes (`sendPhoto`, `sendDocument`) | Easy (@BotFather) | Limit about 1 message per second per chat; best free alternative |
| **Discord webhook** | Free | Yes | Yes (multipart file upload) | Very easy | About 30 requests per minute per webhook; 2,000 characters per message |
| **ntfy.sh** | Free tier (rate-limited), paid from about $5/month, or self-host | Yes (ntfy app) | Small attachments | Very easy (`curl -d`) | Ideal **backup** channel for CRITICAL alerts |
| **Email (SMTP, e.g. Gmail app password)** | Free | Via mail app | Yes | Easy | Good for the weekly report as a fallback |
| **macOS notification** (`osascript`) | Free | No (Mac only) | No | None | Last-resort local fallback |

**Recommendation:**
- **Slack** is the primary channel for everything.
- **No second channel** (decision 2026-10-04): ntfy was considered as a CRITICAL-only backup and dropped. If Slack is down, alerts fall back to a macOS notification and the log; broker-side stops protect positions regardless.
- **Telegram** is a drop-in alternative if you ever leave Slack.

`notify.py` will use a pluggable `Notifier` interface, so channels are enabled in `.env` without code changes.

## Does the Claude app provide alerts?

**Not in a way that's usable for the bot.** From [code.claude.com/docs/en/mobile](https://code.claude.com/docs/en/mobile) and the Claude Code issue tracker:

- **Claude Code mobile push** only works while a **Remote Control** session is active. Claude decides when to push, typically when a task finishes or needs approval. Calls from scheduled tasks or non-Remote-Control sessions return "Remote Control inactive" ([issue #49756](https://github.com/anthropics/claude-code/issues/49756)).
- **Claude Desktop scheduled tasks** show a desktop notification when a run starts. That only works while the app is open and the Mac is awake.
- **Cloud routines** have no documented completion-notification mechanism.
- Every Claude-based alert would also use up Claude usage and depend on an LLM session. That breaks the rule that trading-critical paths are deterministic.

**Conclusion:** Claude is used for *reviews and research*, not as an alert channel. Trading alerts come from plain Python (`notify.py`) to Slack. If a Claude routine produces the weekly review, it posts to Slack through the Slack connector or webhook like any other client.

## Alert levels

| Level | Examples | Channels |
|---|---|---|
| INFO | Order filled, partial exit, EOD summary, AI review skipped, Claude weekly usage ≥ 50%, usage snapshot stale | Slack |
| WARN | Order rejected, stale data, job late or missing, API retry exhausted, setup disabled for falling below the floor, **Claude usage running low** (7-day ≥ 75%, or 5-hour ≥ 80% before an AI job) | Slack |
| CRITICAL | Kill switch tripped, auth failure, DB/broker mismatch, position without a stop, hard-rule violation blocked | Slack with `<!channel>` |

## Behaviour

- Identical alerts within 30 minutes are deduplicated.
- If sending fails (after one retry), the alert goes to a macOS notification and the log. A failed alert **never** crashes a trading job.
- Short timeout (5 s), one retry.

## EOD summary (16:15 ET)

- Equity, day P&L ($ / %), drawdown from peak.
- Open positions: symbol, shares, entry, stop, current R, days held.
- Today's entries and exits with reasons; signals rejected by the probability gate.
- Flags (kill switch / paused) and job heartbeat (did all scheduled jobs run?).

The weekly dashboard report is described in [09-weekly-report.md](09-weekly-report.md).

## Claude usage alerts

Low Claude usage is reported through the same channels, with no Claude call needed. The snapshot comes from the documented Claude Code status-line fields. AI jobs are skipped automatically when usage is ≥ 90% weekly or ≥ 85% in the 5-hour window. Full design and thresholds: [08-claude-pro-usage-limits.md → Low-usage alerts](08-claude-pro-usage-limits.md#low-usage-alerts).

## Phase 2 — Two-way Slack app (optional)

- Slack Bolt for Python + **Socket Mode** (no public URL needed; runs as a launchd service on the Mac).
- Commands: `/bot status`, `/bot pause`, `/bot resume`, `/bot close SYMBOL` (with a confirmation button).
- Restricted to your Slack user ID; every command is logged to `runs`.
- Needs the bot token (`SLACK_BOT_TOKEN`) and an app token (`SLACK_APP_TOKEN`) in `.env`.

## Setup

1. Create a Slack app at https://api.slack.com/apps (Free workspace is fine).
2. **Incoming Webhooks** → on → **Add New Webhook** → channel `#trading-bot` → `SLACK_WEBHOOK_URL`.
3. **OAuth & Permissions** → bot scopes `chat:write`, `files:write` → install → `SLACK_BOT_TOKEN`. Invite the bot to the channel.
4. Put every secret in `.env` only. Never commit them.
