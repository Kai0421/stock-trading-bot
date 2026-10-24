# Author: Kai0421
"""The engine's non-blocking file lock: fcntl on macOS/Linux, msvcrt on Windows."""

from __future__ import annotations

import types

from bot import filelock


def test_second_handle_cannot_take_a_held_lock(tmp_path):
    path = tmp_path / "engine.lock"
    with open(path, "w") as first, open(path, "w") as second:
        assert filelock.try_lock(first)
        assert not filelock.try_lock(second)
    with open(path, "w") as again:
        assert filelock.try_lock(again)


def test_windows_branch_uses_msvcrt(tmp_path, monkeypatch):
    calls = []

    def locking(fd, mode, nbytes):
        calls.append((mode, nbytes))
        if len(calls) > 1:
            raise OSError("locked")

    fake = types.SimpleNamespace(LK_NBLCK=2, locking=locking)
    monkeypatch.setattr(filelock, "fcntl", None)
    monkeypatch.setattr(filelock, "msvcrt", fake)
    with open(tmp_path / "a.lock", "w") as a, open(tmp_path / "b.lock", "w") as b:
        assert filelock.try_lock(a)
        assert not filelock.try_lock(b)
    assert calls == [(2, 1), (2, 1)]
