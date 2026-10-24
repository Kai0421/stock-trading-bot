# Token Efficiency ("Token Compression") for Claude & Devin

Researched 2026-10-04. Sources: [Manage costs effectively](https://code.claude.com/docs/en/costs), [Prompt caching](https://code.claude.com/docs/en/prompt-caching), [Memory / CLAUDE.md](https://code.claude.com/docs/en/memory), [API compaction](https://platform.claude.com/docs/en/build-with-claude/compaction-threshold).

**Goal:** every AI run (routines, Devin, interactive sessions) uses as few tokens as possible, so the Claude Pro limits ([08](08-claude-pro-usage-limits.md)) are never the bottleneck.

## What "token compression" means in practice

| Technique | What it is | Who provides it | Applies to us? |
|---|---|---|---|
| **Prompt caching** | Re-reads of an unchanged prefix are billed at the cheaper cached rate. The cache lasts 1 hour on a subscription and 5 minutes once you're on usage credits | Claude Code, automatic | Yes, automatic. Avoid breaking it (below) |
| **Auto-compaction / `/compact`** | Summarises old conversation when the context fills up | Claude Code | Interactive sessions only. Routine runs are short and fresh |
| **API compaction** (`compact_20260112`, trigger ≥ 50k tokens) | Server-side summarisation for direct Messages API calls | Claude API | **No.** We don't call the API directly. Revisit only if we move to the Agent SDK or API |
| **Preprocessing** (hooks or scripts filter data before Claude sees it) | E.g. grep a log for `ERROR` instead of reading 10k lines: "reducing context from tens of thousands of tokens to hundreds" (official docs) | Us | **Yes, the biggest win.** Python builds compact review packs |
| **Skills instead of long rules** | Skills load on demand; CLAUDE.md/AGENTS.md load every session. Keep CLAUDE.md "under 200 lines" | Claude Code / Devin | Yes |
| **Model and effort choice** | Sonnet < Opus; Haiku for simple subagents; lower effort cuts thinking tokens (billed as output) | Claude Code | Yes |
| **Fewer MCP servers** | Every server adds tool names to context; CLI tools are "more context-efficient" | Claude Code | Yes. No connectors in routines; post to Slack via our Python script |
| **Subagents for verbose work** | Large output stays in the subagent; only a summary returns. But each subagent has its own requests and a 5-minute cache | Claude Code / Devin | Dev sessions only; **not** in routines |
| **Third-party compressor plugins** (e.g. hooks that rewrite or shrink tool output, claiming 40–90%) | Intercept every tool result and drop or rewrite parts | Community / npm | **Not used** (see below) |

### Why we don't install third-party compression plugins

- They silently drop or rewrite tool output. In a trading system, a hidden error line is worse than a few extra tokens.
- They are a supply-chain risk: global npm packages with hooks that see **all** tool output, including anything secret-adjacent.
- Their savings figures are self-reported and depend heavily on content.
- Our design removes most token waste before Claude starts, so there is little left for them to compress.

These can be reconsidered later only after a code review, pinned to a version at least 7 days old, and only for interactive dev sessions, never routines.

## Applied design for this bot

### 1. Review packs: Python compresses, Claude reads one file

Deterministic Python (no LLM) writes a size-capped pack before each AI run:

| Pack | Written by | Budget | Contents |
|---|---|---|---|
| `memory/packs/daily-YYYY-MM-DD.md` | `eod` job | **≤ 6,000 characters** (about 1.5k tokens) | Account line, today's fills and exits, open positions, rejected-signal counts by reason, gate-health table, flags, anomalies |
| `memory/packs/weekly-YYYY-Www.md` | `weekly_report` job | **≤ 16,000 characters** (about 4k tokens) | Metrics table (week / 4 weeks / all time), closed trades, per-signature win rate vs 60%, alt-data hits, rule-audit counts, last week's proposals and their status |

Compression rules applied by `src/bot/pack.py`:
- **Tables, not JSON.** Markdown or CSV rows avoid repeated keys, quotes and braces.
- **Round numbers**: prices to 2 decimals, percentages to 1, R to 2. Drop unchanged or zero fields and nulls.
- **Aggregate, then top-N.** Show totals plus the top 5 winners and losers; never raw logs or bar data.
- **Short, stable codes**: setup signature IDs (`TP-RSI2-LO-BULL`), exit reason codes (`STOP`, `TRAIL`, `TIME`, `TREND`, `TGT`) with a one-line legend.
- **Hard budget**: if a pack exceeds its limit, the lowest-priority sections are cut and replaced with `[truncated: N rows, see reports/...]`. A unit test enforces the limit.
- **No secrets, no account numbers.**

Token estimates use the rough rule of about 4 characters per token for English text. Measure real usage in **Settings > Usage** (or `/usage` in Claude Code). Use the token-counting API if exact numbers are ever needed.

### 2. Tight prompts with explicit read and write scope

The prompts are [`prompts/daily_review.md`](../prompts/daily_review.md) and [`prompts/weekly_review.md`](../prompts/weekly_review.md):
- Read **only** the named pack, plus at most `memory/lessons.md` and the last weekly review.
- **Do not** explore the repo, read source code, run tests, install packages, or browse the web.
- Fixed output template with a word limit (daily ≤ 250 words, weekly ≤ 600 words). Output tokens cost more than input tokens.
- Finish by writing one file and running one command to post to Slack. No back-and-forth.

### 3. Small, layered instruction files

| File | Loaded | Rule |
|---|---|---|
| `CLAUDE.md` | Every Claude session | Imports `@AGENTS.md` and adds compact instructions. It exists because some Claude Code sessions read CLAUDE.md only |
| `AGENTS.md` | Every Devin/Claude session | Hard rules + essentials only; **≤ 60 lines** |
| Skills (`recall`, `remember`, `backtest`) | On demand | Long workflows live here, not in AGENTS.md |
| `docs/` | Only when explicitly read | Never auto-loaded; prompts must not ask agents to read docs |

### 4. Model, effort and cache hygiene

| Setting | Routines | Devin / interactive |
|---|---|---|
| Model | **Sonnet**; Haiku acceptable for the daily review | Sonnet by default; Opus only for hard design problems |
| Effort / thinking | Lowest that gives a good review (set in the routine's model selector where offered) | `/effort` low for mechanical work |
| Fable | **Never** (pay-as-you-go on Pro) | Never |
| Subagents | None | Explore subagents (cheaper model) for research; Haiku for simple subagent tasks |
| Connectors / MCP | **None.** Slack posting uses `python -m bot.notify` with the webhook | Only what's needed; prefer the `gh` CLI over a GitHub MCP |

Cache rules for interactive sessions: pick the model and effort at the **start**. Switching models or effort mid-session, toggling MCP servers or plugins, and `/compact` all invalidate the cache. Use `/clear` between unrelated tasks (free), and `/compact <focus>` only at natural breaks. After more than an hour idle the cache is cold, so prefer a fresh session over resuming a huge one.

### 5. Memory stays small over time

- `memory/lessons.md`: at most 50 bullets. Merge duplicates and drop stale ones during the weekly review.
- `memory/journal/`: daily files are compacted into `memory/weekly/` each week. Agents read weeklies, not dailies.
- Reviewers read at most the **last 4** weekly reviews.
- `memory/state.json`: only current values, no history (history lives in `bot.db`).
- `memory/packs/`: kept 30 days, then deleted by the `eod` job (they can be rebuilt from `bot.db`).

### 6. Measure and adjust

- First month: check **Settings > Usage** after each weekly review, and record the approximate usage per run in `memory/decisions.md`.
- In Claude Code, `/usage` shows attribution by skill, subagent and MCP server, plus a "Loops" row for scheduled tasks. `/context` shows what fills the context window.
- If a run uses more than expected: shrink the pack budget, lower the effort level, or switch the daily review to Haiku.

## Interactive development checklist (Claude Code or Devin)

- [ ] Start a session with a **specific** task ("add Wilson bound to `risk.py` with tests"), not "improve the bot".
- [ ] Use plan mode for multi-file changes; stop early (Esc) if Claude heads the wrong way.
- [ ] Point at file paths and line ranges instead of pasting large content.
- [ ] Run tests with quiet output (`pytest -q`) and show failures only.
- [ ] `/clear` between unrelated tasks; `/compact <focus>` at breaks.
- [ ] Don't switch model or effort mid-task.
