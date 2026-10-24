# Author: Kai0421
"""Stocktwits sentiment shadow log (bot.sentiment): research only, never orders."""

import ast
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from bot import sentiment as st

SRC = Path(__file__).parents[1] / "src" / "bot"
NOW = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)


def post(i, sent=None, user=1, symbols=("NVDA",), at="2026-10-12T13:50:00Z", likes=0, body="x"):
    p = {"id": i, "body": body, "created_at": at, "symbols": list(symbols), "user": {"id": user}}
    if sent:
        p["sentiment"] = sent
    if likes:
        p["likes"] = likes
    return p


def pulse(symbol="NVDA", score=39, vol=51, posts=(), price=229.28):
    return {
        "symbol": symbol,
        "price": price,
        "price_time": "2026-10-09T16:00:00-04:00",
        "sentiment": {
            "score": score,
            "label": "BEARISH",
            "bull_pct": 72.3,
            "bear_pct": 27.7,
            "delta": -6.3,
        },
        "message_volume": {"label": "NORMAL", "score": vol},
        "posts": list(posts),
    }


# ----------------------------------------------------------------- parsing
def test_snapshot_from_pulse_reads_the_fields():
    s = st.snapshot_from_pulse(pulse(posts=[post(1, "Bullish")]), NOW, held=True)
    assert (s.symbol, s.score, s.volume_score, s.price, s.held) == ("NVDA", 39, 51, 229.28, True)
    assert s.bull_pct == 72.3 and s.bear_pct == 27.7 and s.delta == -6.3
    assert s.ts == "2026-10-12T14:00:00Z"
    assert s.post_net == 1.0 and s.post_tagged == 1


def test_snapshot_tolerates_missing_optional_fields():
    s = st.snapshot_from_pulse({"symbol": "AMD", "sentiment": {"score": 50}}, NOW)
    assert s.volume_score is None and s.price is None and s.post_net is None


@pytest.mark.parametrize("bad", [{}, {"symbol": "AMD"}, {"symbol": "AMD", "sentiment": {}}])
def test_snapshot_rejects_payloads_without_a_score(bad):
    with pytest.raises(ValueError):
        st.snapshot_from_pulse(bad, NOW)


# ------------------------------------------------------------ post scoring
def test_post_net_counts_only_tagged_recent_single_stock_posts():
    posts = [
        post(1, "Bullish", user=1),
        post(2, "Bearish", user=2),
        post(3, "Bullish", user=3),
        post(4, None, user=4),  # untagged: not counted
        post(5, "Bullish", user=5, at="2026-10-12T09:00:00Z"),  # outside the window
        post(6, "Bullish", user=6, symbols=("A", "B", "C", "D", "NVDA")),  # ticker spam
    ]
    net, tagged = st.score_posts(posts, "NVDA", NOW, window_minutes=60)
    assert tagged == 3
    assert net == pytest.approx((1 - 1 + 1) / 3)


def test_one_vote_per_user_and_duplicates_dropped():
    posts = [post(i, "Bullish", user=7, body="same") for i in range(5)] + [post(9, "Bearish", 8)]
    net, tagged = st.score_posts(posts, "NVDA", NOW)
    assert tagged == 2 and net == pytest.approx(0.0)


def test_engagement_adds_weight_but_never_more_than_a_cap():
    big = post(1, "Bullish", user=1, likes=10_000)
    small = post(2, "Bearish", user=2)
    net, _ = st.score_posts([big, small], "NVDA", NOW)
    assert 0 < net < 1
    assert st.post_weight(big) <= st.MAX_POST_WEIGHT


def test_no_usable_posts_gives_none():
    assert st.score_posts([post(1, None)], "NVDA", NOW) == (None, 0)
    assert st.score_posts([], "NVDA", NOW) == (None, 0)


