# wake_pool.py
"""Concurrency engine for per-agent wakes.

`Tracker` is the loss-safety crux: it advances a cursor only across the
contiguous-completed prefix of received SSE event ids, so the persisted cursor
never moves past an event whose wake is still in flight (or whose outcome is
unconfirmed). This generalizes feature #1's "never advance past an unverified
event" to out-of-order concurrent completion.

`guard` serializes the handling of one conversation; `slot`/`configure` bound the
concurrent claude runs (process-wide semaphore, taken around the run only).
"""
from __future__ import annotations

import asyncio
from typing import Callable


class Tracker:
    """Per-listener low-water-mark cursor accumulator.

    received() records an event id in arrival order; done() marks completion and
    advances the cursor over the longest completed prefix, persisting when it
    moves; unknown() leaves the id pending (blocking the prefix) and flags a
    reconnect. The cursor is therefore always <= the last fully-handled event.
    """

    def __init__(self, start_cursor, persist: Callable[[int], None]):
        self._cursor = start_cursor
        self._persist = persist
        self._pending: list[int] = []   # received, not yet committed, in order
        self._done: set[int] = set()
        self.reconnect_needed = False

    def on_received(self, ev_id: int) -> None:
        self._pending.append(ev_id)

    def on_done(self, ev_id: int) -> None:
        self._done.add(ev_id)
        moved = False
        while self._pending and self._pending[0] in self._done:
            self._cursor = self._pending.pop(0)
            self._done.discard(self._cursor)
            moved = True
        if moved:
            self._persist(self._cursor)

    def on_unknown(self, ev_id: int) -> None:
        # Leave ev_id in _pending (never marked done) so it blocks the prefix;
        # signal the listener to reconnect and redeliver from the held cursor.
        self.reconnect_needed = True

    def cursor(self):
        return self._cursor

    def in_flight(self) -> int:
        return len(self._pending)


_sem: asyncio.Semaphore | None = None
_conv_locks: dict = {}
_refcount: dict = {}


def configure(max_concurrency: int) -> None:
    """(Re)create the process-wide wake semaphore. Call once at startup."""
    global _sem
    _sem = asyncio.Semaphore(max(1, int(max_concurrency)))


class _Slot:
    """Async context manager: one of the process-wide claude-run slots.

    Taken around the claude RUN only, never around the decision that precedes
    it. Held around the whole event handling, three long wakes (up to an hour
    each) parked EVERY other event of EVERY agent behind them -- skips,
    no_reply_needed, queueing included: on 14/10 a new conversation waited 36
    minutes before anyone even looked at it (#74).
    """

    def __init__(self, on_wait: Callable[[], None] | None = None):
        self._on_wait = on_wait
        self._sem = None

    async def __aenter__(self):
        self._sem = _sem            # capture the semaphore this slot will release
        if self._sem is not None:
            if self._sem.locked() and self._on_wait is not None:
                self._on_wait()
            await self._sem.acquire()
        return self

    async def __aexit__(self, *exc):
        if self._sem is not None:
            self._sem.release()
        return False


def slot(on_wait: Callable[[], None] | None = None) -> _Slot:
    """Bound concurrent claude runs; ``on_wait`` is called if it has to queue."""
    return _Slot(on_wait)


class _Guard:
    """Async context manager: serialize the handling of one conversation.

    Deliberately NOT bounded by the semaphore: deciding what to do with an event
    is cheap and must never wait for an unrelated agent's hour-long run (#74).
    The bound sits on the claude run itself (``slot``).
    """

    def __init__(self, conv_key):
        self._key = conv_key
        self._lock = None

    async def __aenter__(self):
        key = self._key
        # refcount inc + lock lookup are synchronous (no await) -> atomic in the loop
        _refcount[key] = _refcount.get(key, 0) + 1
        self._lock = _conv_locks.setdefault(key, asyncio.Lock())
        try:
            await self._lock.acquire()
        except BaseException:
            self._release_ref(key)
            raise
        return self

    async def __aexit__(self, *exc):
        self._lock.release()
        self._release_ref(self._key)
        return False

    @staticmethod
    def _release_ref(key) -> None:
        n = _refcount.get(key, 0) - 1
        if n <= 0:
            _refcount.pop(key, None)
            _conv_locks.pop(key, None)
        else:
            _refcount[key] = n


def guard(conv_key) -> "_Guard":
    return _Guard(conv_key)
