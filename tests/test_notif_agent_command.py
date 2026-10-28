# tests/test_notif_agent_command.py
"""Agent notifications: `command` run by the Coordinator; meta-text is an error."""
import asyncio
import sys

import pytest

import message_guard
import projects
import providers
import wake_routing
from notifications import Notification


def _run(coro):
    return asyncio.run(coro)


def _py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


@pytest.fixture
def ws(tmp_path, monkeypatch):
    proj = projects.Project("aa", "AA", "\U0001F393", "", str(tmp_path))
    monkeypatch.setattr(projects, "resolve_project", lambda key: proj if key == "aa" else None)

    async def no_service(*a, **k):
        return None

    monkeypatch.setattr(wake_routing, "run_node_service", no_service)
    monkeypatch.setattr(providers.llm_usage, "record", lambda *a, **kw: None)
    return tmp_path


def _agent(**params) -> Notification:
    return Notification("aa-n", "Lezione", "agent", "*/30 * * * *", (), True,
                        {"project": "aa", "prompt": "Riassumi", **params}, destination="group")


class _Claude:
    def __init__(self, reply="RIASSUNTO"):
        self.reply, self.prompts = reply, []

    async def __call__(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply, None


def test_command_empty_stdout_never_calls_the_llm(ws):
    claude = _Claude()
    assert _run(providers.run_provider(_agent(command=_py("pass")), run_claude=claude)) is None
    assert claude.prompts == []


def test_command_failure_raises_without_llm(ws):
    claude = _Claude()
    n = _agent(command=_py("import sys; sys.stderr.write('boom'); sys.exit(3)"))
    with pytest.raises(providers.ProviderError, match="exit 3"):
        _run(providers.run_provider(n, run_claude=claude))
    assert claude.prompts == []


def test_command_stdout_reaches_the_prompt(ws):
    claude = _Claude()
    n = _agent(command=_py("print('<lezione> 1')"))
    assert _run(providers.run_provider(n, run_claude=claude)) == "RIASSUNTO"
    assert claude.prompts[0].startswith("Riassumi\n\nOutput del comando:\n<lezione> 1")
    assert claude.prompts[0].endswith(providers.AGENT_DELIVERY_RULE)


@pytest.mark.parametrize("reply", [
    "Non ho inviato nessun messaggio: il comando richiede un'approvazione.",
    "Ho restituito una stringa vuota.",
    "(The command produced no output)",
    "K",
])
def test_agent_meta_text_is_an_error(ws, reply):
    with pytest.raises(providers.ProviderError):
        _run(providers.run_provider(_agent(), run_claude=_Claude(reply)))


def test_meta_pattern_is_narrow():
    assert not message_guard.is_agent_meta("Oggi alle 14 c'è la lezione di Pittura.")