# ------------------------------------------------------------- classifying
def snap(score, vol=70, held=False, tagged=5, net=0.5):
    return st.Snapshot(
        ts="2026-10-12T14:00:00Z",
        symbol="NVDA",
        price=100.0,
        score=score,
        label="",
        bull_pct=0.0,
        bear_pct=0.0,
        delta=0.0,
        volume_score=vol,
        post_net=net,
        post_tagged=tagged,
        held=held,
    )


def test_buy_watch_needs_strong_sentiment_volume_and_enough_posts():
    t = st.Thresholds()
    assert st.classify(snap(80), t) == "buy_watch"
    assert st.classify(snap(60), t) is None
    assert st.classify(snap(80, vol=40), t) is None  # nobody is talking
    assert st.classify(snap(80, tagged=1), t) is None  # too few tagged posts


def test_sell_warning_only_for_held_stocks():
    t = st.Thresholds()
    assert st.classify(snap(20, held=True, net=-0.5), t) == "sell_warning"
    assert st.classify(snap(20, held=False, net=-0.5), t) is None
    assert st.classify(snap(45, held=True), t) is None


def test_sell_warning_needs_the_posts_to_agree():
    t = st.Thresholds()
    assert st.classify(snap(20, held=True, net=0.4), t) is None


def test_a_held_stock_with_great_sentiment_is_not_a_buy_watch():
    assert st.classify(snap(90, held=True), st.Thresholds()) is None


# ---------------------------------------------------------------- watchlist
def test_watchlist_adds_buy_watch_and_expires_after_five_trading_days():
    wl = st.update_watchlist({}, [snap(80)], today=datetime(2026, 10, 12).date(), held=set())
    assert "NVDA" in wl
    later = datetime(2026, 10, 19).date()  # 5 business days on
    assert st.update_watchlist(wl, [snap(80, vol=40)], today=later, held=set()) == {}


def test_watchlist_forgets_after_two_neutral_days_in_a_row():
    d = datetime(2026, 10, 12).date()
    wl = st.update_watchlist({}, [snap(80)], today=d, held=set())
    wl = st.update_watchlist(
        wl, [snap(50, vol=40)], today=datetime(2026, 10, 13).date(), held=set()
    )
    assert "NVDA" in wl
    wl = st.update_watchlist(
        wl, [snap(52, vol=40)], today=datetime(2026, 10, 14).date(), held=set()
    )
    assert wl == {}


def test_a_neutral_day_resets_when_sentiment_returns():
    d = datetime(2026, 10, 12).date()
    wl = st.update_watchlist({}, [snap(80)], today=d, held=set())
    wl = st.update_watchlist(
        wl, [snap(50, vol=40)], today=datetime(2026, 10, 13).date(), held=set()
    )
    wl = st.update_watchlist(wl, [snap(80)], today=datetime(2026, 10, 14).date(), held=set())
    assert wl["NVDA"]["neutral_days"] == 0


def test_held_stocks_stay_on_the_watchlist_until_sold():
    d = datetime(2026, 10, 12).date()
    wl = st.update_watchlist({}, [snap(20, held=True, net=-0.5)], today=d, held={"NVDA"})
    wl = st.update_watchlist(
        wl, [snap(50, vol=40, held=True)], today=datetime(2026, 10, 30).date(), held={"NVDA"}
    )
    assert "NVDA" in wl
    wl = st.update_watchlist(wl, [snap(50, vol=40)], today=datetime(2026, 11, 2).date(), held=set())
    assert wl == {}


# --------------------------------------------------------------- file store
def test_snapshots_round_trip_through_jsonl(tmp_path):
    f = tmp_path / "snaps.jsonl"
    a = st.snapshot_from_pulse(pulse("NVDA"), NOW)
    b = st.snapshot_from_pulse(pulse("AMD", score=70), NOW, held=True)
    st.append_snapshots(f, [a])
    st.append_snapshots(f, [b])
    assert st.read_snapshots(f) == [a, b]
    assert len(f.read_text().splitlines()) == 2


