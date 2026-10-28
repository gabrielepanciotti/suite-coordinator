import asyncio

import pytest

import bot


def test_projects_refresh_loop_calls_refresh_and_survives_errors(monkeypatch):
    calls = []

    def fake_refresh():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"added": [], "removed": []}

    monkeypatch.setattr(bot.projects, "refresh_projects", fake_refresh)
    monkeypatch.setattr(bot, "PROJECTS_REFRESH_INTERVAL_SEC", 0)

    async def go():
        task = asyncio.create_task(bot._projects_refresh_loop())
        # Let the loop run a couple of iterations (including the one that raises).
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())

    assert len(calls) >= 2  # kept looping after the RuntimeError
