# Author: Kai0421
"""Weekly dashboard report (docs/09-weekly-report.md). Deterministic Python, no LLM.

Output in ``reports/weekly/YYYY-Www/`` (git-ignored: it contains account values):
- six PNG charts (matplotlib): equity vs SPY rebased to 100, drawdown with the -10%
  kill line, daily P&L this week, R-multiple histogram, win rate by signature with
  the 95% Wilson interval and the win-probability floor, cash exposure;
- ``summary.json`` (week / 4-week / since-inception metrics, rule audit, gate health);
- ``report.html``: self-contained, charts embedded as base64 PNGs (no Plotly dependency).

Also writes the size-capped weekly pack for the AI review and posts the charts to Slack,
storing the message ts so the AI review can reply in that thread.
"""

from __future__ import annotations

import base64
import html
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from bot.notify import Level  # noqa: E402
from bot.pack import weekly_pack, write_pack  # noqa: E402
from bot.risk import HARD_MIN_WIN_PROB_FLOOR, wilson_lower_bound  # noqa: E402

DEFAULT_REPORTS_DIR = Path("reports/weekly")
# Validated categorical slots 1-2 (blue, orange) on a light surface; ink for text.
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
RULE_REJECTIONS = (
    "max open positions",
    "max per sector",
    "not enough tradable cash",
    "kill switch",
)


def week_bounds(today: date) -> tuple[str, date, date]:
    """ISO week label and its Monday-Friday for the report run on ``today`` (Saturday)."""
    y, w, _ = today.isocalendar()
    monday = date.fromisocalendar(y, w, 1)
    return f"{y}-W{w:02d}", monday, monday + timedelta(days=4)


def _ret(series: pd.Series) -> float | None:
    s = series.dropna()
    return None if len(s) < 2 else float(s.iloc[-1] / s.iloc[0] - 1)