def test_read_skips_a_torn_last_line(tmp_path):
    f = tmp_path / "snaps.jsonl"
    st.append_snapshots(f, [st.snapshot_from_pulse(pulse(), NOW)])
    with f.open("a") as fh:
        fh.write('{"ts": "2026')
    assert len(st.read_snapshots(f)) == 1


def test_read_missing_file_is_empty(tmp_path):
    assert st.read_snapshots(tmp_path / "none.jsonl") == []


# --------------------------------------------------------------------- ingest
def test_ingest_stores_snapshots_updates_watchlist_and_reports_alerts(tmp_path):
    payloads = [
        pulse("NVDA", score=85, vol=80, posts=[post(i, "Bullish", user=i) for i in range(5)]),
        pulse(
            "AMD",
            score=15,
            vol=80,
            posts=[post(i, "Bearish", user=i, symbols=("AMD",)) for i in range(5)],
        ),
    ]
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(payloads))
    alerts = st.ingest(raw, tmp_path, held={"AMD"}, now=NOW)
    assert {(a.symbol, a.kind) for a in alerts} == {("NVDA", "buy_watch"), ("AMD", "sell_warning")}
    assert len(st.read_snapshots(tmp_path / "snapshots.jsonl")) == 2
    wl = json.loads((tmp_path / "watchlist.json").read_text())
    assert set(wl) == {"NVDA", "AMD"}


def test_ingest_skips_a_bad_payload_but_keeps_the_rest(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps([{"symbol": "BAD"}, pulse("NVDA")]))
    st.ingest(raw, tmp_path, held=set(), now=NOW)
    assert [s.symbol for s in st.read_snapshots(tmp_path / "snapshots.jsonl")] == ["NVDA"]


# ------------------------------------------------------------------- guard
def test_only_the_data_decision_and_engine_layers_import_sentiment():
    allowed = {
        "gather.py",
        "decide.py",
        "engine.py",
        "ricks.py",
        "ricks_shadow.py",
    }  # read, judge, wire, vote
    for path in SRC.rglob("*.py"):
        if path.name == "sentiment.py" or path.name in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            assert not any(m.startswith("bot.sentiment") for m in mods), path


