"""TDD for Task 5: hub.dispatch_webhook_event routes task.due to an injected
handler with dedup on scheduled_task_id. See .superpowers/sdd/task-5-brief.md.
"""

import asyncio

import hub


def _run(coro):
    return asyncio.run(coro)


def setup_function(_fn):
    """Isolate dedup state between tests (module-level set)."""
    hub._seen_task_due.clear()
    hub._task_due_handler = None


def test_task_due_invokes_handler_once():
    """Same scheduled_task_id + same phase = true duplicate -> fires once."""
    seen = []

    async def _cb(evt):
        seen.append(evt["scheduled_task_id"])

    hub.set_task_due_handler(_cb)
    evt = {
        "type": "task.due",
        "scheduled_task_id": 9,
        "phase": "start",
        "user_telegram_chat_id": 1,
    }

    _run(hub.dispatch_webhook_event("t", "a", evt))
    _run(hub.dispatch_webhook_event("t", "a", evt))  # duplicate (same id, same phase)

    assert seen == [9]


def test_task_due_pre_and_start_both_fire():
    """Same scheduled_task_id, different phase ("pre" then "start") -> both
    render: Scheduler pushes two distinct events per task and dedup must key
    on (scheduled_task_id, phase), not scheduled_task_id alone."""
    seen = []

    async def _cb(evt):
        seen.append(evt["phase"])

    hub.set_task_due_handler(_cb)
    pre_evt = {"type": "task.due", "scheduled_task_id": 9, "phase": "pre"}
    start_evt = {"type": "task.due", "scheduled_task_id": 9, "phase": "start"}

    _run(hub.dispatch_webhook_event("t", "a", pre_evt))
    _run(hub.dispatch_webhook_event("t", "a", start_evt))

    assert seen == ["pre", "start"]


def test_task_due_without_handler_is_fail_safe():
    """No handler registered -> logs a warning, never raises."""
    evt = {"type": "task.due", "scheduled_task_id": 1}
    _run(hub.dispatch_webhook_event("t", "a", evt))  # must not raise


def test_non_task_event_passes_through(monkeypatch):
    """A non-task.due event must not invoke the task_due handler, and must
    still flow through the normal webhook-to-bus dispatch path."""
    seen = []

    async def _cb(evt):
        seen.append(evt)

    hub.set_task_due_handler(_cb)

    normal_dispatch_calls = []

    async def fake_dispatch(tenant, agent_id, ev_type, data):
        normal_dispatch_calls.append((tenant, agent_id, ev_type, data))

    monkeypatch.setattr(hub, "_dispatch", fake_dispatch)

    evt = {
        "taskId": "42",
        "status": {"state": "completed"},
        "final": True,
        "metadata": {},
    }
    _run(hub.dispatch_webhook_event("t", "a", evt))

    assert seen == []
    assert len(normal_dispatch_calls) == 1
