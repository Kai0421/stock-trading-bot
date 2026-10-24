# Weekly Dashboard Report

Every weekend the bot produces a performance dashboard, a review of the week's trades, and suggestions for improving the strategy. The numbers and charts come from **deterministic Python**. The review text comes from an AI agent and is **advisory only**.

## Schedule

| When (ET) | Job | Runs on | Output |
|---|---|---|---|
| Sat 09:00 | `weekly_report` | launchd, plain Python | `reports/weekly/YYYY-Www/` (HTML, PNGs, `summary.json`), posted to Slack |
| Sat 10:07 | `weekly_review` | Claude routine (Sonnet) **or** `devin -p` | `memory/weekly/YYYY-Www.md`, posted as a Slack thread reply |

If the AI review is skipped (usage limits, outage), the report itself still goes out. See [08-claude-pro-usage-limits.md](08-claude-pro-usage-limits.md).

## Report contents

### Charts (matplotlib PNG for Slack + Plotly in a self-contained HTML file)
1. **Equity curve vs SPY** (week and since inception), rebased to 100.
2. **Drawdown** from peak, with the −10% kill-switch line.
3. **Daily P&L bars** for the week.
4. **R-multiple distribution** of closed trades (histogram).
5. **Win rate by setup signature** with the 95% Wilson interval and the **win-probability floor line**.
6. **Exposure**: % of cash invested per day and number of positions.

### Tables
- Closed trades: symbol, entry/exit date and price, shares, P&L $, R-multiple, holding days, exit reason, setup signature, probability estimate at entry.
- Open positions with current R and stop.
- Signals rejected and why (probability gate, regime filter, earnings, sector cap, no slots).
- Alternative-data hits (insider buys, news vetoes) and what happened to those trades. See [10-alternative-data.md](10-alternative-data.md).

### Key metrics (week / 4-week rolling / since inception)
- Return vs SPY, max drawdown, win rate (with lower bound), expectancy in R, average win and loss in R, profit factor, number of trades, average holding time.
- **Hard-rule audit**: count of blocked orders by rule. Every blocked order should have been stopped before it reached the broker. The count of violations that reached the broker must always be **0**.
- **Gate health**: each setup signature's live win rate vs the floor. Signatures disabled this week are listed.

## AI weekly review (`memory/weekly/YYYY-Www.md`)

The reviewer agent follows [`prompts/weekly_review.md`](../prompts/weekly_review.md). It reads **only** the size-capped pack `memory/packs/weekly-YYYY-Www.md` (≤ 16,000 characters, built by Python from `summary.json` and the trades table), `memory/lessons.md` and the last 4 weekly reviews ([token efficiency](11-token-efficiency.md)). It writes:

1. **What happened**: performance in plain English, and the best and worst trades with reasons.
2. **Rule and process check**: anything unexpected, such as late jobs, slippage worse than modelled, or stops gapped through.
3. **Patterns**: setups, sectors, regimes or exit types that under- or over-performed.
4. **Improvement proposals**: at most 3. Each has a hypothesis, the parameter or feature to change, and the backtest needed to prove it.
5. **Lessons** to append to `memory/lessons.md` (deduplicated).

Guardrails:
- Proposals are **never applied automatically**.
- Each proposal must be tested by the `backtester` subagent (walk-forward, out-of-sample).
- A proposal is only adopted if it still passes the **win-probability floor (60%)** and positive expectancy. You then approve it through a reviewed commit.
- Proposals may never weaken the hard rules (no options, CFDs or leverage, the win-probability floor, broker-side stops).
- Small samples get flagged: a week typically has few trades, so the reviewer must not draw conclusions from fewer than 30 trades in any bucket.

## Where to view it

| Option | Cost | Notes |
|---|---|---|
| **Slack** post with PNG charts and key metrics | Free | Default, so it's readable on your phone |
| `reports/weekly/.../report.html` opened locally | Free | Full interactive Plotly report |
| **Streamlit** local dashboard (`uv run streamlit run dashboard/app.py`) | Free | Optional live view over `data/bot.db` at http://localhost:8501 |
| GitHub Pages | — | **Not used.** On GitHub Free, Pages requires a public repo, and Pages sites are public anyway |
| Email with the HTML attached | Free | Optional fallback |

`reports/` is git-ignored by default because it contains account values. Commit only if you want history in the private repo.

## Implementation sketch

- `src/bot/report.py`: build metrics from `data/bot.db`, then render charts, `summary.json` and the HTML.
- `src/bot/notify.py`: `SlackNotifier.post_report(summary, images)` using `files_upload_v2`.
- `prompts/weekly_review.md`: a portable prompt that works for a Claude routine, a Desktop task, or Devin.
- Tests: metrics on fixed fixture trades (known win rate, Wilson bound, expectancy), and chart generation smoke tests.
