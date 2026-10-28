# tests/test_wake_pool.py
import wake_pool


def _tracker(start=0):
    persisted = []
    t = wake_pool.Tracker(start, persisted.append)
    return t, persisted


def test_in_order_completion_advances_step_by_step():
    t, persisted = _tracker(100)
    for ev in (101, 102, 103):
        t.on_received(ev)
    t.on_done(101)
    t.on_done(102)
    t.on_done(103)
    assert t.cursor() == 103
    assert persisted == [101, 102, 103]


def test_out_of_order_holds_cursor_back():
    t, persisted = _tracker(100)
    for ev in (101, 102, 103):
        t.on_received(ev)
    t.on_done(103)                 # newest finishes first
    assert t.cursor() == 100       # 101/102 still in flight -> cursor held
    assert persisted == []
    t.on_done(101)
    assert t.cursor() == 101        # only the contiguous prefix
    assert persisted == [101]
    t.on_done(102)                 # fills the gap -> jumps over 102 AND 103
    assert t.cursor() == 103
    assert persisted == [101, 103]


def test_unknown_blocks_all_later_commits_and_flags_reconnect():
    t, persisted = _tracker(100)
    for ev in (101, 102, 103):
        t.on_received(ev)
    t.on_done(101)                 # cursor -> 101
    t.on_unknown(102)             # 102 unconfirmed -> never done
    t.on_done(103)                 # completes but is behind the blocked 102
    assert t.cursor() == 101        # blocked at 101
    assert t.reconnect_needed is True
    assert persisted == [101]


def test_in_flight_counts_pending():
    t, _ = _tracker(0)
    t.on_received(1)
    t.on_received(2)
    assert t.in_flight() == 2
    t.on_done(1)
    assert t.in_flight() == 1


def test_persist_only_when_low_water_mark_moves():
    t, persisted = _tracker(0)
    t.on_received(1)
    t.on_received(2)
    t.on_done(2)                   # does not move (1 pending)
    assert persisted == []
    t.on_done(1)                   # moves to 2
    assert persisted == [2]


# append to tests/test_wake_pool.py
import asyncio


def _run(coro):
    return asyncio.run(coro)


def test_semaphore_bounds_concurrency():
    wake_pool.configure(2)
    active = 0
    peak = 0

    async def body(key):
        nonlocal active, peak
        async with wake_pool.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    async def main():
        await asyncio.gather(*(body(("t", i)) for i in range(6)))

    _run(main())
    assert peak <= 2                 # never more than the bound active at once


def test_slot_reports_when_it_has_to_wait():
    wake_pool.configure(1)
    waited = []

    async def main():
        async with wake_pool.slot(lambda: waited.append("first")):
            inner = asyncio.create_task(_take(lambda: waited.append("second")))
            await asyncio.sleep(0)
        await inner

    async def _take(on_wait):
        async with wake_pool.slot(on_wait):
            pass

    _run(main())
    assert waited == ["second"]


def test_guard_is_not_bounded_by_the_slots():
    """#74: deciding an event never waits behind claude runs of other convs."""
    wake_pool.configure(1)

    async def main():
        async with wake_pool.slot():          # the only slot is busy
            async with wake_pool.guard(("t", 7)):
                return "decided"

    assert asyncio.run(asyncio.wait_for(main(), 1.0)) == "decided"


def test_same_conv_serializes():
    wake_pool.configure(5)
    order = []

    async def body(tag):
        async with wake_pool.guard(("t", 1)):   # same conv key
            order.append(("start", tag))
            await asyncio.sleep(0.01)
            order.append(("end", tag))

    async def main():
        await asyncio.gather(body("a"), body("b"))

    _run(main())
    # serialized: one fully completes before the other starts
    assert order in (
        [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")],
        [("start", "b"), ("end", "b"), ("start", "a"), ("end", "a")],
    )


def test_different_convs_overlap():
    wake_pool.configure(5)
    active = 0
    peak = 0

    async def body(key):
        nonlocal active, peak
        async with wake_pool.guard(key):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    async def main():
        await asyncio.gather(body(("t", 1)), body(("t", 2)))

    _run(main())
    assert peak == 2                 # different convs run in parallel


def test_lock_registry_pruned_after_use():
    wake_pool.configure(5)

    async def body():
        async with wake_pool.guard(("t", 99)):
            pass

    _run(body())
    assert ("t", 99) not in wake_pool._conv_locks      # refcount hit 0 -> pruned
