# Author: Kai0421
"""Non-blocking exclusive file lock that works on macOS/Linux (fcntl) and Windows (msvcrt).

The lock is released when the file handle is closed (or the process exits).
"""

from __future__ import annotations

from typing import IO, Any

fcntl: Any
msvcrt: Any
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # macOS / Linux
    msvcrt = None


def try_lock(handle: IO[Any]) -> bool:
    """Take an exclusive lock on ``handle`` without waiting; False if another holder has it."""
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True
