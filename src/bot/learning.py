# Author: Kai0421
"""The learning loop: learn from finished trades, in shadow. Nothing here can place an order.

Pure functions over DataFrames (no broker, no network, no files; the runner does the I/O). Nothing
in the order path (engine, decide, execute, gather, config) imports this module, so it cannot
change a decision, an order or a stop.

What it does, in plain terms:

1. **Post-mortem** of every finished trade (live and council practice): how it ended, how far it
   went for and against us, whether the price came back after a stop, and the context it was
   bought in (market trend, volatility, opening gap, warning flags, which Ricks voted for it).
2. **Pattern miner**: looks for situations where losses cluster. Fitted on the earlier 60% of the
   trades and checked on the later 40% it never saw, with a multiple-testing guard
   (Benjamini-Hochberg) so a lucky cluster is not called a lesson. Few trades -> "too early".
3. **Challengers**: a candidate tweak ("skip this sector") run beside the current rules on the
   same trades, judged only on the later trades, with a pre-set minimum and margin.
4. **Safety asymmetry**: the loop may only recommend making things *more careful*, and only in
   shadow reports. Anything that loosens, adds risk, or touches the gate floor, stops or sizing
   is a proposal for the owner's reviewed commit (and a memory/decisions.md entry), never applied.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from bot.indicators import atr as atr_series
from bot.indicators import sma
from bot.risk import mean_r_lower_bound, wilson_lower_bound

# ----------------------------------------------------------------------------- settings
RECOVER_DAYS = 5  # sessions after a stop in which "the price came back" is judged
MIN_TOTAL = 30  # finished trades before the miner looks at all
TRAIN_FRAC = 0.6  # fit on the earlier part, confirm on the later part
MIN_TRAIN_CLUSTER = 8
MIN_TEST_CLUSTER = 5
ALPHA = 0.10  # false-discovery rate allowed across everything tried
STRONG_Q = 0.05
CONFIRM_P = 0.10  # the later trades must point the same way at least this clearly
MIN_SKIPPED = 20  # a challenger must have skipped this many later trades to be judged
MARGIN_R = 0.05  # and improve the average by at least this much (in times the amount risked)
MIN_KEPT = 30
MIN_KEPT_SHARE = 0.5  # it may not starve the sample
GATE_TRADES = 100  # the bot's own rule: 100+ trades before a result means anything
Z95 = 1.96

DIM_LABELS = {
    "sector": "Sector",
    "regime": "Market trend",
    "weekday": "Day bought",
    "atr_bucket": "Volatility",
    "gap_bucket": "Opening gap",
    "signature": "Setup type",
    "source": "Account",
    "exit_kind": "How it ended",
}
DEFAULT_DIMS = ("sector", "regime", "weekday", "atr_bucket", "gap_bucket", "signature")

COLUMNS = [
    "id", "source", "symbol", "sector", "signature", "entry_date", "exit_date", "shares",
    "pnl", "r", "win", "exit_reason", "exit_kind", "holding_days", "mae_r", "mfe_r",
    "r_captured", "recovered_after_exit", "regime", "gap_pct", "atr_pct", "risk_flags",
    "votes_for", "lesson",
]  # fmt: skip
COMMON = [
    "id", "source", "symbol", "sector", "signature", "entry_date", "exit_date", "entry_price",
    "exit_price", "shares", "pnl", "r", "exit_reason", "holding_days", "votes_for",
    "initial_stop", "atr", "close", "flags",
]  # fmt: skip


# ----------------------------------------------------------------------------- small maths


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exact and stable for large n (log space)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    logs = [
        math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
        for i in range(k, n + 1)
    ]
    m = max(logs)
    return min(1.0, math.exp(m) * sum(math.exp(x - m) for x in logs))


def bh_adjust(pvals: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values (q-values), in the input order."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvals[i] * m / rank)
        q[i] = running
    return q


def _clip(p: float) -> float:
    return min(0.99, max(0.01, p))


# ----------------------------------------------------------------------------- exits and risk


def exit_kind(reason: str) -> str:
    r = (reason or "").strip().lower()
    if not r:
        return "other"
    if r in ("stop", "trail") or "stop" in r:
        return "stop"
    if "trend" in r:
        return "trend_break"
    if "profit" in r or "target" in r:
        return "profit_target"
    if r.startswith("late"):
        return "late"
    if r == "time" or "time" in r:
        return "time_limit"
    if "council" in r:
        return "council_turned"
    return "other"


def _num(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return math.nan
    return v


def risk_per_share(t: Mapping) -> float:
    """Dollars risked per share: entry minus the first stop, else worked back from R."""
    entry, stop = _num(t.get("entry_price")), _num(t.get("initial_stop"))
    if math.isfinite(entry) and math.isfinite(stop) and entry > stop:
        return entry - stop
    exit_p, r = _num(t.get("exit_price")), _num(t.get("r"))
    if math.isfinite(entry) and math.isfinite(exit_p) and math.isfinite(r) and r != 0:
        return abs(exit_p - entry) / abs(r)
    return math.nan


# ----------------------------------------------------------------------------- post-mortems


def _day(x) -> pd.Timestamp:
    return pd.Timestamp(x).normalize()


def _short(x) -> str:
    d = _day(x)
    return f"{d.day} {d:%b}"


def _regime(spy: pd.DataFrame | None, entry: pd.Timestamp) -> str:
    if spy is None or "close" not in spy:
        return "unknown"
    s = spy["close"].loc[: entry - pd.Timedelta(days=1)]
    if len(s) < 200:
        return "unknown"
    s200 = sma(s, 200).iloc[-1]
    if pd.isna(s200):
        return "unknown"
    return "market_up" if s.iloc[-1] > s200 else "market_down"


def _flags_text(flags) -> str:
    if flags is None or (isinstance(flags, float) and math.isnan(flags)):
        return ""
    if isinstance(flags, str):
        return flags
    return "; ".join(str(f) for f in flags)


def post_mortem(
    trade: Mapping,
    bars: pd.DataFrame | None,
    spy: pd.DataFrame | None = None,
    context: Mapping | None = None,
) -> dict:
    """One finished trade, explained. ``bars`` are the stock's daily bars (None = unknown)."""
    ctx = dict(context or {})
    entry, exit_ = _day(trade["entry_date"]), _day(trade["exit_date"])
    entry_p = _num(trade.get("entry_price"))
    pnl, r = _num(trade.get("pnl")), _num(trade.get("r"))
    risk = risk_per_share(trade)
    kind = exit_kind(str(trade.get("exit_reason", "")))
    mae = mfe = gap = atr_pct = math.nan
    recovered: bool | None = None
    if bars is not None and len(bars):
        hold = bars.loc[entry:exit_]
        if len(hold) and math.isfinite(risk) and risk > 0:
            mae = (entry_p - float(hold["low"].min())) / risk
            mfe = (float(hold["high"].max()) - entry_p) / risk
        before = bars.loc[: entry - pd.Timedelta(days=1)]
        if len(before):
            prev_close = float(before["close"].iloc[-1])
            if prev_close > 0:
                gap = entry_p / prev_close - 1
            if len(before) >= 15 and prev_close > 0:
                a = atr_series(before["high"], before["low"], before["close"], 14).iloc[-1]
                if not pd.isna(a):
                    atr_pct = float(a) / prev_close
        if pnl <= 0:
            after = bars.loc[exit_ + pd.Timedelta(days=1) :].head(RECOVER_DAYS)
            if len(after):
                recovered = bool((after["close"] >= entry_p).any())
    c_atr, c_close = _num(ctx.get("atr")), _num(ctx.get("close"))
    if math.isfinite(c_atr) and math.isfinite(c_close) and c_close > 0:
        atr_pct = c_atr / c_close
    captured = r / mfe if (math.isfinite(mfe) and mfe > 0.1 and r > 0) else math.nan
    pm = {
        "id": trade.get("id"), "source": trade.get("source", ""), "symbol": trade["symbol"],
        "sector": trade.get("sector", ""), "signature": trade.get("signature", ""),
        "entry_date": entry, "exit_date": exit_, "shares": trade.get("shares"),
        "pnl": pnl, "r": r, "win": bool(pnl > 0), "exit_reason": trade.get("exit_reason", ""),
        "exit_kind": kind, "holding_days": (exit_ - entry).days, "mae_r": mae, "mfe_r": mfe,
        "r_captured": captured, "recovered_after_exit": recovered,
        "regime": _regime(spy, entry), "gap_pct": gap, "atr_pct": atr_pct,
        "risk_flags": _flags_text(ctx.get("flags", trade.get("flags"))),
        "votes_for": trade.get("votes_for", ""),
    }  # fmt: skip
    pm["lesson"] = lesson_line(pm)
    return pm


