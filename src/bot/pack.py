# Author: Kai0421
"""Size-capped review packs for AI runs (docs/11-token-efficiency.md).

Deterministic Python compresses the day or week into one markdown file so a reviewer
reads ~1.5k (daily) or ~4k (weekly) tokens instead of the database or logs:

- Tables, not JSON; numbers rounded; zero/empty sections dropped.
- Short stable codes: ``TP-UMSDN`` signatures and STOP/TRAIL/TIME/TREND/TGT exits.
- Hard budget: the lowest-priority sections lose rows first, replaced by a
  ``[truncated: N rows, see reports/]`` marker, then whole sections are dropped.
- No secrets or account numbers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from bot.journal import PositionRow

DAILY_BUDGET = 6_000
WEEKLY_BUDGET = 16_000
DEFAULT_PACK_DIR = Path("memory/packs")
LEGEND = (
    "Legend: exits STOP initial stop, TRAIL trailing/breakeven stop, TIME time stop, "
    "TREND close < SMA50, TGT partial at +2R. Signatures TP-<SPY trend><SPY vol>"
    "<trend strength><pullback depth><EMA distance>."
)
_EXIT_CODES = {
    "stop": "STOP",
    "trail": "TRAIL",
    "time_stop": "TIME",
    "trend_break": "TREND",
    "partial_profit": "TGT",
    "profit_target": "TGT",
    "end_of_data": "EOD",
}


def short_signature(signature: str) -> str:
    parts = signature.split("|") if signature else []
    if len(parts) != 5:
        return "TP-?"
    return "TP-" + "".join("?" if p == "unknown" else p[:1].upper() for p in parts)


def exit_code(reason: str) -> str:
    return _EXIT_CODES.get(reason, "OTHER")


@dataclass
class Section:
    title: str
    rows: list[str]
    priority: int  # lower is cut first
    header: list[str] = field(default_factory=list)
    truncated: int = 0
    dropped: bool = False

    def text(self) -> str:
        body = [*self.header, *self.rows]
        if self.truncated:
            body.append(f"[truncated: {self.truncated} rows, see reports/]")
        return f"## {self.title}\n" + "\n".join(body)


def render(title: str, sections: Sequence[Section], budget: int) -> str:
    secs = [s for s in sections if s.rows or s.header]

    def assemble() -> str:
        return "\n\n".join([title, *(s.text() for s in secs if not s.dropped)]) + "\n"

    text = assemble()
    order = sorted(secs, key=lambda s: s.priority)
    while len(text) > budget:
        victim = next((s for s in order if not s.dropped), None)
        if victim is None:
            break
        if victim.rows:
            # Remove enough rows at once to fit, at least one.
            excess = len(text) - budget
            removed = 0
            while victim.rows and excess > 0:
                excess -= len(victim.rows.pop()) + 1
                removed += 1
            victim.truncated += removed
        else:
            victim.dropped = True
        text = assemble()
    return text if len(text) <= budget else text[: budget - 1] + "\n"


def _money(x: float) -> str:
    return f"{x:,.0f}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


_PCT_WORDS = ("rate", "return", "drawdown", "exposure", "bound", "cagr")


def _cell(key: str, v: Any) -> str:
    """Fractions are shown as % only for rate-like metrics (expectancy stays in R)."""
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return _pct(v) if any(w in key for w in _PCT_WORDS) else f"{v:,.2f}"
    return str(v)


def daily_pack(
    day: date,
    account: Mapping[str, float],
    fills: Sequence[Mapping[str, Any]],
    exits: Sequence[Mapping[str, Any]],
    positions: Sequence[PositionRow],
    prices: Mapping[str, float],
    rejections: Mapping[str, int],
    gate_health: Sequence[Mapping[str, Any]],
    flags: Mapping[str, bool],
    anomalies: Sequence[str],
    shadow: Sequence[str] = (),
) -> str:
    acct = (
        f"equity {_money(account['equity'])} | day P&L {account.get('day_pnl', 0):+,.2f} "
        f"({_pct(account.get('day_pct'))}) | drawdown {_pct(account.get('drawdown'))}"
    )
    pos_rows = []
    for p in positions:
        price = prices.get(p.symbol, p.entry_price)
        r = (price - p.entry_price) / (p.entry_price - p.initial_stop)
        pos_rows.append(
            f"| {p.symbol} | {p.shares} | {p.entry_price:.2f} | {p.stop:.2f} | {r:+.2f} "
            f"| {p.days_held} | {short_signature(p.signature)} |"
        )
    sections = [
        Section("Account", [acct], 100),
        Section("Flags", [" ".join(f"{k}={'yes' if v else 'no'}" for k, v in flags.items())], 90),
        Section("Anomalies", [f"- {a}" for a in anomalies], 80),
        Section(
            "Exits",
            [
                f"| {e['symbol']} | {e['qty']} | {e['price']:.2f} | {e['pnl']:+,.2f} "
                f"| {e['r']:+.2f} | {exit_code(e['reason'])} |"
                for e in exits
            ],
            70,
            header=["| sym | qty | price | P&L | R | exit |", "|---|---|---|---|---|---|"],
        ),
        Section(
            "Fills",
            [f"| {f['symbol']} | {f['side']} | {f['qty']} | {f['price']:.2f} |" for f in fills],
            60,
            header=["| sym | side | qty | price |", "|---|---|---|---|"],
        ),
        Section(
            "Open positions",
            pos_rows,
            50,
            header=[
                "| sym | sh | entry | stop | R | days | sig |",
                "|---|---|---|---|---|---|---|",
            ],
        ),
        Section("Rejected signals", [f"- {k}: {v}" for k, v in rejections.items()], 40),
        Section(
            "Gate health (live, last 30)",
            [
                f"| {short_signature(g['signature'])} | {g['n']} | {_pct(g['win_rate'])} |"
                for g in gate_health
            ],
            30,
            header=["| sig | n | win% |", "|---|---|---|"],
        ),
        Section("Shadow trades (every signal, taken or not)", list(shadow), 20),
    ]
    for s in sections:  # drop empty tables
        if not s.rows:
            s.header = []
    return render(f"# Daily pack {day.isoformat()}\n{LEGEND}", sections, DAILY_BUDGET)


def weekly_pack(
    week: str,
    metrics: Mapping[str, Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    signature_health: Sequence[Mapping[str, Any]],
    altdata_hits: Sequence[str],
    rule_audit: Mapping[str, int],
    proposals: Sequence[str],
) -> str:
    keys = sorted({k for m in metrics.values() for k in m})
    metric_rows = [
        f"| {k} | " + " | ".join(_cell(k, metrics[p].get(k)) for p in metrics) + " |" for k in keys
    ]
    by_pnl = sorted(trades, key=lambda t: t["pnl"])

    def trade_row(t: Mapping[str, Any]) -> str:
        return (
            f"| {t['symbol']} | {t['exit_date']} | {t['pnl']:+,.2f} | {t['r_multiple']:+.2f} "
            f"| {exit_code(t['exit_reason'])} | {short_signature(t['signature'])} |"
        )

    trade_header = ["| sym | exit | P&L | R | exit | sig |", "|---|---|---|---|---|---|"]
    sections = [
        Section(
            "Metrics",
            metric_rows,
            100,
            header=["| metric | " + " | ".join(metrics) + " |", "|---" * (len(metrics) + 1) + "|"],
        ),
        Section("Rule audit", [f"- {k}: {v}" for k, v in rule_audit.items()], 95),
        Section(
            "Signature health (live vs floor)",
            [
                f"| {short_signature(s['signature'])} | {s['n']} | {_pct(s['win_rate'])} |"
                for s in signature_health
            ],
            90,
            header=["| sig | n | win% |", "|---|---|---|"],
        ),
        Section("Last proposals", [f"- {p}" for p in proposals], 85),
        Section(
            "Top 5 winners / losers",
            [trade_row(t) for t in [*by_pnl[-5:][::-1], *by_pnl[:5]]]
            if len(by_pnl) > 10
            else [trade_row(t) for t in by_pnl[::-1]],
            80,
            header=trade_header,
        ),  # fmt: skip
        Section("Alt-data hits", [f"- {h}" for h in altdata_hits], 70),
        Section("All closed trades", [trade_row(t) for t in trades], 10, header=trade_header),
    ]
    for s in sections:
        if not s.rows:
            s.header = []
    return render(f"# Weekly pack {week}\n{LEGEND}", sections, WEEKLY_BUDGET)


def write_pack(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text)
    return path


_DAILY = re.compile(r"^daily-(\d{4}-\d{2}-\d{2})\.md$")
_WEEKLY = re.compile(r"^weekly-(\d{4})-W(\d{2})\.md$")


def cleanup_packs(directory: Path, today: date, keep_days: int = 30) -> list[Path]:
    """Delete packs older than ``keep_days`` (they can be rebuilt from bot.db)."""
    cutoff = today - timedelta(days=keep_days)
    removed = []
    for path in sorted(directory.glob("*.md")) if directory.exists() else []:
        if m := _DAILY.match(path.name):
            day = date.fromisoformat(m.group(1))
        elif m := _WEEKLY.match(path.name):
            day = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        else:
            continue
        if day < cutoff:
            path.unlink()
            removed.append(path)
    return removed
