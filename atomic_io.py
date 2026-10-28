# atomic_io.py
"""Atomic file replacement that survives Windows.

Every store in this repo persists JSON the same way: write a sibling temp file,
then ``os.replace()`` it over the target so no reader ever sees a truncated
file. On POSIX that rename is genuinely atomic and never fails for lock reasons.

On Windows it does. ``os.replace`` maps to MoveFileEx, which refuses with
``PermissionError [WinError 5]`` whenever *any* process holds an open handle on
the source or the destination — and a real-time antivirus scanner opens a file
the instant it is created, for a few milliseconds. The write is not wrong, it is
merely early: retrying the same call a moment later succeeds.

That failure was first seen as flaky tests (migrate/restructure/pipeline stores
losing a write under a fast write loop), but the same code runs inside the bot on
the Windows worker, where a lost write means a half-recorded migration phase.

``replace_with_retry`` is therefore the single chokepoint for the swap. It keeps
the last error and re-raises it if every attempt fails: a genuine permission
problem (read-only file, wrong ACL) still surfaces instead of being swallowed.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("coordinator.atomic_io")

# Total worst-case wait is ~0.35s (5 attempts, 10ms doubling to 160ms). Tuned to
# outlast a real-time AV scan, which clears in tens of milliseconds, while never
# blocking an async handler long enough to matter. Raising the attempt count
# trades responsiveness for resilience on a heavily-scanned box.
_ATTEMPTS = 5
_INITIAL_BACKOFF_SEC = 0.01


def replace_with_retry(src: str, dst: str) -> None:
    """``os.replace(src, dst)``, retrying the transient Windows lock.

    Raises the last ``PermissionError`` if the swap never succeeds — a lock that
    outlives every attempt is not transient and must not pass silently.
    """
    backoff = _INITIAL_BACKOFF_SEC
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            os.replace(src, dst)
            if attempt > 1:
                logger.debug("replace of %s succeeded on attempt %d", dst, attempt)
            return
        except PermissionError:
            if attempt == _ATTEMPTS:
                logger.warning(
                    "could not replace %s after %d attempts: still locked",
                    dst,
                    _ATTEMPTS,
                )
                raise
            time.sleep(backoff)
            backoff *= 2