def _money(x: float) -> str:
    return f"${abs(x):,.0f}"


def _times(r: float) -> str:
    return f"{abs(r):.2f} times the amount risked" if math.isfinite(r) else "an unknown multiple"


def lesson_line(pm: Mapping) -> str:
    """One plain-English sentence or two about a finished trade."""
    sym, kind = pm["symbol"], pm["exit_kind"]
    pnl, r, shares = pm["pnl"], pm["r"], pm.get("shares")
    when = _short(pm["exit_date"])
    outcome = f"{'a gain' if pnl > 0 else 'a loss'} of {_money(pnl)}"
    sh = f"{int(shares)} shares, " if shares is not None and not pd.isna(shares) else ""
    tail = f" ({sh}{_times(r)})"
    if kind == "stop":
        out = f"{sym}: stopped out on {when} for {outcome}{tail}."
        rec = pm.get("recovered_after_exit")
        if rec is True:
            out += (
                f" The price recovered above the entry price within {RECOVER_DAYS} sessions, "
                "so the stop may have been too tight."
            )
        elif rec is False:
            out += " The price kept falling afterwards, so the stop did its job."
        return out
    if kind == "trend_break":
        out = f"{sym}: sold on {when} when the trend broke, for {outcome}{tail}."
        mfe = pm.get("mfe_r")
        if mfe is not None and math.isfinite(mfe) and mfe > 0.1:
            out += f" It had been up as much as {_times(mfe)} before that."
        return out
    if kind == "profit_target":
        return f"{sym}: sold on {when} after hitting its profit target, for {outcome}{tail}."
    if kind == "late":
        return f"{sym}: sold on {when}, later than planned, for {outcome}{tail}."
    if kind == "time_limit":
        return f"{sym}: sold on {when} at the time limit, for {outcome}{tail}."
    if kind == "council_turned":
        return f"{sym}: sold on {when} because the council turned against it, for {outcome}{tail}."
    return f"{sym}: closed on {when} for {outcome}{tail}."


