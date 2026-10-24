# Author: Kai0421
"""Build the dashboard page from a snapshot. Run from the repo root:
    uv run python scripts/dashboard/build_page.py [snapshot.json] [out.html]
Defaults: data/dashboard/snapshot.json -> data/dashboard/dashboard.html (both gitignored).
The page embeds the snapshot and, opened in the Claude desktop app with the read-only
`tradingbot` MCP server (mcp_server.py), refreshes it live.
"""

import json
import sys
from pathlib import Path

TEMPLATE = Path(__file__).with_name("template.html")


def render(snapshot: dict, template: str) -> str:
    """Embed the snapshot as JSON inside the page's <script type="application/json"> block."""
    if "__DATA__" not in template:
        raise ValueError("template has no __DATA__ placeholder")
    data = json.dumps(snapshot, separators=(",", ":")).replace("</", "<\\/")
    return template.replace("__DATA__", data)


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "data/dashboard/snapshot.json")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "data/dashboard/dashboard.html")
    out.write_text(render(json.loads(src.read_text()), TEMPLATE.read_text()))
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
