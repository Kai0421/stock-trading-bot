# Agent memory

Layer 2 of the bot's memory (docs/05-memory.md): small markdown files in git that AI agents
read and write. The trading code's own state lives in `data/bot.db` (agents never touch it).

## Read first
1. `state.json`: current equity, flags and open positions (written by the EOD job).
2. `lessons.md`: durable learnings (max 50 bullets).
3. For reviews: only the pack named in your prompt (`packs/daily-*.md`, `packs/weekly-*.md`).

## Index
| Path | Contents | Written by |
|---|---|---|
| `state.json` | current values only | EOD job |
| `strategy.md` | current parameters and rationale | humans (reviewed commits) |
| `decisions.md` | ADR log: date, decision, evidence, backtest id, status | humans, researcher ("proposed") |
| `lessons.md` | lessons tagged #entry #exit #risk #infra #data | reviewers |
| `journal/YYYY-MM-DD.md` | daily review | trade-reviewer |
| `weekly/YYYY-Www.md` | weekly review, compacts the dailies | weekly reviewer |
| `backtests/<id>.md` | backtest reports | backtester (`python -m bot.backtest`) |
| `evidence/signatures.json` | per-signature evidence for the live gate | backtester |
| `packs/` | size-capped review packs, kept 30 days | EOD and weekly jobs |

## Write protocol
- Append-only, dated entries. Corrections are new entries; never rewrite history.
- Cite evidence for every lesson or decision (trade id, backtest id, journal date).
- No secrets, API keys or account numbers.
- `lessons.md`: deduplicate. Update an existing bullet's evidence instead of adding a near-copy.
- Never weaken the hard rules in AGENTS.md. Proposals are advisory until backtested and approved.
