# Author: Kai0421
"""Claude usage watcher: low-usage alerts and the pre-flight gate for AI jobs.

Reads the snapshot that ``scripts/claude_statusline.sh`` writes from the documented
status-line fields (``rate_limits.five_hour`` / ``seven_day``). It never calls Claude,
so it costs zero tokens, and it can never affect trading (docs/08).

Thresholds (docs/08 "Low-usage alerts"):
- 7-day >= 50% INFO, >= 75% WARN, >= 90% WARN and AI jobs paused.
- 5-hour >= 80% with an AI job due before the 5-hour reset: WARN "review may be skipped".
- Gate: skip AI jobs when 7-day >= 90% or 5-hour >= 85%.
- Snapshot older than 48 h: INFO "usage unknown". No snapshot at all (e.g. on GitHub
  Actions): silent. The gate stays open in both cases.
Alert keys include the window's reset time so each threshold fires once per window.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from bot.notify import Level

STALE_AFTER = timedelta(hours=48)
WEEKLY_LEVELS = ((90, Level.WARN), (75, Level.WARN), (50, Level.INFO))
FIVE_HOUR_WARN = 80
GATE_WEEKLY = 90
GATE_FIVE_HOUR = 85
ROUTINE_BETA = "experimental-cc-routine-2026-04-01"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class UsageWindow:
    used_percentage: float
    resets_at: datetime


@dataclass(frozen=True)
class Snapshot:
    captured_at: datetime
    five_hour: UsageWindow | None
    seven_day: UsageWindow | None


def _window(raw: Any) -> UsageWindow | None:
    try:
        return UsageWindow(
            float(raw["used_percentage"]), datetime.fromtimestamp(float(raw["resets_at"]), UTC)
        )
    except (TypeError, KeyError, ValueError):
        return None


def read_snapshot(path: Path) -> Snapshot | None:
    try:
        raw = json.loads(path.read_text())
        captured = datetime.fromisoformat(raw["captured_at"].replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return Snapshot(captured, _window(raw.get("five_hour")), _window(raw.get("seven_day")))


@dataclass(frozen=True)
class UsageAlert:
    level: Level
    key: str
    text: str


@dataclass
class UsageResult:
    alerts: list[UsageAlert] = field(default_factory=list)
    gate_ok: bool = True
    gate_reason: str = ""


def _live(w: UsageWindow | None, now: datetime) -> UsageWindow | None:
    """A window whose reset time has passed no longer counts."""
    return w if w is not None and w.resets_at > now else None


def evaluate(
    snapshot: Snapshot | None, now: datetime, ai_job_due: datetime | None = None
) -> UsageResult:
    result = UsageResult()
    if snapshot is None:
        return result  # no usage data here at all (e.g. GitHub Actions): stay quiet
    if now - snapshot.captured_at > STALE_AFTER:
        result.alerts.append(
            UsageAlert(
                Level.INFO,
                f"usage:stale:{now.date().isoformat()}",
                "Claude usage is unknown. Open Claude Code or check Settings > Usage.",
            )
        )
        return result

    week = _live(snapshot.seven_day, now)
    if week is not None:
        for threshold, level in WEEKLY_LEVELS:
            if week.used_percentage >= threshold:
                when = week.resets_at.strftime("%a %H:%M UTC")
                extra = ". AI reviews are paused until then" if threshold == GATE_WEEKLY else ""
                result.alerts.append(
                    UsageAlert(
                        level,
                        f"usage:7d:{threshold}:{int(week.resets_at.timestamp())}",
                        f"Claude weekly usage is at {week.used_percentage:.0f}%; "
                        f"it resets {when}{extra}.",
                    )
                )
                break
        if week.used_percentage >= GATE_WEEKLY:
            result.gate_ok = False
            result.gate_reason = f"7-day usage {week.used_percentage:.0f}% >= {GATE_WEEKLY}%"

    five = _live(snapshot.five_hour, now)
    if five is not None:
        due_before_reset = ai_job_due is not None and ai_job_due < five.resets_at
        if five.used_percentage >= FIVE_HOUR_WARN and due_before_reset:
            result.alerts.append(
                UsageAlert(
                    Level.WARN,
                    f"usage:5h:{FIVE_HOUR_WARN}:{int(five.resets_at.timestamp())}",
                    f"Claude 5-hour usage is at {five.used_percentage:.0f}%, so the next review "
                    "may be skipped.",
                )
            )
        if five.used_percentage >= GATE_FIVE_HOUR and result.gate_ok:
            result.gate_ok = False
            result.gate_reason = f"5-hour usage {five.used_percentage:.0f}% >= {GATE_FIVE_HOUR}%"
    return result


def fire_routine(url: str, token: str, text: str, post: Callable[..., Any] = requests.post) -> bool:
    """Trigger a Claude Code routine via its API trigger. Never raises."""
    if not (url and token):
        return False
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": ROUTINE_BETA,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    try:
        resp = post(url, headers=headers, json={"text": text}, timeout=10)
    except Exception:
        return False
    return 200 <= resp.status_code < 300
