"""What a run cost: measured by the runner, shipped to Life's ledger (#28).

The value under test is not "a number gets stored" but the two refusals the
ledger is built on — a killed run never reports 0, and a cost is never
recomputed locally — plus the compatibility constraint that made this feature
non-trivial: ~15 call sites and every injected fake unpack the runner's return
as a 2-tuple, and none of them may break.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from test_claude_runner import _FakeProc, _HangingProc, _patch_spawn

import claude_runner
import llm_usage


def _result_event(**over):
    ev = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "ses_1",
        "result": "ciao",
        "duration_ms": 37000,
        "total_cost_usd": 0.0018,
        "usage": {
            "input_tokens": 2965,
            "output_tokens": 1209,
            "cache_read_input_tokens": 120576,
            "cache_creation_input_tokens": 512,
        },
        "modelUsage": {"claude-sonnet-4-5": {"costUSD": 0.0018}},
    }
    ev.update(over)
    return ev


# --- the carrier ------------------------------------------------------------


def test_result_still_unpacks_as_the_old_two_tuple():
    """The whole reason RunResult subclasses tuple: nothing else may change."""
    r = claude_runner.RunResult("ciao", "ses_1", llm_usage.RunUsage("claude", "sonnet"))

    text, sid = r

    assert (text, sid) == ("ciao", "ses_1")
    assert r == ("ciao", "ses_1")  # equality with a plain tuple still holds
    assert (r.text, r.session_id) == ("ciao", "ses_1")


def test_usage_of_tolerates_the_plain_tuples_the_fakes_return():
    """Injected run_claude fakes (hub, providers, scheduler) return 2-tuples.
    Reading usage off one must be a no-op, not an AttributeError."""
    assert llm_usage.usage_of(("ciao", "ses_1")) is None
    assert llm_usage.usage_of(claude_runner.RunResult("ciao", None)) is None


# --- claude: reading the result event ---------------------------------------


def test_claude_result_event_becomes_a_ledger_row():
    usage = claude_runner.usage_from_result_event(
        _result_event(), model="sonnet", started_at=datetime.now(UTC)
    )

    assert usage.engine == "claude"
    assert usage.outcome == "ok"
    assert usage.cost_usd == 0.0018
    assert (usage.input_tokens, usage.output_tokens) == (2965, 1209)
    assert usage.cache_read_tokens == 120576
    assert usage.cache_write_tokens == 512
    assert usage.duration_ms == 37000


def test_the_model_recorded_is_the_one_that_served_not_the_one_asked_for():
    """`--model sonnet` is a request; modelUsage is what actually ran. The
    ledger's /api/llm/models view answers "which model does this node run",
    which is only worth anything if it reports the observed one."""
    usage = claude_runner.usage_from_result_event(
        _result_event(), model="sonnet", started_at=datetime.now(UTC)
    )

    assert usage.model == "claude-sonnet-4-5"


def test_a_result_event_without_model_usage_falls_back_to_the_requested_model():
    ev = _result_event()
    ev.pop("modelUsage")

    usage = claude_runner.usage_from_result_event(
        ev, model="sonnet", started_at=datetime.now(UTC)
    )

    assert usage.model == "sonnet"


def test_an_errored_result_is_recorded_as_such():
    usage = claude_runner.usage_from_result_event(
        _result_event(is_error=True, subtype="error_during_execution"),
        model="sonnet",
        started_at=datetime.now(UTC),
    )

    assert usage.outcome == "error"


async def test_a_claude_run_carries_its_usage_end_to_end(monkeypatch):
    import json

    line = json.dumps(_result_event()).encode() + b"\n"
    _patch_spawn(monkeypatch, _FakeProc([line]), [])

    result = await claude_runner._run_claude_cli("hi", workspace="/w", persona=None)

    text, sid = result
    assert (text, sid) == ("ciao", "ses_1")
    assert result.usage.cost_usd == 0.0018


async def test_a_killed_run_reports_timeout_with_no_cost(monkeypatch):
    """The rule that makes the ledger readable: a run killed at the cap has
    spent real money nobody can total. 0 there would rank the worst runs as the
    cheapest — exactly the reading error the ledger exists to prevent."""
    monkeypatch.setattr(claude_runner, "CLAUDE_IDLE_TIMEOUT", 0.05)
    _patch_spawn(monkeypatch, _HangingProc([]), [])

    result = await claude_runner._run_claude_cli("hi", workspace="/w", persona=None)

    assert result.usage.outcome == "timeout"
    assert result.usage.cost_usd is None
    assert result.usage.duration_ms >= 0


async def test_a_nonzero_exit_is_recorded_as_an_error(monkeypatch):
    _patch_spawn(monkeypatch, _FakeProc([], returncode=1, stderr=b"boom"), [])

    result = await claude_runner._run_claude_cli("hi", workspace="/w", persona=None)

    assert claude_runner.is_transport_error(result.text)
    assert result.usage.outcome == "error"
    assert result.usage.cost_usd is None


# --- opencode: summing the steps --------------------------------------------


async def test_opencode_sums_cost_and_tokens_over_every_step(monkeypatch):
    """opencode reports per step, not per run: three tool steps report three
    costs and only their sum is what the run cost."""
    _patch_spawn(
        monkeypatch,
        _FakeProc(
            [
                b'{"type":"step_start","sessionID":"ses_1"}\n',
                b'{"type":"step_finish","cost":0.001,'
                b'"tokens":{"input":100,"output":10,"reasoning":5,'
                b'"cache":{"read":40,"write":2}}}\n',
                b'{"type":"text","part":{"id":"p1","text":"ciao"}}\n',
                b'{"type":"step_finish","cost":0.002,'
                b'"tokens":{"input":200,"output":20,"reasoning":0,'
                b'"cache":{"read":60,"write":0}}}\n',
            ]
        ),
        [],
    )

    result = await claude_runner._run_opencode("hi", workspace="/w", persona=None)

    assert result.text == "ciao"
    assert result.usage.engine == "opencode"
    assert result.usage.cost_usd == pytest.approx(0.003)
    assert (result.usage.input_tokens, result.usage.output_tokens) == (300, 30)
    assert result.usage.reasoning_tokens == 5
    assert (result.usage.cache_read_tokens, result.usage.cache_write_tokens) == (100, 2)


async def test_opencode_reads_the_numbers_nested_under_part_too(monkeypatch):
    """opencode's stream schema is empirical here (undocumented): the totals
    have been seen both top-level and under "part". Betting on one shape would
    silently zero the ledger on the other."""
    _patch_spawn(
        monkeypatch,
        _FakeProc(
            [
                b'{"type":"step_finish","part":{"cost":0.004,'
                b'"tokens":{"input":10,"output":1,"cache":{"read":0,"write":0}}}}\n',
            ]
        ),
        [],
    )

    result = await claude_runner._run_opencode("hi", workspace="/w", persona=None)

    assert result.usage.cost_usd == pytest.approx(0.004)
    assert result.usage.input_tokens == 10


async def test_an_opencode_run_cut_short_reports_no_cost(monkeypatch):
    """No step-finish means the run died before the model was done: the partial
    sum would understate the spend, so it is reported as unknown."""
    _patch_spawn(
        monkeypatch,
        _FakeProc([b'{"type":"text","part":{"id":"p1","text":"parziale"}}\n']),
        [],
    )

    result = await claude_runner._run_opencode("hi", workspace="/w", persona=None)

    assert result.text == "parziale"
    assert result.usage.outcome == "error"
    assert result.usage.cost_usd is None


# --- the emitter ------------------------------------------------------------


def test_payload_lower_cases_the_node():
    """The ledger groups on the raw string: homeResearcher and homeresearcher
    arriving from two emitters would split one node's spend in two."""
    usage = llm_usage.RunUsage(
        engine="claude",
        model="sonnet",
        started_at=datetime(2026, 8, 16, 13, 53, 38, tzinfo=UTC),
        cost_usd=0.5,
    )

    payload = usage.payload(node="homeResearcher", caller="telegram")

    assert payload["node"] == "homeresearcher"
    assert payload["started_at"].startswith("2026-08-16T13:53:38")
    assert payload["outcome"] == "ok"


