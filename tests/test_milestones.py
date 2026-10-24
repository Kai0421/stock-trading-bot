# Author: Kai0421
from bot import milestones as ms


def test_a_system_is_due_once_when_it_reaches_the_bar():
    assert ms.due({"bot": 12, "council": 30}, {}) == ["council"]
    assert ms.due({"bot": 31, "council": 40}, {}) == ["bot", "council"]
    assert ms.due({"bot": 31, "council": 40}, {"bot": 30, "council": 30}) == []  # told already


def test_unreadable_counts_are_skipped_not_fatal():
    assert ms.due({"bot": None, "council": 5}, {}) == []


def test_messages_are_plain_english_and_name_the_channel_owner():
    title, text = ms.message("council", 31, "A line about results.")
    assert "council" in title.lower() and "30" in title
    assert "31 finished" in text and "A line about results." in text
    assert "charts" in text.lower() and "no real order" in text.lower()
    title, text = ms.message("bot", 30, "Too early to say")
    assert "trading bot" in title.lower() and "30 finished" in text