# ----------------------------------------------------------------------------- sources


def _features(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def normalise_live(
    trades: pd.DataFrame, signals: pd.DataFrame, sectors: Mapping[str, str]
) -> pd.DataFrame:
    """Journal trades joined to the signal that created them (features: atr, close, flags)."""
    rows = []
    sig = signals.copy()
    if len(sig):
        sig["day"] = pd.to_datetime(sig["day"])
    for _, t in trades.iterrows():
        feats: dict = {}
        if len(sig):
            m = sig[(sig["symbol"] == t["symbol"]) & (sig["day"] <= pd.Timestamp(t["entry_date"]))]
            if "taken" in m:
                taken = m[m["taken"] == 1]
                m = taken if len(taken) else m
            if len(m):
                feats = _features(m.sort_values("day").iloc[-1].get("features"))
        rows.append({
            "id": f"live-{t['id']}", "source": "live", "symbol": t["symbol"],
            "sector": sectors.get(t["symbol"], ""), "signature": t.get("signature", ""),
            "entry_date": pd.Timestamp(t["entry_date"]), "exit_date": pd.Timestamp(t["exit_date"]),
            "entry_price": float(t["entry_price"]), "exit_price": float(t["exit_price"]),
            "shares": int(t["shares"]), "pnl": float(t["pnl"]), "r": float(t["r_multiple"]),
            "exit_reason": t["exit_reason"], "holding_days": t.get("holding_days"),
            "votes_for": "", "initial_stop": math.nan, "atr": _num(feats.get("atr")),
            "close": _num(feats.get("close")), "flags": _flags_text(feats.get("flags")),
        })  # fmt: skip
    return pd.DataFrame(rows, columns=COMMON)


def normalise_council(ledger: pd.DataFrame) -> pd.DataFrame:
    """Finished pretend trades from the council practice account."""
    if ledger is None or ledger.empty:
        return pd.DataFrame(columns=COMMON)
    c = ledger[ledger["status"] == "closed"]
    rows = []
    for _, t in c.iterrows():
        reason = str(t["exit_reason"])
        mapped = reason if reason in ("stop", "time") else "council turned"
        rows.append({
            "id": f"council-{t['id']}", "source": "council", "symbol": t["symbol"],
            "sector": t.get("sector", ""), "signature": "",
            "entry_date": pd.Timestamp(t["entry_date"]), "exit_date": pd.Timestamp(t["exit_date"]),
            "entry_price": float(t["entry_price"]), "exit_price": float(t["exit_price"]),
            "shares": int(t["shares"]), "pnl": float(t["pnl"]), "r": float(t["r"]),
            "exit_reason": mapped,
            "holding_days": (pd.Timestamp(t["exit_date"]) - pd.Timestamp(t["entry_date"])).days,
            "votes_for": t.get("votes_for", "") if isinstance(t.get("votes_for"), str) else "",
            "initial_stop": _num(t.get("initial_stop")), "atr": math.nan, "close": math.nan,
            "flags": "",
        })  # fmt: skip
    return pd.DataFrame(rows, columns=COMMON)


def build_postmortems(
    trades: pd.DataFrame,
    bars: Mapping[str, pd.DataFrame],
    spy: pd.DataFrame | None = None,
) -> pd.DataFrame:
    rows = []
    for _, t in trades.iterrows():
        ctx = {"atr": t.get("atr"), "close": t.get("close"), "flags": t.get("flags")}
        rows.append(post_mortem(t.to_dict(), bars.get(t["symbol"]), spy, ctx))
    return pd.DataFrame(rows, columns=COLUMNS)


def add_buckets(df: pd.DataFrame) -> pd.DataFrame:
    """Bucket columns the miner can group by (only fills what is missing)."""
    out = df.copy()
    if "atr_bucket" not in out and "atr_pct" in out:
        a = out["atr_pct"]
        out["atr_bucket"] = np.select(
            [a < 0.015, a < 0.03, a >= 0.03], ["low", "mid", "high"], None
        )
        out.loc[a.isna(), "atr_bucket"] = None
    if "gap_bucket" not in out and "gap_pct" in out:
        g = out["gap_pct"]
        out["gap_bucket"] = np.select(
            [g < -0.005, g > 0.005, g.notna()], ["gap_down", "gap_up", "flat"], None
        )
        out.loc[g.isna(), "gap_bucket"] = None
    if "weekday" not in out and "entry_date" in out:
        out["weekday"] = pd.to_datetime(out["entry_date"]).dt.strftime("%a")
    return out


# ----------------------------------------------------------------------------- stats


@dataclass(frozen=True)
class Stats:
    n: int
    wins: int
    win_rate: float
    wilson_lb: float
    mean_r: float
    sd_r: float
    mean_r_lb: float
    mean_r_ub: float


def stats(d: pd.DataFrame) -> Stats:
    n = len(d)
    if n == 0:
        nan = math.nan
        return Stats(0, 0, nan, nan, nan, nan, nan, nan)
    wins = int(d["win"].astype(bool).sum())
    r = d["r"].astype(float)
    mean, sd = float(r.mean()), float(r.std(ddof=1)) if n > 1 else math.nan
    lb = mean_r_lower_bound(mean, sd, n)
    ub = mean + Z95 * sd / math.sqrt(n) if n > 1 and math.isfinite(sd) else math.nan
    return Stats(n, wins, wins / n, wilson_lower_bound(wins, n), mean, sd, lb, ub)


# ----------------------------------------------------------------------------- the miner


@dataclass(frozen=True)
class Lesson:
    dim: str
    value: str
    n: int
    n_test: int
    loss_rate: float
    rest_loss_rate: float
    mean_r: float
    rest_mean_r: float
    win_low: float
    win_high: float
    p: float
    q: float
    test_p: float
    strength: str  # strong | moderate | weak


@dataclass(frozen=True)
class MineResult:
    status: str  # ok | too_early
    message: str
    n: int
    lessons: list[Lesson] = field(default_factory=list)
    watch: list[Lesson] = field(default_factory=list)


def _split(d: pd.DataFrame, frac: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = d.sort_values("entry_date", kind="stable").reset_index(drop=True)
    cut = int(len(d) * frac)
    return d.iloc[:cut], d.iloc[cut:]


def _rates(d: pd.DataFrame, mask: pd.Series) -> tuple[int, int, int, int]:
    lose = ~d["win"].astype(bool)
    return int(mask.sum()), int(lose[mask].sum()), int((~mask).sum()), int(lose[~mask].sum())


def mine(
    df: pd.DataFrame,
    dims: Sequence[str] = DEFAULT_DIMS,
    train_frac: float = TRAIN_FRAC,
    min_total: int = MIN_TOTAL,
) -> MineResult:
    n = len(df)
    if n < min_total:
        return MineResult(
            "too_early",
            f"Too early: only {n} finished trades. It needs at least {min_total} to look for "
            f"patterns and {GATE_TRADES}+ before trusting one.",
            n,
        )
    d = df.sort_values("entry_date", kind="stable").reset_index(drop=True)
    train, test = _split(d, train_frac)
    tried = []
    for dim in dims:
        if dim not in d:
            continue
        for value in sorted(train[dim].dropna().astype(str).unique()):
            mask = train[dim].astype(str) == value
            n_c, k, n_rest, k_rest = _rates(train, mask)
            if n_c < MIN_TRAIN_CLUSTER or n_rest < MIN_TRAIN_CLUSTER:
                continue
            tried.append((dim, value, binom_sf(k, n_c, _clip(k_rest / n_rest))))
    if not tried:
        return MineResult("ok", "No situation had enough trades to compare yet.", n)
    qs = bh_adjust([t[2] for t in tried])
    lessons, watch = [], []
    for (dim, value, p), q in zip(tried, qs, strict=True):
        if q > ALPHA:
            continue
        full = d[dim].astype(str) == value
        n_all, k_all, n_rest_all, k_rest_all = _rates(d, full)
        tm = test[dim].astype(str) == value
        n_t, k_t, n_rest_t, k_rest_t = _rates(test, tm)
        loss, rest_loss = k_all / n_all, k_rest_all / max(1, n_rest_all)
        mean_c, mean_rest = float(d.loc[full, "r"].mean()), float(d.loc[~full, "r"].mean())
        test_p = math.nan
        confirmed = False
        if n_t >= MIN_TEST_CLUSTER and n_rest_t >= MIN_TEST_CLUSTER:
            test_p = binom_sf(k_t, n_t, _clip(k_rest_t / n_rest_t))
            r_t = float(test.loc[tm, "r"].mean())
            r_rest_t = float(test.loc[~tm, "r"].mean())
            confirmed = (
                (k_t / n_t) > (k_rest_t / n_rest_t) and r_t < r_rest_t and test_p <= CONFIRM_P
            )
        if confirmed and q <= STRONG_Q and test_p <= 0.05:
            strength = "strong"
        elif confirmed:
            strength = "moderate"
        else:
            strength = "weak"
        lo, hi = 1 - _upper(k_all, n_all), 1 - _lower(k_all, n_all)
        item = Lesson(dim, value, n_all, n_t, loss, rest_loss, mean_c, mean_rest, lo, hi, p, q,
                      test_p, strength)  # fmt: skip
        (lessons if confirmed else watch).append(item)
    lessons.sort(key=lambda x: x.q)
    watch.sort(key=lambda x: x.q)
    msg = (
        f"It checked {len(tried)} situations."
        if lessons or watch
        else f"It checked {len(tried)} situations and no loss pattern stood out beyond what "
        "luck produces."
    )
    return MineResult("ok", msg, n, lessons, watch)


def _upper(losses: int, n: int) -> float:
    """Upper bound of the loss rate (95% Wilson), i.e. 1 - the lower bound of the win rate."""
    return 1.0 - wilson_lower_bound(n - losses, n)


def _lower(losses: int, n: int) -> float:
    return wilson_lower_bound(losses, n)


def lesson_text(x: Lesson) -> str:
    label = DIM_LABELS.get(x.dim, x.dim)
    value = str(x.value).replace("_", " ")
    out = (
        f"{label} {value}: {x.n} trades, {x.loss_rate:.0%} lost money, against "
        f"{x.rest_loss_rate:.0%} for all the others. On average {x.mean_r:+.2f} times the amount "
        f"risked per trade, against {x.rest_mean_r:+.2f}."
    )
    if x.strength == "weak" or x.n_test < MIN_TEST_CLUSTER:
        return out + " Not yet confirmed on later trades."
    return out + f" Confirmed on {x.n_test} later trades ({x.strength} evidence)."


# ----------------------------------------------------------------------------- challengers


@dataclass(frozen=True)
class Challenger:
    id: str
    name: str
    kind: str
    keep: Callable[[Mapping], bool]
    dim: str = ""
    value: str = ""


def skip_context(dim: str, value: str, id_: str) -> Challenger:
    label = DIM_LABELS.get(dim, dim).lower()
    return Challenger(
        id_, f"skip trades where {label} is {str(value).replace('_', ' ')}", "skip_context",
        lambda row: str(row.get(dim)) != str(value), dim, str(value),
    )  # fmt: skip


TIGHTEN_KINDS = {"skip_context", "skip_signature", "skip_sector", "require_votes", "pause"}
FORBIDDEN_KINDS = {"lower_gate", "remove_stop", "raise_risk", "leverage", "live_orders"}


def direction_of(kind: str) -> str:
    return "tighten" if kind in TIGHTEN_KINDS else "loosen"


@dataclass(frozen=True)
class CompareResult:
    challenger: Challenger
    champion: Stats
    kept: Stats
    skipped: Stats
    direction: str
    proposable: bool
    reason: str


def compare(df: pd.DataFrame, ch: Challenger, test_frac: float = 1 - TRAIN_FRAC) -> CompareResult:
    """Current rules vs a challenger on the same trades; judged on the later trades only."""
    d = df.sort_values("entry_date", kind="stable").reset_index(drop=True)
    n_test = int(round(len(d) * test_frac))
    test = d.iloc[len(d) - n_test :]
    keep = np.array([bool(ch.keep(row)) for _, row in test.iterrows()], dtype=bool)
    champ, kept, skipped = stats(test), stats(test[keep]), stats(test[~keep])
    direction = direction_of(ch.kind)
    if skipped.n < MIN_SKIPPED:
        reason = (
            f"Not enough later trades to judge: it would have skipped {skipped.n} "
            f"(needs {MIN_SKIPPED})."
        )
        return CompareResult(ch, champ, kept, skipped, direction, False, reason)
    if kept.n < max(MIN_KEPT, MIN_KEPT_SHARE * champ.n):
        reason = f"It would leave only {kept.n} of {champ.n} trades, too few to trust."
        return CompareResult(ch, champ, kept, skipped, direction, False, reason)
    if not (math.isfinite(skipped.mean_r_ub) and skipped.mean_r_ub < 0):
        reason = "The trades it would skip were not clearly losers on the later trades."
        return CompareResult(ch, champ, kept, skipped, direction, False, reason)
    if kept.mean_r - champ.mean_r < MARGIN_R:
        reason = f"The improvement is under the {MARGIN_R:.2f} margin required."
        return CompareResult(ch, champ, kept, skipped, direction, False, reason)
    reason = (
        f"On {champ.n} later trades it would have skipped {skipped.n} that averaged "
        f"{skipped.mean_r:+.2f} times the amount risked, lifting the average from "
        f"{champ.mean_r:+.2f} to {kept.mean_r:+.2f}."
    )
    return CompareResult(ch, champ, kept, skipped, direction, True, reason)


# ----------------------------------------------------------------------------- safety asymmetry


@dataclass(frozen=True)
class Triage:
    action: str  # shadow_pause | owner_proposal | refused | none
    message: str
    applied_to_live: bool = False  # always False: this loop never changes the live bot


_OWNER_ONLY = (
    "Only the owner can change this, in a reviewed commit with an entry in memory/decisions.md."
)


def triage_kind(kind: str) -> Triage:
    if kind in FORBIDDEN_KINDS:
        return Triage("refused", f"Refused. This touches a hard rule. {_OWNER_ONLY}")
    if direction_of(kind) == "tighten":
        return Triage("shadow_pause", "A more careful change: recommended in shadow reports only.")
    return Triage("owner_proposal", f"This loosens or adds risk. {_OWNER_ONLY}")


def triage(res: CompareResult) -> Triage:
    ch = res.challenger
    if ch.kind in FORBIDDEN_KINDS:
        return triage_kind(ch.kind)
    if not res.proposable:
        return Triage("none", res.reason)
    if res.direction == "tighten":
        return Triage(
            "shadow_pause",
            f"Proposal: {ch.name}. {res.reason} Shadow reports will show it as a recommendation; "
            "the live bot is not changed unless you approve a reviewed commit.",
        )
    return Triage(
        "owner_proposal",
        f"Proposal: {ch.name}. {res.reason} This loosens or adds risk, so it is only a proposal. "
        f"{_OWNER_ONLY} It needs a reviewed commit and an entry in memory/decisions.md.",
    )


# ----------------------------------------------------------------------------- report


@dataclass(frozen=True)
class Report:
    status: str
    message: str
    n: int
    live_trades: int
    council_trades: int
    lessons: list[Lesson]
    watch: list[Lesson]
    results: list[CompareResult]
    triage: list[Triage]
    notes: list[str]


def _auto_challengers(lessons: Sequence[Lesson]) -> list[Challenger]:
    out = []
    for x in lessons:
        slug = f"{x.dim}-{str(x.value).lower().replace(' ', '-')}"
        out.append(skip_context(x.dim, x.value, f"c-skip-{slug}"))
    return out


def build_report(
    pm: pd.DataFrame,
    live_trades: int,
    council_trades: int,
    challengers: Sequence[Challenger] | None = None,
    dims: Sequence[str] = DEFAULT_DIMS,
) -> Report:
    d = add_buckets(pm) if len(pm) else pm
    res = mine(d, dims)
    if res.status == "too_early":
        return Report(
            "too_early", res.message, res.n, live_trades, council_trades, [], [], [], [], []
        )
    chs = list(challengers) if challengers is not None else _auto_challengers(res.lessons)
    results = [compare(d, ch) for ch in chs]
    notes = []
    if "exit_kind" in d and "recovered_after_exit" in d:
        stops = d[(d["exit_kind"] == "stop") & (~d["win"].astype(bool))]
        known = stops["recovered_after_exit"].dropna()
        if len(known) >= 5:
            share = float(known.astype(bool).mean())
            notes.append(
                f"Of {len(known)} losing stop-outs, {share:.0%} recovered above the entry price "
                f"within {RECOVER_DAYS} sessions."
            )
    return Report(
        "ok", res.message, res.n, live_trades, council_trades, res.lessons, res.watch,
        results, [triage(r) for r in results], notes,
    )  # fmt: skip


def digest(rep: Report, top: int = 3) -> tuple[str, str]:
    """(title, text) for the weekly Slack post, in plain English. Proposals only."""
    title = "Learning loop: weekly look at past mistakes (proposals only)"
    split = f"{rep.live_trades} from the live bot, {rep.council_trades} from the council practice"
    foot = "Proposals only: no real order was placed and nothing was changed on the live bot."
    if rep.status == "too_early":
        text = (
            f"The learning loop has looked at {rep.n} finished trades ({split}). That is far too "
            f"early to find a reliable pattern: it needs at least {MIN_TOTAL} to look and "
            f"{GATE_TRADES}+ before anything counts. Nothing to change. Keep collecting.\n\n{foot}"
        )
        return title, text
    lines = [f"It looked at {rep.n} finished trades ({split}). {rep.message}", ""]
    if rep.lessons:
        lines.append("Situations that keep losing (confirmed on later trades):")
        lines += [f"• {lesson_text(x)}" for x in rep.lessons[:top]]
    else:
        lines.append("No confirmed losing pattern yet.")
    if rep.watch:
        lines += ["", "Worth watching, not confirmed yet:"]
        lines += [f"• {lesson_text(x)}" for x in rep.watch[:top]]
    lines += ["", *[f"• Note: {n}" for n in rep.notes]] if rep.notes else []
    acts = [t for t in rep.triage if t.action in ("shadow_pause", "owner_proposal", "refused")]
    lines.append("")
    if acts:
        lines.append("Proposals for you to decide:")
        lines += [f"• {t.message}" for t in acts]
    else:
        lines.append("Nothing to change this week.")
    lines += ["", foot]
    return title, "\n".join(lines)


def changelog_entry(id_: str, title: str, date: str, approved: str) -> str:
    """A change-log entry in the same shape as docs/22-council-changelog.md."""
    return (
        f"### {id_} ({date}, proposed)\n"
        f"- What: {title}\n"
        "- Why: see the weekly learning-loop report for the evidence and sample sizes.\n"
        f"- Approved by: {approved}"
    )
