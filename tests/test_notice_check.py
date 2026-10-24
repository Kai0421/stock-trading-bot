# Author: Kai0421
from types import SimpleNamespace as N

from bot import notice_check as nc
from bot.notify import Dispatcher, Level


def test_clean_messages_pass():
    assert (
        nc.lint("Morning plan", "Planned buys: GILD (71% win chance)\nHolding 3 positions.") == []
    )


def test_placeholders_raw_codes_and_nonsense_numbers_are_caught():
    cases = {
        "Position None sold": "None",
        "P/L is nan today": "nan",
        "Bought {symbol} at $10": "unfilled",
        "sold because time_stop": "raw",
        "Traceback (most recent call last)": "error",
        "GILD (171% win chance)": "percent",
        "bought -3 shares of GILD": "negative",
    }
    for text in cases:
        assert nc.lint("t", text), text


def test_the_word_none_inside_a_normal_sentence_is_not_a_false_alarm():
    assert nc.lint("t", "Planned sales: none today. Nothing to buy.") == []
    assert nc.lint("t", "Anyone? None of the setups passed.") == ["the word None appears"]


def test_plan_check_catches_too_many_buys_held_symbols_and_full_sectors():
    plans = [
        N(symbol="A", sector="Tech"),
        N(symbol="B", sector="Tech"),
        N(symbol="C", sector="Energy"),
    ]
    held = [N(symbol="X", sector="Tech"), N(symbol="C", sector="Energy")]
    problems = nc.check_plan(plans, held, max_positions=4, max_per_sector=2)
    text = " ".join(problems)
    assert "3 buys planned but only 2 slot" in text
    assert "already holds C" in text
    assert "Tech" in text and "limit of 2" in text
    assert nc.check_plan([N(symbol="A", sector="Tech")], [], 6, 2) == []


def test_sale_check_catches_selling_more_than_held():
    held = [N(symbol="TMO", shares=26, pending_exit=("time_stop", 30))]
    assert "TMO" in " ".join(nc.check_sales(held))
    ok = [
        N(symbol="TMO", shares=26, pending_exit=("time_stop", 26)),
        N(symbol="L", shares=1, pending_exit=None),
    ]
    assert nc.check_sales(ok) == []


class Rec:
    def __init__(self, name, sent):
        self.name, self.sent = name, sent

    def send(self, level, title, text, files=(), thread=None):
        self.sent.append((self.name, level, title, text))


def test_dispatcher_holds_back_a_flawed_info_message_and_tells_general():
    sent = []
    d = Dispatcher(
        slack=[Rec("bot", sent)], fallback=[], routes={"general": [Rec("general", sent)]}
    )
    out = d.notify(Level.INFO, "Morning plan", "P/L is nan")
    assert out.ok is False
    assert [s[0] for s in sent] == ["general"]
    assert "held back" in sent[0][2].lower() and "Morning plan" in sent[0][3]


def test_dispatcher_sends_a_flawed_warning_but_flags_it():
    sent = []
    d = Dispatcher(slack=[Rec("bot", sent)], fallback=[])
    d.notify(Level.CRITICAL, "No stop", "BLK has no stop. P/L nan")
    assert len(sent) == 1 and "automatic check" in sent[0][3] and "BLK has no stop" in sent[0][3]


def test_dispatcher_can_be_told_not_to_check():
    sent = []
    d = Dispatcher(slack=[Rec("bot", sent)], fallback=[], check=False)
    d.notify(Level.INFO, "t", "P/L is nan")
    assert len(sent) == 1 and "automatic check" not in sent[0][3]