def period_metrics(
    trades: Sequence[Mapping[str, Any]], equity: pd.DataFrame, spy_close: pd.Series
) -> dict[str, Any]:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)

    def mean(xs: Sequence[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    eq = equity["equity"] if len(equity) else pd.Series(dtype=float)
    return {
        "trades": n,
        "win_rate": len(wins) / n if n else None,
        "wilson_lb": wilson_lower_bound(len(wins), n) if n else None,
        "expectancy_r": mean([t["r_multiple"] for t in trades]),
        "avg_win_r": mean([t["r_multiple"] for t in wins]),
        "avg_loss_r": mean([t["r_multiple"] for t in losses]),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "avg_holding_days": mean([t["holding_days"] for t in trades]),
        "return": _ret(eq),
        "spy_return": _ret(spy_close),
        "max_drawdown": float((1 - eq / eq.cummax()).max()) if len(eq) else None,
    }


# --------------------------------------------------------- charts


def _axes(title: str):
    fig, ax = plt.subplots(figsize=(8, 4), dpi=110)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=12, fontweight="bold")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    return fig, ax


def _save(fig: Any, path: Path) -> Path:
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


def _ref_line(ax: Any, y: float, label: str) -> None:
    ax.axhline(y, color=MUTED, linewidth=1, linestyle="--")
    ax.annotate(label, (0, y), xycoords=("axes fraction", "data"), xytext=(4, 4),
                textcoords="offset points", ha="left", color=MUTED, fontsize=8)  # fmt: skip


def render_charts(
    out: Path,
    equity: pd.DataFrame,
    spy_close: pd.Series,
    trades: Sequence[Mapping[str, Any]],
    week_start: date,
) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = []

    fig, ax = _axes("Equity vs SPY (rebased to 100)")
    if len(equity):
        eq = equity["equity"] / equity["equity"].iloc[0] * 100
        ax.plot(eq.index, eq.values, color=BLUE, linewidth=2, label="Bot")
        spy = spy_close.reindex(equity.index).ffill().dropna()
        if len(spy):
            ax.plot(spy.index, spy / spy.iloc[0] * 100, color=ORANGE, linewidth=2, label="SPY")
        ax.legend(frameon=False, labelcolor=INK, fontsize=9)
    files.append(_save(fig, out / "1_equity_vs_spy.png"))

    fig, ax = _axes("Drawdown from peak (%)")
    if len(equity):
        dd = -equity["drawdown"] * 100
        ax.fill_between(dd.index, dd.values, 0, color=BLUE, alpha=0.25, linewidth=0)
        ax.plot(dd.index, dd.values, color=BLUE, linewidth=2)
    ax.set_ylim(min(-12, ax.get_ylim()[0]), 0.5)
    _ref_line(ax, -10, "-10% kill switch")
    files.append(_save(fig, out / "2_drawdown.png"))

    fig, ax = _axes("Daily P&L this week ($)")
    week = equity[equity.index >= pd.Timestamp(week_start)] if len(equity) else equity
    if len(equity) >= 2 and len(week):
        pnl = equity["equity"].diff().reindex(week.index).fillna(0)
        colors = [BLUE if v >= 0 else ORANGE for v in pnl]
        ax.bar([d.strftime("%a %d") for d in pnl.index], pnl.values, color=colors, width=0.6)
        ax.axhline(0, color=MUTED, linewidth=1)
    files.append(_save(fig, out / "3_daily_pnl.png"))

    fig, ax = _axes("R-multiple of closed trades")
    rs = [t["r_multiple"] for t in trades]
    if rs:
        ax.hist(rs, bins=min(20, max(5, len(rs))), color=BLUE, edgecolor=SURFACE, linewidth=2)
        ax.axvline(0, color=MUTED, linewidth=1)
    ax.set_xlabel("R", color=MUTED)
    files.append(_save(fig, out / "4_r_distribution.png"))

    fig, ax = _axes("Win rate by setup signature (95% Wilson interval)")
    groups: dict[str, list[bool]] = defaultdict(list)
    for t in trades:
        groups[t["signature"]].append(t["pnl"] > 0)
    if groups:
        from bot.pack import short_signature

        names = sorted(groups)
        rates = [sum(groups[s]) / len(groups[s]) * 100 for s in names]
        lows = [wilson_lower_bound(sum(groups[s]), len(groups[s])) * 100 for s in names]
        highs = [100 - wilson_lower_bound(len(groups[s]) - sum(groups[s]), len(groups[s])) * 100
                 for s in names]  # fmt: skip
        x = range(len(names))
        ax.vlines(x, lows, highs, color=BLUE, linewidth=2)
        ax.scatter(x, rates, color=BLUE, s=64, zorder=3, edgecolors=SURFACE, linewidths=2)
        ax.set_xticks(list(x), [f"{short_signature(s)}\nn={len(groups[s])}" for s in names])
    ax.set_ylim(0, 100)
    _ref_line(ax, HARD_MIN_WIN_PROB_FLOOR * 100, f"{HARD_MIN_WIN_PROB_FLOOR:.0%} floor")
    files.append(_save(fig, out / "5_win_rate_by_signature.png"))

    fig, ax = _axes("Exposure: % of equity invested")
    if len(equity):
        exp = (1 - equity["cash"] / equity["equity"]) * 100
        ax.plot(exp.index, exp.values, color=BLUE, linewidth=2)
    ax.set_ylim(0, 100)
    files.append(_save(fig, out / "6_exposure.png"))
    return files


# --------------------------------------------------------- html


def _fmt(key: str, v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        pct = any(w in key for w in ("rate", "return", "drawdown", "lb"))
        return f"{v * 100:.1f}%" if pct else f"{v:,.2f}"
    return str(v)


def render_html(summary: Mapping[str, Any], charts: Sequence[Path]) -> str:
    periods = summary["periods"]
    keys = list(next(iter(periods.values())))
    head = "".join(f"<th>{html.escape(p)}</th>" for p in periods)
    rows = "".join(
        f"<tr><td>{html.escape(k)}</td>"
        + "".join(f"<td>{_fmt(k, periods[p][k])}</td>" for p in periods)
        + "</tr>"
        for k in keys
    )
    imgs = "".join(
        f'<img alt="{html.escape(c.stem)}" src="data:image/png;base64,'
        f'{base64.b64encode(c.read_bytes()).decode()}">'
        for c in charts
    )
    audit = "".join(f"<li>{html.escape(k)}: {v}</li>" for k, v in summary["rule_audit"].items())
    rejected = (
        "".join(f"<li>{html.escape(k)}: {v}</li>" for k, v in summary["rejections"].items())
        or "<li>none</li>"
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekly report {html.escape(summary["week"])}</title>
<style>
body{{font:15px/1.5 -apple-system,system-ui,sans-serif;color:{INK};background:{SURFACE};
margin:0 auto;max-width:900px;padding:16px}}
h1{{font-size:22px}} h2{{font-size:17px;margin-top:28px}}
table{{border-collapse:collapse;width:100%}} td,th{{padding:4px 8px;border-bottom:1px solid {GRID};
text-align:right}} td:first-child,th:first-child{{text-align:left}}
img{{width:100%;height:auto;margin:8px 0}} .muted{{color:{MUTED}}}
</style></head><body>
<h1>Weekly report {html.escape(summary["week"])}</h1>
<p class="muted">{summary["start"]} to {summary["end"]} · paper trading</p>
<h2>Key metrics</h2><table><tr><th>metric</th>{head}</tr>{rows}</table>
<h2>Hard-rule audit</h2><ul>{audit}</ul>
<h2>Signals rejected this week</h2><ul>{rejected}</ul>
<h2>Charts</h2>{imgs}
</body></html>
"""


# --------------------------------------------------------- job


def run_weekly_report(engine: Any, reports_dir: Path = DEFAULT_REPORTS_DIR) -> Path:
    j = engine.journal
    today = engine.clock().date()
    week, start, end = week_bounds(today)
    equity = j.equity_history()
    all_trades = j.trades()
    first = start - timedelta(weeks=3, days=7)
    if len(equity):
        first = min(first, equity.index[0].date() - timedelta(days=7))
    spy = engine.load_bars(["SPY"], first, end).get("SPY")
    spy_close = spy["close"] if spy is not None else pd.Series(dtype=float)

    def spy_over(eq: pd.DataFrame, lo: pd.Timestamp | None) -> pd.Series:
        """SPY over the same dates as the bot's equity window (else ``lo`` to ``end``)."""
        if len(eq):
            lo, hi = eq.index.min(), eq.index.max()
        elif lo is None:
            return pd.Series(dtype=float)
        else:
            hi = pd.Timestamp(end)
        return spy_close[(spy_close.index >= lo) & (spy_close.index <= hi)]

    def window(since: date) -> tuple[list, pd.DataFrame, pd.Series]:
        lo = pd.Timestamp(since) - pd.Timedelta(days=3)
        trades = [t for t in all_trades if since.isoformat() <= t["exit_date"] <= end.isoformat()]
        eq = equity[(equity.index >= lo) & (equity.index <= pd.Timestamp(end))]
        return trades, eq, spy_over(eq, lo)

    periods = {
        "week": period_metrics(*window(start)),
        "4 weeks": period_metrics(*window(start - timedelta(weeks=3))),
        "all": period_metrics(all_trades, equity, spy_over(equity, None)),
    }
    rejections = j.rejection_counts(start, end)
    orders = [o for d in pd.bdate_range(start, end) for o in j.orders_on(d.date())]
    rule_audit = {
        "blocked": sum(v for k, v in rejections.items() if any(r in k for r in RULE_REJECTIONS)),
        "reached_broker": sum(1 for o in orders if o["status"] == "rejected"),
    }
    outcomes = j.outcomes_by_signature()
    health = [
        {"signature": s, "n": len(o[-30:]), "win_rate": sum(o[-30:]) / len(o[-30:])}
        for s, o in sorted(outcomes.items())
    ]
    summary = {
        "week": week,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "periods": periods,
        "rule_audit": rule_audit,
        "rejections": rejections,
        "gate_health": health,
    }
    out = reports_dir / week
    week_trades = window(start)[0]
    charts = render_charts(out, equity, spy_close, all_trades, start)
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str) + "\n")
    (out / "report.html").write_text(render_html(summary, charts))
    write_pack(
        engine.memory_dir / "packs",
        f"weekly-{week}.md",
        weekly_pack(week, periods, week_trades, health, [], rule_audit, _last_proposals(engine)),
    )
    w = periods["week"]
    text = (
        f"Return {_fmt('return', w['return'])} vs SPY {_fmt('return', w['spy_return'])} | "
        f"trades {w['trades']} | win rate {_fmt('rate', w['win_rate'])} | "
        f"expectancy {_fmt('x', w['expectancy_r'])}R | max DD {_fmt('drawdown', w['max_drawdown'])}"
        f"\nRule audit: blocked {rule_audit['blocked']}, "
        f"reached broker {rule_audit['reached_broker']}"
        f"\nFull report: {out / 'report.html'}"
    )
    delivery = engine.notifier.notify(Level.INFO, f"Weekly report {week}", text, files=charts)
    if delivery.ref:
        j.set_kv("slack_thread:weekly", delivery.ref)
    return out


def _last_proposals(engine: Any) -> list[str]:
    """Proposals from the most recent weekly review, so the next review sees their status."""
    weekly = sorted((engine.memory_dir / "weekly").glob("*.md")) if engine.memory_dir else []
    if not weekly:
        return []
    lines = weekly[-1].read_text().splitlines()
    try:
        i = next(k for k, ln in enumerate(lines) if ln.startswith("## Proposals"))
    except StopIteration:
        return []
    out = []
    for ln in lines[i + 1 :]:
        if ln.startswith("## "):
            break
        if ln.strip():
            out.append(ln.strip().lstrip("0123456789. "))
    return out[:3]
