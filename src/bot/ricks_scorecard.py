# Author: Kai0421
"""Council scorecard: is the council getting better? Pure functions over the paper ledger.

The Ricks are fixed rules; they do not learn by themselves. "Improving" means we measure here,
then change a rule deliberately (owner approves, logged in docs/22-council-changelog.md) and
compare each rules version's results separately. Nothing here can place an order.
"""

from __future__ import annotations

import html
from collections.abc import Mapping

import pandas as pd

from bot.ricks_paper import GATE_TRADES, RULES_CHANGES, RULES_VERSION, START_EQUITY, money
from bot.ricks_shadow import NAMES
from bot.risk import wilson_lower_bound

ROLLING = 20


def wilson_interval(wins: int, n: int) -> tuple[float, float] | None:
    """95% Wilson interval for a win rate (None when there are no trades)."""
    if n <= 0:
        return None
    return wilson_lower_bound(wins, n), 1.0 - wilson_lower_bound(n - wins, n)


def _closed(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    c = trades[trades["status"] == "closed"].copy()
    c["exit_date"] = pd.to_datetime(c["exit_date"])
    return c.sort_values(["exit_date", "id"]).reset_index(drop=True)


def _block(c: pd.DataFrame) -> dict:
    n = len(c)
    wins = int((c["pnl"] > 0).sum()) if n else 0
    return {
        "finished": n,
        "wins": wins,
        "win_rate": wins / n if n else None,
        "interval": wilson_interval(wins, n),
        "avg_r": float(c["r"].mean()) if n else None,
        "pnl": float(c["pnl"].sum()) if n else 0.0,
    }


def weekly(trades: pd.DataFrame) -> list[dict]:
    """One block per week (Monday date) in which pretend trades finished, oldest first."""
    c = _closed(trades)
    if c.empty:
        return []
    c["week"] = c["exit_date"].dt.to_period("W-SUN").dt.start_time
    return [{"week": w, **_block(g)} for w, g in c.groupby("week")]


def rolling(trades: pd.DataFrame, window: int = ROLLING) -> pd.DataFrame:
    """Win rate and average R of the last ``window`` finished trades (NaN until enough)."""
    c = _closed(trades)
    out = pd.DataFrame({"trade": range(1, len(c) + 1)})
    if c.empty:
        out["win_rate"] = out["avg_r"] = pd.Series(dtype=float)
        return out
    out["exit_date"] = c["exit_date"]
    out["win_rate"] = (c["pnl"] > 0).astype(float).rolling(window, min_periods=window).mean()
    out["avg_r"] = c["r"].rolling(window, min_periods=window).mean()
    return out


def max_drawdown(equity: pd.DataFrame) -> float:
    """Worst peak-to-trough fall of the account, as a fraction (0.05 = 5%)."""
    if equity.empty:
        return 0.0
    e = equity["equity"].astype(float)
    return float(((e.cummax() - e) / e.cummax()).max())


def by_version(trades: pd.DataFrame) -> dict[str, dict]:
    c = _closed(trades)
    if c.empty or "rules_version" not in c:
        return {}
    return {str(v): _block(g) for v, g in c.groupby("rules_version")}


def build(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    raises: pd.DataFrame,
    ablation: Mapping | None = None,
) -> dict:
    """Everything the Slack post and the HTML report show."""
    c = _closed(trades)
    cum = _block(c)
    last = float(equity["equity"].iloc[-1]) if len(equity) else START_EQUITY
    spy = None
    if len(equity) >= 2:
        spy = float(equity["spy"].iloc[-1] / equity["spy"].iloc[0] - 1)
    return {
        "cumulative": cum,
        "weekly": weekly(trades),
        "rolling": rolling(trades),
        "versions": by_version(trades),
        "equity": equity,
        "profit": last - START_EQUITY,
        "return_pct": last / START_EQUITY - 1,
        "spy_pct": spy,
        "max_drawdown": max_drawdown(equity),
        "stops_raised": 0 if raises is None or raises.empty else len(raises),
        "open": int((trades["status"] == "open").sum()) if not trades.empty else 0,
        "since": equity["day"].iloc[0] if len(equity) else None,
        "through": equity["day"].iloc[-1] if len(equity) else None,
        "ablation": dict(ablation or {}),
    }


def verdict(card: Mapping) -> str:
    n = card["cumulative"]["finished"]
    if n == 0:
        return f"0 of {GATE_TRADES} trades: nothing to judge yet."
    if n < GATE_TRADES:
        return f"{n} of {GATE_TRADES} trades: too early to judge. Keep collecting."
    lo = card["cumulative"]["interval"][0]
    return (
        f"{n} of {GATE_TRADES} trades done. The win rate's lower bound is {lo:.0%}; the real bot "
        "needs at least 60% and a positive average result before it would trade."
    )


# ------------------------------------------------------------------ plain-English weekly post


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _rick_lines(ablation: Mapping) -> list[str]:
    by = ablation.get("by_rick", {})
    rows = [(k, v) for k, v in by.items() if v and v.get("n")]
    if not rows:
        return ["Each Rick on his own: not enough labelled days yet."]
    lines = ["Each Rick's 'yes' votes on their own (5 days later, vs the average stock):"]
    for k, v in rows:
        lines.append(
            f"• {NAMES.get(k, k)}: {v['mean_x5_bps']:+.0f} basis points on average over "
            f"{v['n']} votes ({v['hit5']:.0%} went up)"
        )
    return lines


def weekly_message(card: Mapping, week_start: pd.Timestamp) -> tuple[str, str]:
    cum = card["cumulative"]
    this = next((w for w in card["weekly"] if w["week"] == week_start), None)
    if this:
        lines = [
            f"This week {this['finished']} pretend trade{'s' if this['finished'] != 1 else ''} "
            f"finished, {this['wins']} won, and in total they "
            f"{'made' if this['pnl'] >= 0 else 'lost'} "
            f"{money(abs(this['pnl']))}."
        ]
    else:
        lines = ["No pretend trades finished this week."]
    spy = (
        ""
        if card["spy_pct"] is None
        else f" The overall market (SPY) moved {card['spy_pct']:+.1%}."
    )
    lines.append(
        f"Since the start the practice account is {money(card['profit'], True)} "
        f"({card['return_pct']:+.1%}).{spy}"
    )
    if cum["finished"]:
        lo, hi = cum["interval"]
        lines.append(
            f"Win rate so far: {_pct(cum['win_rate'])} of {cum['finished']} finished "
            f"trade{'s' if cum['finished'] != 1 else ''} "
            f"(plausibly anywhere from {lo:.0%} to {hi:.0%}). On average a trade "
            f"{'gains' if cum['avg_r'] >= 0 else 'loses'} {abs(cum['avg_r']):.2f} times what was "
            "risked on it."
        )
    r = card["rolling"].dropna(subset=["win_rate"])
    if len(r):
        lines.append(
            f"Last {ROLLING} trades: {r['win_rate'].iloc[-1]:.0%} won, "
            f"{r['avg_r'].iloc[-1]:+.2f} per trade on average."
        )
    lines += _rick_lines(card["ablation"])
    lines.append(
        f"Worst dip so far: {card['max_drawdown']:.1%}. Pretend stop-losses raised: "
        f"{card['stops_raised']}."
    )
    lines.append(verdict(card))
    lines.append(f"Rules version in use: {RULES_VERSION}. {RULES_CHANGES[RULES_VERSION]}")
    lines.append(
        "The Ricks are fixed rules and do not learn by themselves. Any change to their rules "
        "is made by us on purpose and recorded in the change log, so each version is judged "
        "on its own results."
    )
    lines.append("Practice only: no real order was placed and no real money is involved.")
    return f"Council scorecard, week of {week_start:%d %b}", "\n".join(lines)


# ------------------------------------------------------------------ HTML report

_CSS = """
:root{--bg:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--grid:#e4e3df;
--s1:#2a78d6;--s2:#eb6834;--good:#0ca30c;--bad:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--grid:#383835;--s1:#3987e5;--s2:#d95926}}
:root[data-theme=dark]{--bg:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--grid:#383835;--s1:#3987e5;--s2:#d95926}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:.2em 0}h2{font-size:1.05rem;margin:1.4em 0 .4em}
.card{background:var(--surface);border:1px solid var(--grid);border-radius:12px;padding:14px;
margin:12px 0}.muted{color:var(--ink2);font-size:.9rem}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--surface);border:1px solid var(--grid);border-radius:12px;padding:10px 12px}
.tile b{display:block;font-size:1.35rem}.tile span{color:var(--ink2);font-size:.85rem}
svg{width:100%;height:auto;display:block}svg text{fill:var(--ink2);font-size:11px}
.grid{stroke:var(--grid);stroke-width:1}.l1{stroke:var(--s1);fill:none;stroke-width:2}
.l2{stroke:var(--s2);fill:none;stroke-width:2}.legend{display:flex;gap:16px;flex-wrap:wrap;
font-size:.9rem;color:var(--ink2);margin-top:6px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{text-align:right;padding:4px 6px;
border-bottom:1px solid var(--grid)}th:first-child,td:first-child{text-align:left}
details{margin-top:8px}
"""


def _fmt_date(x) -> str:
    return pd.Timestamp(x).strftime("%d %b")


def _line_svg(series: dict[str, list[float]], dates: list, cls: dict[str, str], unit: str) -> str:
    n = max((len(v) for v in series.values()), default=0)
    if n < 2:
        return '<p class="muted">Needs at least two days of data.</p>'
    w, h, ml, mr, mt, mb = 640, 240, 44, 56, 12, 24
    ys = [y for v in series.values() for y in v]
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.1 or 1.0
    lo, hi = lo - pad, hi + pad

    def px(i):
        return ml + (w - ml - mr) * i / (n - 1)

    def py(y):
        return mt + (h - mt - mb) * (1 - (y - lo) / (hi - lo))

    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Line chart">']
    for k in range(4):
        v = lo + (hi - lo) * k / 3
        out.append(
            f'<line class="grid" x1="{ml}" x2="{w - mr}" y1="{py(v):.1f}" y2="{py(v):.1f}"/>'
            f'<text x="{ml - 6}" y="{py(v) + 4:.1f}" text-anchor="end">{v:.1f}{unit}</text>'
        )
    out.append(
        f'<text x="{ml}" y="{h - 6}">{html.escape(_fmt_date(dates[0]))}</text>'
        f'<text x="{w - mr}" y="{h - 6}" text-anchor="end">'
        f"{html.escape(_fmt_date(dates[-1]))}</text>"
    )
    for name, v in series.items():
        pts = " ".join(f"{px(i):.1f},{py(y):.1f}" for i, y in enumerate(v))
        out.append(f'<polyline class="{cls[name]}" points="{pts}"/>')
        for i, y in enumerate(v):
            out.append(
                f'<circle cx="{px(i):.1f}" cy="{py(y):.1f}" r="4" fill="transparent">'
                f"<title>{html.escape(name)} {html.escape(_fmt_date(dates[i]))}: {y:.2f}{unit}"
                "</title></circle>"
            )
        out.append(
            f'<text x="{px(len(v) - 1) + 6:.1f}" y="{py(v[-1]) + 4:.1f}">{html.escape(name)}</text>'
        )
    out.append("</svg>")
    return "".join(out)


def _bars_svg(rows: list[tuple[str, float, int]]) -> str:
    """Horizontal bars of average 5-day excess return (basis points) with the vote count."""
    if not rows:
        return (
            '<p class="muted">Not enough labelled days yet (needs 5 trading days after a vote).</p>'
        )
    w, rowh, ml = 640, 30, 150
    span = max(abs(v) for _, v, _ in rows) or 1.0
    zero = ml + (w - ml - 70) / 2
    scale = (w - ml - 70) / 2 / span
    h = rowh * len(rows) + 8
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Bar chart">']
    out.append(f'<line class="grid" x1="{zero:.1f}" x2="{zero:.1f}" y1="0" y2="{h}"/>')
    for i, (name, v, n) in enumerate(rows):
        y = 4 + i * rowh
        x = zero if v >= 0 else zero + v * scale
        out.append(
            f'<text x="{ml - 8}" y="{y + 17}" text-anchor="end">{html.escape(name)}</text>'
            f'<rect x="{x:.1f}" y="{y + 4}" width="{abs(v) * scale:.1f}" height="16" rx="3" '
            f'fill="var(--s1)"><title>{html.escape(name)}: {v:+.0f} bps over {n} votes'
            "</title></rect>"
            f'<text x="{w - 66}" y="{y + 17}">{v:+.0f} bps (n={n})</text>'
        )
    out.append("</svg>")
    return "".join(out)


def html_report(card: Mapping, generated: str = "") -> str:
    cum = card["cumulative"]
    eq = card["equity"]
    tiles = [
        (f"{cum['finished']} of {GATE_TRADES}", "finished pretend trades"),
        (_pct(cum["win_rate"]), "win rate" + (
            f" ({cum['interval'][0]:.0%} to {cum['interval'][1]:.0%})" if cum["interval"] else "")),
        ("n/a" if cum["avg_r"] is None else f"{cum['avg_r']:+.2f}", "average result per trade"),
        (money(card["profit"], True), "practice profit" + (
            "" if card["spy_pct"] is None else f" (market {card['spy_pct']:+.1%})")),
        (f"{card['max_drawdown']:.1%}", "worst dip"),
        (str(card["stops_raised"]), "stop-losses raised"),
    ]  # fmt: skip
    tile_html = "".join(
        f"<div class='tile'><b>{html.escape(a)}</b><span>{html.escape(b)}</span></div>"
        for a, b in tiles
    )
    equity_chart = "<p class='muted'>No days yet.</p>"
    if len(eq) >= 2:
        base_e, base_s = float(eq["equity"].iloc[0]), float(eq["spy"].iloc[0])
        equity_chart = _line_svg(
            {
                "Council": [100 * x / base_e for x in eq["equity"]],
                "SPY": [100 * x / base_s for x in eq["spy"]],
            },
            list(eq["day"]), {"Council": "l1", "SPY": "l2"}, "",
        )  # fmt: skip
    r = card["rolling"].dropna(subset=["win_rate"])
    if len(r):
        roll_chart = _line_svg(
            {"Win rate": [100 * x for x in r["win_rate"]]}, list(r["exit_date"]),
            {"Win rate": "l1"}, "%",
        )  # fmt: skip
    else:
        roll_chart = f"<p class='muted'>Needs {ROLLING} finished trades first.</p>"
    by = card["ablation"].get("by_rick", {})
    bars = [
        (NAMES.get(k, k) + " said yes", v["mean_x5_bps"], v["n"])
        for k, v in by.items()
        if v and v.get("n")
    ]
    buys = card["ablation"].get("buys")
    if buys and buys.get("n"):
        bars.append(("Council buys", buys["mean_x5_bps"], buys["n"]))
    week_rows = (
        "".join(
            f"<tr><td>{w['week']:%d %b}</td><td>{w['finished']}</td><td>{_pct(w['win_rate'])}</td>"
            f"<td>{'n/a' if w['avg_r'] is None else format(w['avg_r'], '+.2f')}</td>"
            f"<td>{money(w['pnl'], True)}</td></tr>"
            for w in card["weekly"]
        )
        or "<tr><td colspan='5'>No finished trades yet</td></tr>"
    )
    ver_rows = (
        "".join(
            f"<tr><td>{html.escape(v)}</td><td>{b['finished']}</td><td>{_pct(b['win_rate'])}</td>"
            f"<td>{'n/a' if b['avg_r'] is None else format(b['avg_r'], '+.2f')}</td>"
            f"<td>{money(b['pnl'], True)}</td></tr>"
            for v, b in card["versions"].items()
        )
        or "<tr><td colspan='5'>No finished trades yet</td></tr>"
    )
    eq_rows = "".join(
        f"<tr><td>{_fmt_date(d)}</td><td>{money(e)}</td><td>{s:.2f}</td></tr>"
        for d, e, s in zip(eq["day"], eq["equity"], eq["spy"], strict=True)
    )
    through = "" if card["through"] is None else f"Data through {_fmt_date(card['through'])}. "
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Council Scorecard</title><style>{_CSS}</style></head><body><main>
<h1>Council of Ricks: practice scorecard</h1>
<p class="muted">{html.escape(through)}Practice only: no real orders, no real money.
The Ricks are fixed rules and do not learn by themselves; we measure them here and change
rules on purpose.</p>
<div class="card"><b>{html.escape(verdict(card))}</b></div>
<div class="tiles">{tile_html}</div>
<h2>Practice account vs the market (both start at 100)</h2><div class="card">{equity_chart}
<div class="legend"><span><i class="sw" style="background:var(--s1)"></i>Council</span>
<span><i class="sw" style="background:var(--s2)"></i>SPY</span></div></div>
<h2>Win rate of the last {ROLLING} finished trades</h2><div class="card">{roll_chart}</div>
<h2>Each Rick's yes votes: average 5-day result vs the average stock</h2>
<div class="card">{_bars_svg(bars)}</div>
<h2>By week</h2><div class="card"><table><tr><th>Week of</th><th>Finished</th><th>Win rate</th>
<th>Avg result</th><th>Profit</th></tr>{week_rows}</table></div>
<h2>By rules version</h2><div class="card"><table><tr><th>Version</th><th>Finished</th>
<th>Win rate</th><th>Avg result</th><th>Profit</th></tr>{ver_rows}</table>
<p class="muted">Every rule change is logged in docs/22-council-changelog.md so versions are
compared separately.</p></div>
<details class="card"><summary>Table view of the account curve</summary>
<table><tr><th>Day</th><th>Account value</th><th>SPY</th></tr>{eq_rows}</table></details>
<p class="muted">{html.escape(generated)}</p>
</main></body></html>
"""
