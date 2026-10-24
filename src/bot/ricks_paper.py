# Author: Kai0421
"""Council paper ledger: the Ricks pretend-trade a virtual account. Nothing here can place an order.

Pure functions over DataFrames (no broker, no network, no files; the runner does the I/O).
The council's buy verdicts from the shadow log are pretend-bought and pretend-sold under rules
fixed in advance, so the result is an honest answer to "would these decisions have made money?":

- **Entry**: the session after a buy verdict, at the open. Sized like the real bot
  (``bot.risk``): at most 1% of equity at risk, 6 positions, 2 per sector, cash only, 2% reserve.
- **Stop**: 2 x ATR(14) below the entry (the bot's own stop distance). Checked on the daily low;
  a gap below the stop fills at the open; the stop wins a same-day tie.
- **Trailing stop** (``trail_stop``; off unless ``TRAIL_STOP``): at each close the stop ratchets
  up to the highest close since entry minus 2 x ATR (the ATR at entry); never down, never below
  the first stop, applies from the next session. Fixed in advance, not tuned on shadow data.
- **Time limit**: sold at the close of the 5th session (entry day counts as the 1st). 5 is the
  longest horizon the shadow log already labels, so it was not tuned on these trades.
- **Council turns against**: after any close where a Rick vetoes the stock (degree <= -0.7) or
  two Ricks are against it (<= -0.3), sold at the next open.
- Costs: 5 bps per side. The whole ledger is recomputed from the log and the bars on every run
  (so it is idempotent); only the "message sent" flags are carried over by trade id.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import pandas as pd

from bot.config import RiskConfig
from bot.indicators import atr
from bot.ricks import DEFAULT_RULES
from bot.ricks_shadow import NAMES, RICKS
from bot.risk import size_position, wilson_lower_bound
from bot.slack_format import bullets, section, table

START_EQUITY = 100_000.0
STOP_ATR = 2.0
HOLD_SESSIONS = 5
COST_BPS = 5.0
# Trailing stop: built and tested, OFF by default. Back-test of the rule alone (random stocks,
# 3 a day, last year, 5-day hold) showed no clear gain: win rate 45.9% vs 48.8% and average
# return -0.17% vs +0.07% per trade for fixed (difference within noise). The owner's rule is
# to keep the simple stop when there is no clear difference. Switching it on is a rule change:
# bump RULES_VERSION and log it in docs/22-council-changelog.md.
TRAIL_STOP = False
RULES_VERSION = "v2"  # bump (and log in docs/22-council-changelog.md) on any rule change
# One plain-English line per version, told in Slack the first day a new version is live.
# A test fails if RULES_VERSION has no entry here, so a bump cannot forget it.
RULES_CHANGES = {
    "v1": "First rules: 2x ATR stop, 5-session limit, sell next open if the council turns against.",
    "v2": (
        "Skeptic Rick now counts an unknown earnings date as a warning, so he stops giving "
        "a free 'yes' vote. Trades already open keep their old rules."
    ),
}
GATE_TRADES = 100  # the real bot's rule: 100+ settled trades before a win rate means anything

COLUMNS = [
    "id", "signal_day", "symbol", "sector", "votes_for", "votes_other", "entry_date",
    "entry_price", "shares", "initial_stop", "stop", "stops_raised", "rules_version", "status",
    "exit_date", "exit_price", "exit_reason",
    "exit_note", "pnl", "risk_amount", "r", "buy_sent", "sell_sent",
]  # fmt: skip
EQUITY_COLUMNS = ["day", "cash", "equity", "open", "spy"]
RAISE_COLUMNS = [
    "id", "trade_id", "symbol", "day", "old_stop", "new_stop", "highest_close", "shares",
    "entry_price", "sent",
]  # fmt: skip


@dataclass(frozen=True)
class PaperResult:
    trades: pd.DataFrame
    equity: pd.DataFrame
    marks: dict[str, float]
    raises: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=RAISE_COLUMNS))


def trail_stop(current_stop: float, initial_stop: float, highest_close: float, atr: float) -> float:
    """The council's trailing-stop rule: highest close since entry minus 2 x ATR, only ever up.

    Never below the initial stop and never below the stop already in place. Applied at a
    session's close; it protects from the next session on.
    """
    return max(current_stop, initial_stop, highest_close - STOP_ATR * atr)


def _cost(price: float, shares: int) -> float:
    return price * shares * COST_BPS / 1e4


def _flip_note(row: Mapping) -> str | None:
    """Why the council now wants out of this stock, or None."""
    rules = DEFAULT_RULES
    veto = [NAMES[k] for k in RICKS if row[k] <= rules.veto]
    if veto:
        return f"{' and '.join(veto)} vetoed it"
    against = [NAMES[k] for k in RICKS if row[k] <= rules.against_at]
    if len(against) >= 2:
        return f"{' and '.join(against)} are now against it"
    return None


def _votes(row: Mapping) -> tuple[str, str]:
    pro = [NAMES[k] for k in RICKS if row[k] >= DEFAULT_RULES.for_at]
    other = [NAMES[k] for k in RICKS if row[k] < DEFAULT_RULES.for_at]
    return ", ".join(pro), ", ".join(other)


def replay(
    log: pd.DataFrame,
    bars: Mapping[str, pd.DataFrame],
    sectors: Mapping[str, str],
    through: pd.Timestamp | None = None,
    cfg: RiskConfig | None = None,
    trail: bool | None = None,
) -> PaperResult:
    """Run the pretend account over every session from the first logged day to ``through``."""
    cfg = cfg or RiskConfig()
    trail = TRAIL_STOP if trail is None else trail
    empty = PaperResult(pd.DataFrame(columns=COLUMNS), pd.DataFrame(columns=EQUITY_COLUMNS), {})
    spy = bars.get("SPY")
    if log.empty or spy is None:
        return empty
    log = log.copy()
    log["day"] = pd.to_datetime(log["day"])
    first = log["day"].min()
    sessions = [d for d in spy.index if d >= first and (through is None or d <= through)]
    if not sessions:
        return empty
    by_day = {d: g for d, g in log.groupby("day")}
    rows_by_key = {(r["day"], r["symbol"]): r for r in log.to_dict("records")}
    all_sessions = list(spy.index)

    cash = START_EQUITY
    held: dict[str, dict] = {}
    done: list[dict] = []
    raise_rows: list[dict] = []
    equity_rows: list[dict] = []
    prev_close: dict[str, float] = {}

    def bar(sym: str, d: pd.Timestamp):
        b = bars.get(sym)
        return None if b is None or d not in b.index else b.loc[d]

    def close_trade(pos: dict, d: pd.Timestamp, price: float, reason: str, note: str = "") -> None:
        nonlocal cash
        cash += price * pos["shares"] - _cost(price, pos["shares"])
        gross = (price - pos["entry_price"]) * pos["shares"]
        pnl = gross - _cost(pos["entry_price"], pos["shares"]) - _cost(price, pos["shares"])
        risk = (pos["entry_price"] - pos["initial_stop"]) * pos["shares"]
        done.append(
            {**pos["row"], "stop": pos["stop"], "stops_raised": pos["raised"],
             "status": "closed", "exit_date": d, "exit_price": price,
             "exit_reason": reason, "exit_note": note, "pnl": pnl, "risk_amount": risk,
             "r": pnl / risk if risk else math.nan}
        )  # fmt: skip

    for d in sessions:
        i = all_sessions.index(d)
        prev = all_sessions[i - 1] if i > 0 else None

        # 1. pretend sells queued by last night's council, at the open
        for sym in [s for s, p in held.items() if p["pending"] is not None]:
            b = bar(sym, d)
            if b is not None:
                pos = held.pop(sym)
                close_trade(pos, d, float(b["open"]), "council", pos["pending"])

        # 2. pretend buys from the previous session's verdicts, at the open
        cands = by_day.get(prev) if prev is not None else None
        if cands is not None:
            equity_open = cash + sum(p["shares"] * prev_close.get(s, p["entry_price"])
                                     for s, p in held.items())  # fmt: skip
            for r in (
                cands[cands["buy"].astype(bool)]
                .sort_values("mean", ascending=False)
                .to_dict("records")
            ):
                sym = r["symbol"]
                sec = sectors.get(sym, "?")
                b = bar(sym, d)
                full = bars.get(sym)
                if sym in held or b is None or full is None:
                    continue
                if len(held) >= cfg.max_open_positions:
                    break
                if sum(1 for s in held if sectors.get(s, "?") == sec) >= cfg.max_per_sector:
                    continue
                hist = full.loc[:prev]
                if len(hist) < 15:
                    continue
                a = atr(hist["high"], hist["low"], hist["close"], 14).iloc[-1]
                entry = float(b["open"])
                stop = entry - STOP_ATR * float(a)
                if pd.isna(a) or not 0 < stop < entry:
                    continue
                spendable = cash - cfg.cash_reserve_pct * equity_open
                shares = size_position(equity_open, entry, stop, spendable - _cost(entry, 0), cfg)
                if shares < 1:
                    continue
                cash -= shares * entry + _cost(entry, shares)
                pro, other = _votes(r)
                held[sym] = {
                    "shares": shares, "entry_price": entry, "stop": stop, "initial_stop": stop,
                    "atr": float(a), "highest": entry, "raised": 0, "n": 0, "pending": None,
                    "row": {
                        "id": f"{prev.date()}-{sym}", "signal_day": prev, "symbol": sym,
                        "sector": sec, "votes_for": pro, "votes_other": other, "entry_date": d,
                        "entry_price": entry, "shares": shares, "initial_stop": stop,
                        "rules_version": RULES_VERSION, "buy_sent": 0, "sell_sent": 0,
                    },
                }  # fmt: skip

        # 3. the session itself: stop, then time limit
        for sym in list(held):
            pos = held[sym]
            b = bar(sym, d)
            if b is None:
                continue
            pos["n"] += 1
            if float(b["low"]) <= pos["stop"]:
                held.pop(sym)
                close_trade(pos, d, min(float(b["open"]), pos["stop"]), "stop")
            elif pos["n"] >= HOLD_SESSIONS:
                held.pop(sym)
                close_trade(pos, d, float(b["close"]), "time")

        # 3b. after the close the stop follows the price up (from the next session on)
        for sym, pos in held.items():
            b = bar(sym, d)
            if b is None or not trail:
                continue
            pos["highest"] = max(pos["highest"], float(b["close"]))
            new = trail_stop(pos["stop"], pos["initial_stop"], pos["highest"], pos["atr"])
            if new > pos["stop"] + 1e-9:
                raise_rows.append(
                    {"id": f"{pos['row']['id']}-{d.date()}", "trade_id": pos["row"]["id"],
                     "symbol": sym, "day": d, "old_stop": pos["stop"], "new_stop": new,
                     "highest_close": pos["highest"], "shares": pos["shares"],
                     "entry_price": pos["entry_price"], "sent": 0}
                )  # fmt: skip
                pos["stop"] = new
                pos["raised"] += 1

        # 4. after the close: did the council turn against anything still held?
        for sym, pos in held.items():
            row = rows_by_key.get((d, sym))
            if row is not None and (note := _flip_note(row)):
                pos["pending"] = note
        for sym in bars:
            b = bar(sym, d)
            if b is not None:
                prev_close[sym] = float(b["close"])

        mkt = sum(p["shares"] * prev_close.get(s, p["entry_price"]) for s, p in held.items())
        equity_rows.append(
            {"day": d, "cash": cash, "equity": cash + mkt, "open": len(held),
             "spy": float(spy.loc[d, "close"])}
        )  # fmt: skip

    open_rows = [
        {**p["row"], "stop": p["stop"], "stops_raised": p["raised"], "status": "open",
         "exit_date": pd.NaT, "exit_price": math.nan,
         "exit_reason": "", "exit_note": "", "pnl": math.nan,
         "risk_amount": (p["entry_price"] - p["initial_stop"]) * p["shares"], "r": math.nan}
        for p in held.values()
    ]  # fmt: skip
    trades = pd.DataFrame(done + open_rows, columns=COLUMNS)
    trades = trades.sort_values(["entry_date", "symbol"]).reset_index(drop=True)
    marks = {s: prev_close[s] for s in held if s in prev_close}
    raises = pd.DataFrame(raise_rows, columns=RAISE_COLUMNS)
    return PaperResult(trades, pd.DataFrame(equity_rows, columns=EQUITY_COLUMNS), marks, raises)


# ------------------------------------------------------------------ carry-over, summary


def merge_sent(new: pd.DataFrame, old: pd.DataFrame) -> pd.DataFrame:
    """Keep the "message sent" flags of trades already in the ledger (matched by id)."""
    out = new.copy()
    for col in ("buy_sent", "sell_sent"):
        prior = old.set_index("id")[col] if not old.empty and col in old else pd.Series(dtype=int)
        out[col] = out["id"].map(prior).fillna(0).astype(int)
    return out


def merge_sent_raises(new: pd.DataFrame, old: pd.DataFrame) -> pd.DataFrame:
    out = new.copy()
    prior = old.set_index("id")["sent"] if not old.empty and "sent" in old else pd.Series(dtype=int)
    out["sent"] = out["id"].map(prior).fillna(0).astype(int)
    return out


def summary(trades: pd.DataFrame, equity: pd.DataFrame, marks: Mapping[str, float] | None = None):
    closed = trades[trades["status"] == "closed"] if not trades.empty else trades
    wins = int((closed["pnl"] > 0).sum()) if len(closed) else 0
    realized = float(closed["pnl"].sum()) if len(closed) else 0.0
    last = float(equity["equity"].iloc[-1]) if len(equity) else START_EQUITY
    spy_pct = None
    if len(equity) >= 2:
        spy_pct = float(equity["spy"].iloc[-1] / equity["spy"].iloc[0] - 1)
    return {
        "closed": len(closed),
        "wins": wins,
        "win_rate": wins / len(closed) if len(closed) else None,
        "win_rate_lower": wilson_lower_bound(wins, len(closed)) if len(closed) else None,
        "open": int((trades["status"] == "open").sum()) if not trades.empty else 0,
        "total_pnl": realized,
        "avg_return": float((closed["pnl"] / (closed["entry_price"] * closed["shares"])).mean())
        if len(closed)
        else None,
        "equity": last,
        "profit": last - START_EQUITY,
        "return_pct": last / START_EQUITY - 1,
        "spy_pct": spy_pct,
        "since": equity["day"].iloc[0] if len(equity) else None,
        "marks": dict(marks or {}),
        "stops_raised": int(trades["stops_raised"].sum()) if not trades.empty else 0,
    }


def money(x: float, sign: bool = False) -> str:
    s = f"{abs(x):,.0f}"
    if sign:
        return ("+" if x >= 0 else "-") + "$" + s
    return ("-" if x < 0 else "") + "$" + s


def verdict_line(s: Mapping) -> str:
    if not s["closed"] and not s["open"]:
        return "No pretend trades yet, so there is nothing to judge."
    if s["closed"] < GATE_TRADES:
        return (
            f"It is far too early to judge: only {s['closed']} pretend trade"
            f"{'s' if s['closed'] != 1 else ''} finished so far, and we want at least "
            f"{GATE_TRADES} before the results mean anything. Keep collecting."
        )
    lower = s["win_rate_lower"]
    return (
        f"{s['closed']} pretend trades finished; the win rate is {s['win_rate']:.0%} and we are "
        f"95% sure it is at least {lower:.0%}. The real bot needs at least 60% before it trades."
    )


# ------------------------------------------------------------------ messages (plain English)

_PRACTICE = "Practice only: no real order was placed and no real money is involved."


def _record(s: Mapping) -> str:
    n = s["closed"]
    return (
        f"Practice profit so far: {money(s['profit'], True)} "
        f"({n} finished pretend trade{'s' if n != 1 else ''}, {s['open']} still open)."
    )


def buy_message(t: Mapping, s: Mapping) -> tuple[str, str]:
    cost = t["entry_price"] * t["shares"]
    other = t["votes_other"] if isinstance(t["votes_other"], str) else ""
    items = [
        f"The council pretend-bought {int(t['shares'])} shares of {t['symbol']} at "
        f"${t['entry_price']:,.2f} (about {money(cost)} of pretend money)",
        f"Voted for it: {t['votes_for']}",
    ]
    if other:
        items.append(f"Not convinced: {other}")
    items += [
        f"The pretend stop-loss starts at ${t['initial_stop']:,.2f} and moves up as the "
        "price rises",
        f"It is sold if the price falls to the stop, after {HOLD_SESSIONS} trading days, "
        "or sooner if the council turns against it",
        _record(s).rstrip("."),
    ]
    return f"Practice buy: {t['symbol']} (Council of Ricks)", bullets(items) + "\n" + _PRACTICE


def raise_message(r: Mapping, s: Mapping) -> tuple[str, str]:
    locked = (r["new_stop"] - r["entry_price"]) * r["shares"]
    effect = (
        f"If it is hit, the trade still makes at least about {money(locked)}"
        if locked > 0
        else f"If it is hit, the loss is limited to about {money(-locked)}"
    )
    items = [
        f"The council raised the pretend stop-loss on {r['symbol']} from ${r['old_stop']:,.2f} "
        f"to ${r['new_stop']:,.2f}",
        f"Why: the price rose, with its best close so far at ${r['highest_close']:,.2f}, and "
        "the stop follows two normal daily swings below that",
        "A stop only ever moves up",
        f"{effect} ({int(r['shares'])} shares, bought at ${r['entry_price']:,.2f})",
    ]
    return f"Practice stop raised: {r['symbol']} (Council of Ricks)", bullets(items) + (
        "\n" + _PRACTICE
    )


def _why_sold(t: Mapping) -> str:
    reason = t["exit_reason"]
    if reason == "stop":
        return "the price fell to the pretend stop-loss"
    if reason == "time":
        return f"it reached the {HOLD_SESSIONS}-day time limit"
    why = t.get("exit_note")
    note = f" ({why})" if isinstance(why, str) and why else ""
    return f"the council turned against it{note}"


def sell_message(t: Mapping, s: Mapping) -> tuple[str, str]:
    proceeds = t["exit_price"] * t["shares"]
    pct = t["exit_price"] / t["entry_price"] - 1
    word = "made" if t["pnl"] >= 0 else "lost"
    items = [
        f"The council pretend-sold {int(t['shares'])} shares of {t['symbol']} at "
        f"${t['exit_price']:,.2f} (about {money(proceeds)})",
        f"Why: {_why_sold(t)}",
        f"On this trade it {word} {money(abs(t['pnl']))} ({pct:+.1%}); bought at "
        f"${t['entry_price']:,.2f}",
        _record(s).rstrip("."),
    ]
    return f"Practice sell: {t['symbol']} (Council of Ricks)", bullets(items) + "\n" + _PRACTICE


def upgrade_message(previous: str | None, current: str = RULES_VERSION) -> tuple[str, str] | None:
    """The standalone 'council upgraded' post; None on the first run or when nothing changed."""
    if previous is None or previous == current:
        return None
    what = RULES_CHANGES.get(current, "See the council change log for details.")
    text = "\n".join(
        [
            f"The council's rules changed from {previous} to {current}.",
            what,
            "Pretend trades already open keep the rules they started with; new ones use "
            f"{current} and are counted separately.",
            _PRACTICE,
        ]
    )
    return f"Council of Ricks upgraded to {current}", text


def daily_message(day: pd.Timestamp, trades: pd.DataFrame, s: Mapping) -> tuple[str, str]:
    head = [
        f"The council's practice account is worth {money(s['equity'])} "
        f"({money(s['profit'], True)}, {s['return_pct']:+.1%} since {s['since']:%d %b})"
    ]
    if s["spy_pct"] is not None:
        head.append(f"The overall market (SPY) moved {s['spy_pct']:+.1%} in the same time")
    blocks = [bullets(head)]
    held = trades[trades["status"] == "open"] if not trades.empty else trades
    if len(held):
        rows = []
        for t in held.to_dict("records"):
            now = s["marks"].get(t["symbol"], t["entry_price"])
            gain = (now - t["entry_price"]) * t["shares"]
            rows.append(
                [
                    t["symbol"],
                    str(int(t["shares"])),
                    f"${t['entry_price']:,.2f}",
                    f"${now:,.2f}",
                    money(gain, True),
                    f"${t['stop']:,.2f}",
                ]
            )
        blocks.append(
            section(
                "Pretend holdings",
                table(["Stock", "Shares", "Bought at", "Now", "Gain/loss", "Stop-loss"], rows),
            )
        )
    else:
        blocks.append("It is not holding anything right now.")
    blocks += [bullets([verdict_line(s)]), _PRACTICE]
    return f"Council practice account, {day:%a %d %b}", "\n\n".join(b for b in blocks if b)


# ------------------------------------------------------------------ one message per event


def send_all(
    trades: pd.DataFrame,
    raises: pd.DataFrame,
    s: Mapping,
    send: Callable[[str, str], None],
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """One message per new buy, stop raise and sell (in that order for each trade).

    A flag is set only when the send worked; ``send`` may raise (the next run retries).
    Never raises itself and never repeats an event whose flag is set.
    """
    out, rout, sent = trades.copy(), raises.copy(), 0

    def tell(build: Callable, row: Mapping) -> bool:
        try:
            send(*build(row, s))
        except Exception:
            return False
        return True

    for i, t in out.iterrows():
        if not int(t["buy_sent"]) and tell(buy_message, t.to_dict()):
            out.at[i, "buy_sent"] = 1
            sent += 1
        for j, r in rout[rout["trade_id"] == t["id"]].iterrows():
            if not int(r["sent"]) and tell(raise_message, r.to_dict()):
                rout.at[j, "sent"] = 1
                sent += 1
        if t["status"] == "closed" and not int(t["sell_sent"]) and tell(sell_message, t.to_dict()):
            out.at[i, "sell_sent"] = 1
            sent += 1
    return out, rout, sent


def send_events(
    trades: pd.DataFrame, s: Mapping, send: Callable[[str, str], None]
) -> tuple[pd.DataFrame, int]:
    """Buys and sells only (no stop raises); see ``send_all``."""
    out, _, sent = send_all(trades, pd.DataFrame(columns=RAISE_COLUMNS), s, send)
    return out, sent