def test_the_decision_layer_uses_only_the_pure_sentiment_functions():
    imported = set()
    for node in ast.walk(ast.parse((SRC / "decide.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.sentiment":
            imported |= {a.name for a in node.names}
    assert imported <= {"DEFAULT_THRESHOLDS", "Snapshot", "classify"}


def test_sentiment_module_never_touches_the_broker():
    src = (SRC / "sentiment.py").read_text(encoding="utf-8")
    for banned in ("bot.broker", "bot.execute", "alpaca", "submit_", "sell_market", "sell_at_open"):
        assert banned not in src


# ------------------------------------------------- light payloads (two cheap calls)
def test_snapshot_reads_the_get_sentiment_and_get_message_volume_shapes():
    light = {
        "symbol": "AMD",
        "sentiment": {
            "bearish_pct": 26.25,
            "bullish_delta": 4.66,
            "bullish_pct": 73.75,
            "label": "NEUTRAL",
            "score": 47,
            "symbol": "AMD",
        },
        "message_volume": {
            "series": [
                {"timeframe": "now", "normalized_value": 43},
                {"timeframe": "1D", "normalized_value": 44},
            ]
        },
    }
    s = st.snapshot_from_pulse(light, NOW)
    assert (s.score, s.volume_score, s.bull_pct, s.bear_pct, s.delta) == (
        47,
        43,
        73.75,
        26.25,
        4.66,
    )
    assert s.price is None and s.post_net is None and s.post_tagged == 0


# ----------------------------------------------------------- freshness (pure)
def test_latest_fresh_keeps_the_newest_snapshot_per_symbol_within_the_age_limit():
    def mk(sym, ts, score):
        return st.Snapshot(ts=ts, symbol=sym, price=None, score=score)

    snaps = [
        mk("NVDA", "2026-10-12T13:00:00Z", 20),
        mk("NVDA", "2026-10-12T13:40:00Z", 25),  # newest NVDA
        mk("AMD", "2026-10-12T10:00:00Z", 20),  # too old (240 min)
        mk("AAPL", "2026-10-12T13:50:00Z", 70),  # not asked for
    ]
    out = st.latest_fresh(snaps, {"NVDA", "AMD"}, NOW, max_age_minutes=90)
    assert set(out) == {"NVDA"} and out["NVDA"].score == 25


def test_latest_fresh_ignores_snapshots_from_the_future_and_bad_timestamps():
    bad = [
        st.Snapshot(ts="2026-10-12T15:00:00Z", symbol="NVDA", price=None, score=20),
        st.Snapshot(ts="garbage", symbol="NVDA", price=None, score=20),
    ]
    assert st.latest_fresh(bad, {"NVDA"}, NOW, 90) == {}


# ------------------------------------------------------- when the collector is due


def et(h, m=0, day=date(2026, 10, 12)):  # Mon 12 Oct 2026, EDT
    return datetime(day.year, day.month, day.day, h, m, tzinfo=st.ET)


def test_nothing_is_due_on_a_day_without_a_session():
    assert st.due_mode(et(16, 0), session=False, held={"AMD"}, state={}) == "none"


def test_a_full_run_is_due_once_after_1530_et_on_a_session_day():
    assert st.due_mode(et(15, 30), True, set(), {}) == "full"
    done = {"last_full_day": "2026-10-12"}
    assert st.due_mode(et(15, 45), True, set(), done) == "none"
    assert st.due_mode(et(15, 45), True, set(), {"last_full_day": "2026-10-09"}) == "full"


def test_before_1530_only_held_stocks_are_checked_and_not_when_nothing_is_held():
    assert st.due_mode(et(11, 0), True, set(), {}) == "none"
    assert st.due_mode(et(11, 0), True, {"AMD"}, {}) == "held"


def test_held_checks_run_about_every_30_minutes_between_1000_and_1530():
    recent = {"last_held_ts": "2026-10-12T15:00:00Z"}  # 11:00 ET
    assert st.due_mode(et(11, 20), True, {"AMD"}, recent) == "none"
    assert st.due_mode(et(11, 30), True, {"AMD"}, recent) == "held"
    assert st.due_mode(et(9, 45), True, {"AMD"}, {}) == "none"


def test_a_full_run_wins_when_both_are_due():
    assert (
        st.due_mode(et(15, 30), True, {"AMD"}, {"last_held_ts": "2026-10-12T10:00:00Z"}) == "full"
    )


def test_a_garbled_state_file_is_treated_as_never_run():
    assert st.due_mode(et(11, 0), True, {"AMD"}, {"last_held_ts": "garbage"}) == "held"


def test_state_round_trips_and_a_bad_file_reads_empty(tmp_path):
    f = tmp_path / "state.json"
    assert st.read_state(f) == {}
    st.write_state(f, {"last_full_day": "2026-10-12"})
    assert st.read_state(f) == {"last_full_day": "2026-10-12"}
    f.write_text("{not json")
    assert st.read_state(f) == {}


def test_ingest_records_the_mode_it_ran_in(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps([pulse("NVDA")]))
    st.ingest(raw, tmp_path, held=set(), now=NOW, mode="full")
    s = st.read_state(tmp_path / "state.json")
    assert s["last_full_day"] == "2026-10-12" and s["last_held_ts"] == "2026-10-12T14:00:00Z"
    st.ingest(raw, tmp_path, held=set(), now=datetime(2026, 10, 12, 15, 0, tzinfo=UTC), mode="held")
    s = st.read_state(tmp_path / "state.json")
    assert s["last_full_day"] == "2026-10-12" and s["last_held_ts"] == "2026-10-12T15:00:00Z"


def test_ingest_without_a_mode_leaves_the_state_alone(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps([pulse("NVDA")]))
    st.ingest(raw, tmp_path, held=set(), now=NOW)
    assert not (tmp_path / "state.json").exists()
