# tests/test_scheduler_client_schedule.py
"""Maggiordomo proattivo (AC) Task 2: SchedulerClient.list_scheduled_tasks +
reschedule_task. Mirrors the c._get/c._post monkeypatch style used by
test_list_categories_parses in tests/test_scheduler_client_taskgen.py."""
import asyncio

import pytest

from scheduler_client import SchedulerClient


def _run(coro):
    return asyncio.run(coro)


# --- list_scheduled_tasks ---


def test_list_scheduled_tasks_calls_get_with_window_and_origin(monkeypatch):
    c = SchedulerClient(base_url="http://hw.test", secret="s")
    captured = {}

    async def fake_get(path, headers=None):
        captured["path"] = path
        captured["headers"] = headers
        return [{"id": 1, "name": "Cena"}]

    monkeypatch.setattr(c, "_get", fake_get)
    out = _run(
        c.list_scheduled_tasks(
            telegram_chat_id=542,
            frm="2026-07-09T00:00:00+02:00",
            to="2026-07-10T00:00:00+02:00",
        )
    )
    assert captured["path"].startswith("/api/agent/scheduled-tasks?")
    assert "from=2026-07-09T00%3A00%3A00%2B02%3A00" in captured["path"]
    assert "to=2026-07-10T00%3A00%3A00%2B02%3A00" in captured["path"]
    assert "telegram_chat_id=542" in captured["path"]
    assert captured["headers"]["X-Agent-Origin"] == "user"
    assert out == [{"id": 1, "name": "Cena"}]


def test_list_scheduled_tasks_returns_empty_list_on_non_list(monkeypatch):
    c = SchedulerClient(base_url="http://hw.test", secret="s")

    async def fake_get(path, headers=None):
        return {"detail": "not a list"}

    monkeypatch.setattr(c, "_get", fake_get)
    out = _run(
        c.list_scheduled_tasks(
            telegram_chat_id=542,
            frm="2026-07-09T00:00:00+02:00",
            to="2026-07-10T00:00:00+02:00",
        )
    )
    assert out == []


# --- reschedule_task ---


def test_reschedule_task_calls_post_with_path_body_and_origin(monkeypatch):
    c = SchedulerClient(base_url="http://hw.test", secret="s")
    captured = {}

    async def fake_post(path, body, headers=None):
        captured["path"] = path
        captured["body"] = body
        captured["headers"] = headers
        return {"id": 7, "start_at": "2026-07-09T21:00:00+02:00"}

    monkeypatch.setattr(c, "_post", fake_post)
    out = _run(
        c.reschedule_task(
            st_id=7, telegram_chat_id=542, start_at="2026-07-09T21:00:00+02:00"
        )
    )
    assert captured["path"] == "/api/agent/scheduled-tasks/7/reschedule"
    assert captured["body"] == {
        "telegram_chat_id": 542,
        "start_at": "2026-07-09T21:00:00+02:00",
    }
    assert captured["headers"]["X-Agent-Origin"] == "user"
    assert out["id"] == 7


def test_reschedule_task_propagates_typed_error(monkeypatch):
    import scheduler_client as hw

    c = SchedulerClient(base_url="http://hw.test", secret="s")

    async def fake_post(path, body, headers=None):
        raise hw.SchedulerConflict("Scheduler 409: already decided")

    monkeypatch.setattr(c, "_post", fake_post)
    with pytest.raises(hw.SchedulerConflict):
        _run(
            c.reschedule_task(
                st_id=7, telegram_chat_id=542, start_at="2026-07-09T21:00:00+02:00"
            )
        )