async def test_emit_posts_the_row_with_the_admin_token(monkeypatch):
    sent = {}

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent.update(url=url, json=json, headers=headers)

            class _R:
                def raise_for_status(self):
                    return None

            return _R()

    monkeypatch.setattr(llm_usage.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(llm_usage, "LIFE_ADMIN_TOKEN", "s3cret")
    monkeypatch.setattr(llm_usage, "LIFE_BACKEND_URL", "http://life:9999")

    ok = await llm_usage.emit(
        llm_usage.RunUsage("claude", "sonnet", cost_usd=0.01),
        node="coordinator",
        caller="telegram",
    )

    assert ok is True
    assert sent["url"] == "http://life:9999/api/llm/runs"
    assert sent["headers"]["X-Life-Token"] == "s3cret"
    assert sent["json"]["caller"] == "telegram"


async def test_an_unreachable_ledger_never_costs_the_user_their_reply(monkeypatch):
    """Emission is best-effort by design: the run's purpose is the answer."""

    class _Boom:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(llm_usage.httpx, "AsyncClient", _Boom)
    monkeypatch.setattr(llm_usage, "LIFE_ADMIN_TOKEN", "s3cret")

    assert await llm_usage.emit(
        llm_usage.RunUsage("claude", "sonnet"), node="n", caller="c"
    ) is False


async def test_record_is_a_no_op_without_a_token(monkeypatch):
    """A deployment that never configured the ledger must not fire a request
    per run — nor log an error per run about it."""
    monkeypatch.setattr(llm_usage, "LIFE_ADMIN_TOKEN", "")
    called = []
    monkeypatch.setattr(llm_usage, "emit", lambda *a, **k: called.append(1))

    llm_usage.record(
        claude_runner.RunResult("ciao", None, llm_usage.RunUsage("claude", "sonnet")),
        node="coordinator",
        caller="telegram",
    )

    assert called == []


async def test_record_ships_a_measured_run_without_blocking(monkeypatch):
    monkeypatch.setattr(llm_usage, "LIFE_ADMIN_TOKEN", "s3cret")
    seen = {}

    async def fake_emit(usage, *, node, caller):
        seen.update(usage=usage, node=node, caller=caller)
        return True

    monkeypatch.setattr(llm_usage, "emit", fake_emit)
    usage = llm_usage.RunUsage("claude", "sonnet", cost_usd=0.2)

    llm_usage.record(
        claude_runner.RunResult("ciao", "ses", usage), node="cosmos", caller="telegram"
    )
    await asyncio.sleep(0)  # let the fire-and-forget task run

    assert seen == {"usage": usage, "node": "cosmos", "caller": "telegram"}


async def test_record_ignores_a_result_that_measured_nothing(monkeypatch):
    """A fake, or a backend that reported nothing, claims nothing."""
    monkeypatch.setattr(llm_usage, "LIFE_ADMIN_TOKEN", "s3cret")
    called = []
    monkeypatch.setattr(llm_usage, "emit", lambda *a, **k: called.append(1))

    llm_usage.record(("ciao", "ses"), node="cosmos", caller="telegram")

    assert called == []


# --- wiring (the part that silently regresses) -------------------------------


async def test_a_notification_run_is_attributed_to_its_notification(monkeypatch):
    """The bench in #28 was measured per notification ("stessa notifica, stesso
    workspace"), so that is the granularity the ledger must record."""
    import providers
    from notifications import Notification

    monkeypatch.setattr(
        providers.projects,
        "resolve_project",
        lambda key: type("P", (), {"key": "cheftoni", "workspace": "/w"})(),
    )
    seen = {}
    monkeypatch.setattr(
        providers.llm_usage,
        "record",
        lambda run, *, node, caller: seen.update(node=node, caller=caller),
    )

    async def fake_run_claude(prompt, *, workspace, persona, resume_id):
        return claude_runner.RunResult(
            "testo", "ses", llm_usage.RunUsage("claude", "sonnet", cost_usd=0.01)
        )

    notif = Notification(
        key="cheftoni-cena",
        title="Cena",
        provider="agent",
        schedule="0 18 * * *",
        recipients=("owner",),
        params={"project": "cheftoni", "prompt": "che si mangia"},
    )
    text = await providers._provider_agent(notif, run_claude=fake_run_claude)

    assert text == "testo"
    assert seen == {"node": "cheftoni", "caller": "cheftoni-cena"}


def test_a_live_turn_dates_itself_from_its_own_duration():
    """A warm session spans many turns, so there is no spawn time to use as the
    run's start: it is walked back from now over the event's duration. Without
    this the interactive half of the bot is missing from the ledger entirely on
    every CLAUDE_LIVE_SESSIONS deployment — which is the default one."""
    import bot

    usage = bot._live_usage_from_event(_result_event(duration_ms=5000))
    age_s = (datetime.now(UTC) - usage.started_at).total_seconds()

    assert usage.cost_usd == 0.0018
    assert 4.5 <= age_s <= 10  # dated back by the turn's own duration


async def test_a_durable_outage_is_reported_once_not_once_per_run(monkeypatch, caplog):
    """Life's 0006 migration missing means every run fails identically for days.
    One warning per run would bury the bot's own logs, so the repeat is DEBUG
    and the next distinct cause (or the next success) re-arms the warning."""

    class _Boom:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(llm_usage.httpx, "AsyncClient", _Boom)
    monkeypatch.setattr(llm_usage, "LIFE_ADMIN_TOKEN", "s3cret")
    monkeypatch.setattr(llm_usage, "_last_failure", None)
    usage = llm_usage.RunUsage("claude", "sonnet")

    with caplog.at_level("WARNING", logger="coordinator.llm_usage"):
        for _ in range(3):
            await llm_usage.emit(usage, node="n", caller="c")

    assert len(caplog.records) == 1
