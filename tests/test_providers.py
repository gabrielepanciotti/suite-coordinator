import os
import time
import asyncio

import providers
from notifications import Notification


def _run(coro):
    return asyncio.run(coro)


def _n(provider, params):
    return Notification("k", "T", provider, "0 8 * * *", ("owner",), True, params)


def test_static_returns_text():
    assert _run(providers.run_provider(_n("static", {"text": "ciao"}), run_claude=None)) == "ciao"


def test_static_empty_is_none():
    assert _run(providers.run_provider(_n("static", {"text": "  "}), run_claude=None)) is None


def test_agent_calls_run_claude_with_workspace():
    calls = {}

    async def fake_run_claude(prompt, *, workspace, persona, resume_id):
        calls.update(prompt=prompt, workspace=workspace, persona=persona, resume_id=resume_id)
        return "RISPOSTA", "sid-1"

    n = _n("agent", {"project": "personalChef", "prompt": "vai"})
    out = _run(providers.run_provider(n, run_claude=fake_run_claude))
    assert out == "RISPOSTA"
    assert calls["prompt"] == "vai" + providers.AGENT_DELIVERY_RULE and calls["resume_id"] is None and calls["persona"] is None
    assert calls["workspace"].endswith("personalChef")          # project.workspace container path


def test_agent_unknown_project_is_none():
    n = _n("agent", {"project": "ghost", "prompt": "x"})
    assert _run(providers.run_provider(n, run_claude=None)) is None


def test_growbox_stub_when_ha_not_configured(monkeypatch):
    monkeypatch.setattr(providers, "HA_BASE_URL", "")
    monkeypatch.setattr(providers, "HA_TOKEN", "")
    out = _run(providers.run_provider(_n("growbox", {}), run_claude=None))
    assert "non disponibili" in out.lower() or "non configurato" in out.lower()


# --- report provider: a file a node's own job already wrote -----------------


def _report(tmp_path, monkeypatch, body, *, age_h=0.0, name="drift.txt", **params):
    monkeypatch.setenv("REPORT_PROVIDER_ROOT", str(tmp_path))
    target = tmp_path / name
    target.write_text(body, encoding="utf-8")
    if age_h:
        old = time.time() - age_h * 3600
        os.utime(target, (old, old))
    return _n("report", {"path": name, **params})


def test_report_delivers_the_file_content(tmp_path, monkeypatch):
    n = _report(tmp_path, monkeypatch, "Cosmos 25 indietro\n")
    assert _run(providers.run_provider(n, run_claude=None)) == "Cosmos 25 indietro"


def test_an_empty_report_sends_nothing(tmp_path, monkeypatch):
    # Silence is the feature: the producer prints nothing when all is aligned.
    n = _report(tmp_path, monkeypatch, "\n  \n")
    assert _run(providers.run_provider(n, run_claude=None)) is None


def test_a_stale_report_speaks_instead_of_repeating_itself(tmp_path, monkeypatch):
    # The worst case: the job died and yesterday's file still reads as reassuring.
    n = _report(tmp_path, monkeypatch, "tutto a posto", age_h=50)
    out = _run(providers.run_provider(n, run_claude=None))
    assert out is not None and "fermo da" in out and "tutto a posto" not in out


def test_a_report_within_its_period_is_not_called_stale(tmp_path, monkeypatch):
    n = _report(tmp_path, monkeypatch, "contenuto", age_h=24)
    assert _run(providers.run_provider(n, run_claude=None)) == "contenuto"


def test_a_missing_report_is_named_never_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORT_PROVIDER_ROOT", str(tmp_path))
    n = _n("report", {"path": "mai-scritto.txt"})
    out = _run(providers.run_provider(n, run_claude=None))
    assert out is not None and "mai-scritto.txt" in out


def test_a_path_escaping_the_workspaces_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORT_PROVIDER_ROOT", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    (tmp_path / "segreto.txt").write_text("TOKEN", encoding="utf-8")
    n = _n("report", {"path": "../segreto.txt"})
    out = _run(providers.run_provider(n, run_claude=None))
    assert "TOKEN" not in (out or "")


# --- regime provider: Health renders the slot at send time ------------------

import httpx
import pytest

import notifications


def _regime(monkeypatch, handler, **params):
    """Run the regime provider against a fake Health served by ``handler``."""
    seen = []

    def _wrapped(request):
        seen.append(request)
        return handler(request)

    real = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(_wrapped), **kw))
    monkeypatch.setattr(providers, "HEALTH_URL", "http://health.test")
    out = _run(providers.run_provider(_n("regime", params), run_claude=None))
    return out, seen


def _ok(body):
    return lambda request: httpx.Response(200, json=body)


def test_regime_forwards_the_text_health_rendered(monkeypatch):
    out, seen = _regime(monkeypatch, _ok(
        {"slot": "breakfast", "text": "💊 <b>Colazione</b>\n☐ B12", "empty": False}),
        person="owner")
    assert out == "💊 <b>Colazione</b>\n☐ B12"
    assert seen[0].url.path == "/api/persons/owner/agenda/next"
    assert "slot" not in seen[0].url.params


def test_regime_empty_agenda_is_silence(monkeypatch):
    out, _ = _regime(monkeypatch, _ok({"slot": None, "text": "", "empty": True}),
                     person="owner")
    assert out is None


def test_regime_health_down_is_none_never_an_exception(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    out, _ = _regime(monkeypatch, boom, person="owner")
    assert out is None


def test_regime_health_error_status_is_none(monkeypatch):
    out, _ = _regime(monkeypatch, lambda r: httpx.Response(404, json={"detail": "x"}),
                     person="ghost")
    assert out is None


def test_regime_pinned_slot_is_passed_and_checked(monkeypatch):
    out, seen = _regime(monkeypatch, _ok(
        {"slot": "dinner", "text": "💊 Cena", "empty": False}),
        person="owner", slot="dinner")
    assert out == "💊 Cena"
    assert seen[0].url.params["slot"] == "dinner"


def test_regime_refuses_another_slot_than_the_pinned_one(monkeypatch):
    # The wrong pills at the wrong meal: better silent than wrong.
    out, _ = _regime(monkeypatch, _ok(
        {"slot": "lunch", "text": "💊 Pranzo", "empty": False}),
        person="owner", slot="breakfast")
    assert out is None


@pytest.mark.parametrize("person", ["", "../admin", "the owner", "a/b"])
def test_regime_bad_person_never_reaches_health(monkeypatch, person):
    out, seen = _regime(monkeypatch, _ok({"slot": "x", "text": "t", "empty": False}),
                        person=person)
    assert out is None and seen == []


def test_regime_is_a_valid_provider_scoped_to_health():
    assert "regime" in notifications.VALID_PROVIDERS
    assert providers.PROVIDER_PROJECT["regime"] == "health"


def test_regime_validation_requires_a_person_slug():
    errs = notifications.validate(_n("regime", {}))
    assert any("person" in e for e in errs)
    assert notifications.validate(_n("regime", {"person": "owner", "slot": "lunch"})) == []
    assert any("slot" in e for e in notifications.validate(
        _n("regime", {"person": "owner", "slot": "Lunch!"})))
