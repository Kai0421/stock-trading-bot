# Author: Kai0421
from bot import slack_format as sf


def test_bullets_put_each_item_on_its_own_line():
    assert sf.bullets(["one", "two", "three"]) == "• one\n• two\n• three"
    assert sf.bullets([]) == ""
    assert sf.bullets(["a", "", "  ", "b"]) == "• a\n• b"  # blanks are dropped


def test_table_is_an_aligned_code_block_with_a_header_rule():
    out = sf.table(["Stock", "Shares", "P/L"], [["BLK", "17", "-$194"], ["TMO", "26", "+$12"]])
    lines = out.strip("`\n").splitlines()
    assert out.startswith("```\n") and out.endswith("\n```")
    assert lines[0].split() == ["Stock", "Shares", "P/L"]
    assert set(lines[1]) == {"-"}
    assert lines[2].startswith("BLK") and lines[3].startswith("TMO")
    # every row lines up with the header columns
    col = lines[0].index("Shares")
    assert lines[2].index("17") == col and lines[3].index("26") == col


def test_table_with_no_rows_is_empty_and_long_cells_do_not_break_alignment():
    assert sf.table(["A", "B"], []) == ""
    out = sf.table(["Name", "Note"], [["x", "a much longer note"], ["yyyyyyyy", "b"]])
    lines = out.strip("`\n").splitlines()
    assert lines[2].index("a much") == lines[3].index("b") == lines[0].index("Note")


def test_section_joins_a_heading_and_a_body_and_skips_empty_bodies():
    assert sf.section("Planned buys", "• A\n• B") == "*Planned buys*\n• A\n• B"
    assert sf.section("Planned buys", "") == ""
