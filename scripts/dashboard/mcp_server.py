# Author: Kai0421
"""Read-only local MCP server for the Trading Bot Dashboard artifact.

One tool, `dashboard_snapshot`, returns the same data as snapshot.py: the Alpaca PAPER
account (GET requests only) plus the bot's records. It has no way to place, change or
cancel orders. Started by the Claude desktop app:
    uv run --directory <repo> --with "mcp<2" python scripts/dashboard/mcp_server.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import snapshot  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

server = FastMCP("tradingbot")


@server.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
)
def dashboard_snapshot() -> dict:
    """Read-only snapshot of the paper trading account and the bot's records for the dashboard."""
    return snapshot.build()


if __name__ == "__main__":
    server.run()
