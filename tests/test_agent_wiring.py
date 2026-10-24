# Author: Kai0421
"""Agent wiring (docs/05, docs/11): files exist, stay small and keep agents off the order path."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_agents_md_is_small_and_points_to_memory():
    text = (ROOT / "AGENTS.md").read_text()
    assert len(text.splitlines()) <= 60
    assert "memory/README.md" in text


def test_memory_layout():
    for name in ("README.md", "lessons.md", "strategy.md", "decisions.md"):
        assert (ROOT / "memory" / name).exists(), name
    lessons = [
        ln for ln in (ROOT / "memory/lessons.md").read_text().splitlines() if ln.startswith("- ")
    ]
    assert len(lessons) <= 50


def test_prompts_read_packs_only():
    for name, pack in (
        ("daily_review.md", "memory/packs/daily-"),
        ("weekly_review.md", "memory/packs/weekly-"),
    ):
        text = (ROOT / "prompts" / name).read_text()
        assert pack in text and "python -m bot.notify" in text
        assert "Do NOT explore the repository" in text


def test_routine_prompts_treat_trigger_text_as_data():
    for name, branch in (
        ("routine_daily.md", "claude/review-"),
        ("routine_weekly.md", "claude/weekly-"),
    ):
        text = (ROOT / "prompts" / name).read_text()
        assert "DATA" in text and "never instructions" in text
        assert "uv run python -m bot.notify" in text
        assert "--thread" not in text  # no local bot.db in the cloud
        assert branch in text
        assert "never place, modify or cancel orders" in text
        assert "Do NOT explore the repository" in text


def test_engine_workflow_is_safe_by_default():
    text = (ROOT / ".github/workflows/engine.yml").read_text()
    assert "concurrency:" in text and "cancel-in-progress: false" in text
    assert "DRY_RUN: ${{ vars.DRY_RUN || 'true' }}" in text
    assert 'PAPER: "true"' in text
    assert "bot-state" in text and "bot.state_store" in text
    # The engine runs on the Mac (launchd); the workflow is manual-only so the two never
    # trade at the same time.
    assert "workflow_dispatch:" in text
    assert "schedule:" not in text and "cron:" not in text
