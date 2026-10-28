# tests/test_quiet_failures.py
"""A failed notification goes to the outcome ledger, never to the chat."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import notif_health
import notifications
import providers
from notifications import Notification


def _agent(prompt="fai il punto"):
    return Notification("aa-pre", "Pre lezione", "agent", "*/30 * * * *", ("owner",),
                        True, {"project": "aa", "prompt": prompt}, destination="group")


@pytest.fixture
def agent_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(providers.projects, "resolve_project",
                        lambda k: SimpleNamespace(key="aa", workspace="."))

    async def no_service(*a, **kw):
        return None
    monkeypatch.setattr(providers.wake_routing, "run_node_service", no_service)
    monkeypatch.setattr(providers.llm_usage, "record", lambda *a, **kw: None)

    def answer(text):
        async def fake(prompt, **kw):
            seen["prompt"] = prompt
            return (text, None)
        return asyncio.run(providers.run_provider(_agent(), run_claude=fake))
    answer.seen = seen
    return answer


def test_agent_prompt_carries_the_delivery_rule(agent_run):
    agent_run("ciao")
    assert agent_run.seen["prompt"].endswith(providers.AGENT_DELIVERY_RULE)


def test_agent_text_is_sent(agent_run):
    assert agent_run("Lezione alle 10") == "Lezione alle 10"


def test_agent_no_reply_marker_is_silence(agent_run):
    assert agent_run(providers.NO_REPLY_MARKER) is None


@pytest.mark.parametrize("text, reason", [
    ("[[ERRORE]] permesso negato su Bash", "permesso negato su Bash"),
    ("❌ coda coordinator.notificaAgente esaurita: x", "❌ coda coordinator.notificaAgente esaurita: x"),
])
def test_agent_failure_raises(agent_run, text, reason):
    with pytest.raises(providers.ProviderError, match=reason[:20]):
        agent_run(text)


def test_error_outcome_alarms_at_once():
    now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    rec = {"last_sent_at": now.isoformat(), "last_attempt_at": now.isoformat(),
           "last_outcome": f"{notifications.OUTCOME_ERROR_PREFIX}: boom",
           "last_category": notifications.classify_outcome("errore: boom"),
           "first_seen": now.isoformat()}
    entry = notif_health._entry(_agent(), rec, now)
    assert rec["last_category"] == notifications.CAT_BROKEN
    assert entry["status"] == notif_health.STATUS_BROKEN and entry["alarm"]


def test_digest_escapes_error_text():
    report = {"status": "critical", "watchdog": {"stale": False}, "notifications": [],
              "alarms": [{"key": "k", "reason": "r", "silent_for_h": 1, "schedule": "* * * * *",
                          "last_outcome": "errore: <oops> &"}]}
    assert "&lt;oops&gt; &amp;" in notif_health.format_alert(report)
