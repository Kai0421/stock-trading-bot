# Bot dashboard and research scripts

Read-only tools. None of them can place, change or cancel orders.

## Trading Bot Dashboard (`scripts/dashboard/`)

A single HTML page showing the paper account and the bot's own records: account value and
drawdown, progress toward the go-live rule (100 trades, 62% lower bound, positive average R),
open positions with stops, closed trades, results by stock, the setups the bot considered (with
shadow outcomes) and broker orders.

| File | What it does |
|---|---|
| `snapshot.py` | GET requests to the Alpaca **paper** API plus `data/bot.db` and `memory/state.json`; writes `data/dashboard/snapshot.json` |
| `build_page.py` | Embeds a snapshot into `template.html`; writes `data/dashboard/dashboard.html` |
| `template.html` | The page. Opened in the Claude desktop app with the `tradingbot` MCP server it refreshes live every minute; anywhere else it shows the embedded snapshot |
| `mcp_server.py` | Local MCP server with one read-only tool, `dashboard_snapshot` (calls `snapshot.build()`) |

Refresh a snapshot page:

```bash
uv run python scripts/dashboard/snapshot.py
uv run python scripts/dashboard/build_page.py
```

Live mode in the Claude desktop app: add to `claude_desktop_config.json` under `mcpServers`
(`mcp` is not a project dependency; `mcp<2` because 2.x dropped `fastmcp`):

```json
"tradingbot": {
  "command": "/opt/homebrew/bin/uv",
  "args": ["run", "--directory", "/path/to/StockTradingBot", "--with", "mcp<2",
           "python", "scripts/dashboard/mcp_server.py"]
}
```

## Gap-up fade study (`scripts/research/gap_fade.py`)

One-off research from 2026-10-07: which S&P 500 / Nasdaq stocks gap up more than 2% overnight
and then fall after the open (5 years of daily bars). Result: the effect is weak (gap-ups close
below the open 52% of the time vs 49% normally) and stock rankings barely persist; Micron is not
a fader. Fading needs short selling, so it is not tradable under the bot's rules. The long-only
"buy at the close, sell at the open" variant lost to buy-and-hold after costs. Not used by the bot.
