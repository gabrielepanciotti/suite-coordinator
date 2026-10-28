"""Tests for graph_mailer: silent-only auth, sendMail retries, drafts, app-only.

The real shared token cache is NEVER touched: every config points
token_cache_file at tmp_path, and the MSAL app is a local fake — msal itself
is not even imported.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
import requests
import responses  # pytest dep: responses

import graph_mailer
import local_defaults

# The installation's identity is read from the locale file AT IMPORT (issue #43).
# These tests never use — nor name — the real one: the module is reloaded below
# against this throwaway map, and restored afterwards.
_LOCALE_DI_PROVA = {
    "MICROSOFT_CLIENT_ID": "client-id-di-prova",
    "MICROSOFT_TENANT_ID": "tenant-id-di-prova",
    "MICROSOFT_TOKEN_CACHE_FILE": "/m365/cache-di-prova.json",
    "EMAIL_SENDER": "mailer@example.com",
}
DEFAULT_CLIENT_ID = _LOCALE_DI_PROVA["MICROSOFT_CLIENT_ID"]
DEFAULT_TENANT_ID = _LOCALE_DI_PROVA["MICROSOFT_TENANT_ID"]
DEFAULT_TOKEN_CACHE_FILE = _LOCALE_DI_PROVA["MICROSOFT_TOKEN_CACHE_FILE"]
DEFAULT_SENDER = _LOCALE_DI_PROVA["EMAIL_SENDER"]


@pytest.fixture(autouse=True, scope="module")
def _graph_mailer_con_locale_di_prova(tmp_path_factory):
    """Reload graph_mailer against the throwaway locale, restore it afterwards.

    Module-scoped, so no ``monkeypatch``: env and module dict are put back by hand.
    The restore reuses the same module dict, so objects other test modules
    imported keep pointing at the original values.
    """
    path = tmp_path_factory.mktemp("locale") / "defaults.json"
    path.write_text(json.dumps(_LOCALE_DI_PROVA), encoding="utf-8")
    env_keys = (local_defaults.LOCALE_FILE_ENV, *_LOCALE_DI_PROVA)
    saved_env = {k: os.environ.get(k) for k in env_keys}
    for k in _LOCALE_DI_PROVA:  # env > file: a device var would mask the file
        os.environ.pop(k, None)
    os.environ[local_defaults.LOCALE_FILE_ENV] = str(path)
    local_defaults.clear_cache()
    snapshot = dict(vars(graph_mailer))
    importlib.reload(graph_mailer)
    try:
        yield
    finally:
        vars(graph_mailer).clear()
        vars(graph_mailer).update(snapshot)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        local_defaults.clear_cache()


SEND_URL = f"https://graph.microsoft.com/v1.0/users/{DEFAULT_SENDER}/sendMail"


# --------------------------------------------------------------------- fakes
class FakeCache:
    """Stand-in for msal.SerializableTokenCache."""

    def __init__(self, *, has_state_changed: bool = False, payload: str = '{"AccessToken": {}}'):
        self.has_state_changed = has_state_changed
        self._payload = payload
        self.serialize_calls = 0

    def serialize(self) -> str:
        self.serialize_calls += 1
        return self._payload


class FakeApp:
    """Stand-in for msal.PublicClientApplication (silent flow only)."""

    def __init__(self, *, accounts: list[dict] | None = None, result: dict | None = None):
        self._accounts = [{"username": DEFAULT_SENDER}] if accounts is None else accounts
        self._result = result
        self.silent_calls: list[tuple] = []

    def get_accounts(self) -> list[dict]:
        return list(self._accounts)

    def acquire_token_silent(self, scopes, account=None):
        self.silent_calls.append((tuple(scopes), account))
        return self._result


def make_mailer(
    tmp_path: Path,
    *,
    app: FakeApp | None = None,
    cache: FakeCache | None = None,
    wire: bool = True,
) -> graph_mailer.GraphMailer:
    """A mailer whose cache path is inside tmp_path, optionally pre-wired to fakes."""
    cfg = graph_mailer.MailerConfig(
        client_id="test-client",
        tenant_id="test-tenant",
        token_cache_file=str(tmp_path / "token_cache.json"),
        sender=DEFAULT_SENDER,
    )
    mailer = graph_mailer.GraphMailer(cfg)
    if wire:
        # Bypass _ensure_app: never import msal, never read the real cache.
        mailer._app = app or FakeApp(result={"access_token": "tok"})
        mailer._cache = cache if cache is not None else FakeCache()
    return mailer


# -------------------------------------------------------------------- config
def test_load_config_defaults_to_shared_identity():
    cfg = graph_mailer.load_config(env={})
    assert cfg.client_id == DEFAULT_CLIENT_ID
    assert cfg.tenant_id == DEFAULT_TENANT_ID
    assert cfg.token_cache_file == DEFAULT_TOKEN_CACHE_FILE
    assert cfg.sender == DEFAULT_SENDER
    assert cfg.enabled


def test_load_config_env_overrides():
    cfg = graph_mailer.load_config(
        env={
            "MICROSOFT_CLIENT_ID": " cid ",
            "MICROSOFT_TENANT_ID": "tid",
            "MICROSOFT_TOKEN_CACHE_FILE": "/m365/token_cache.json",
            "EMAIL_SENDER": "altro@example.com",
        }
    )
    assert cfg.client_id == "cid"
    assert cfg.tenant_id == "tid"
    assert cfg.token_cache_file == "/m365/token_cache.json"
    assert cfg.sender == "altro@example.com"


def test_load_config_blank_env_falls_back_to_default():
    """A blank var is a deployment slip, not a request to send from nobody."""
    assert graph_mailer.load_config(env={"EMAIL_SENDER": "   "}).sender == DEFAULT_SENDER


# ---------------------------------------------------------------------- auth
@responses.activate
def test_send_returns_false_when_token_unavailable_without_calling_http(tmp_path):
    app = FakeApp(result={"error": "invalid_grant", "error_description": "expired"})
    mailer = make_mailer(tmp_path, app=app)

    assert mailer.send(to="a@b.it", subject="s", body_html="<p>x</p>") is False
    assert len(responses.calls) == 0  # no HTTP attempted at all
    # Gli scope richiesti sono fissati apposta: MSAL e' tutto-o-niente
    # sull'insieme, quindi chiederne di meno e risalvare la cache la
    # restringe — ed e' cosi' che il 10/09 sono spariti gli scope Chat.* e
    # sono morti in blocco sei consumer. Se questa riga cambia, deve essere
    # una decisione, non un effetto collaterale.
    assert app.silent_calls[0][0] == ("Mail.Send", "Mail.ReadWrite.Shared")


@responses.activate
def test_send_returns_false_when_cache_has_no_account(tmp_path):
    mailer = make_mailer(tmp_path, app=FakeApp(accounts=[]))

    assert mailer.send(to="a@b.it", subject="s", body_html="<p>x</p>") is False
    assert len(responses.calls) == 0


@responses.activate
def test_send_returns_false_when_cache_file_missing(tmp_path):
    """The state before the docker-compose mount exists: clean failure, no login."""
    mailer = make_mailer(tmp_path, wire=False)  # real _ensure_app, file absent

    assert mailer.send(to="a@b.it", subject="s", body_html="<p>x</p>") is False
    assert len(responses.calls) == 0


def test_picks_the_configured_sender_among_several_accounts(tmp_path):
    app = FakeApp(
        accounts=[{"username": "owner@example.com"}, {"username": DEFAULT_SENDER}],
        result={"access_token": "tok"},
    )
    mailer = make_mailer(tmp_path, app=app)
    assert mailer._acquire_token() == "tok"
    assert app.silent_calls[0][1]["username"] == DEFAULT_SENDER


def test_module_never_opens_an_interactive_flow():
    """Guardrail on the absolute rule: silent refresh only, no device code."""
    source = Path(graph_mailer.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "initiate_device_flow",
        "acquire_token_by_device_flow",
        "acquire_token_interactive",
        "acquire_token_by_username_password",
        "ConfidentialClientApplication",
    ):
        assert forbidden not in source


# -------------------------------------------------------------------- upload
@responses.activate
def test_send_ok_on_202(tmp_path):
    responses.add(responses.POST, SEND_URL, status=202)
    mailer = make_mailer(tmp_path)

    ok = mailer.send(
        to="mario.rossi@example.com",
        subject="[work instance Ticket #42] Bolletta",
        body_html="<p>ciao</p>",
        cc=["owner@example.com", "  ", ""],
    )

    assert ok is True
    assert len(responses.calls) == 1
    request = responses.calls[0].request
    assert request.headers["Authorization"] == "Bearer tok"
    body = json.loads(request.body)
    assert body["saveToSentItems"] is True
    assert body["message"]["body"]["contentType"] == "HTML"
    assert body["message"]["toRecipients"] == [
        {"emailAddress": {"address": "mario.rossi@example.com"}}
    ]
    # Blank cc entries are dropped, not sent as empty addresses.
    assert body["message"]["ccRecipients"] == [
        {"emailAddress": {"address": "owner@example.com"}}
    ]


@responses.activate
def test_send_without_recipient_is_refused(tmp_path):
    mailer = make_mailer(tmp_path)
    assert mailer.send(to="   ", subject="s", body_html="<p>x</p>") is False
    assert len(responses.calls) == 0


@responses.activate
def test_send_retries_once_on_429_honouring_retry_after(tmp_path, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(graph_mailer, "_sleep", slept.append)
    responses.add(responses.POST, SEND_URL, status=429, headers={"Retry-After": "2"})
    responses.add(responses.POST, SEND_URL, status=202)

    assert make_mailer(tmp_path).send(to="a@b.it", subject="s", body_html="<p>x</p>") is True
    assert len(responses.calls) == 2
    assert slept == [2.0]


@responses.activate
def test_send_retries_once_on_503_then_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_mailer, "_sleep", lambda _s: None)
    responses.add(responses.POST, SEND_URL, status=503)
    responses.add(responses.POST, SEND_URL, status=503)

    assert make_mailer(tmp_path).send(to="a@b.it", subject="s", body_html="<p>x</p>") is False
    assert len(responses.calls) == 2  # never more than _MAX_ATTEMPTS


@responses.activate
def test_send_does_not_retry_on_400(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_mailer, "_sleep", lambda _s: pytest.fail("must not sleep"))
    responses.add(responses.POST, SEND_URL, status=400, body='{"error":"bad"}')

    assert make_mailer(tmp_path).send(to="a@b.it", subject="s", body_html="<p>x</p>") is False
    assert len(responses.calls) == 1


@responses.activate
def test_send_survives_a_network_error_without_retrying(tmp_path):
    """sendMail is not idempotent: a network error must not risk a double send."""
    responses.add(responses.POST, SEND_URL, body=requests.exceptions.ConnectionError("boom"))

    assert make_mailer(tmp_path).send(to="a@b.it", subject="s", body_html="<p>x</p>") is False
    assert len(responses.calls) == 1


def test_send_never_raises_towards_the_telegram_callback(tmp_path):
    """Even an exploding MSAL layer comes back as a plain False."""

    class ExplodingApp(FakeApp):
        def get_accounts(self):
            raise RuntimeError("corrupt cache")

    mailer = make_mailer(tmp_path, app=ExplodingApp())
    assert mailer.send(to="a@b.it", subject="s", body_html="<p>x</p>") is False


# -------------------------------------------------------------- cache writing
@responses.activate
def test_cache_not_written_when_state_unchanged(tmp_path):
    responses.add(responses.POST, SEND_URL, status=202)
    cache = FakeCache(has_state_changed=False)
    mailer = make_mailer(tmp_path, cache=cache)

    assert mailer.send(to="a@b.it", subject="s", body_html="<p>x</p>") is True
    assert cache.serialize_calls == 0
    assert not (tmp_path / "token_cache.json").exists()


@responses.activate
def test_cache_written_atomically_when_state_changed(tmp_path):
    responses.add(responses.POST, SEND_URL, status=202)
    cache = FakeCache(has_state_changed=True, payload='{"RefreshToken": "rotated"}')
    mailer = make_mailer(tmp_path, cache=cache)

    assert mailer.send(to="a@b.it", subject="s", body_html="<p>x</p>") is True
    target = tmp_path / "token_cache.json"
    assert target.read_text(encoding="utf-8") == '{"RefreshToken": "rotated"}'
    assert cache.serialize_calls == 1
    # The atomic write leaves no debris next to the shared cache.
    assert [p.name for p in tmp_path.iterdir()] == ["token_cache.json"]


def test_cache_persisted_even_when_the_silent_refresh_fails(tmp_path):
    """A rotated refresh token must survive a failed acquisition, not be thrown away."""
    cache = FakeCache(has_state_changed=True)
    mailer = make_mailer(tmp_path, app=FakeApp(result=None), cache=cache)

    assert mailer._acquire_token() is None
    assert (tmp_path / "token_cache.json").exists()


# --------------------------------------------------------------- app-only


def test_config_is_app_only_when_a_secret_is_set():
    cfg = graph_mailer.load_config({"MICROSOFT_CLIENT_SECRET": "s3cr3t"})

    assert cfg.app_only is True
    assert cfg.client_secret == "s3cr3t"


def test_config_stays_delegated_without_a_secret():
    cfg = graph_mailer.load_config({})

    assert cfg.app_only is False


def test_app_only_is_enabled_without_a_token_cache():
    """There is no cache in app-only: requiring one would disable sending."""
    cfg = graph_mailer.load_config(
        {"MICROSOFT_CLIENT_SECRET": "s3cr3t", "MICROSOFT_TOKEN_CACHE_FILE": ""}
    )

    assert cfg.enabled is True


def test_delegated_still_needs_the_shared_cache():
    cfg = graph_mailer.MailerConfig(
        client_id="c", tenant_id="t", token_cache_file="", sender="a@b.it"
    )

    assert cfg.enabled is False


def test_app_only_token_comes_from_the_shared_library(monkeypatch):
    class FakeAuth:
        ultimo_errore = None

        def token(self):
            return "tok-app"

    seen = {}

    def fake_app_auth(**kwargs):
        seen.update(kwargs)
        return FakeAuth()

    monkeypatch.setattr(graph_mailer, "app_auth", fake_app_auth)
    mailer = graph_mailer.GraphMailer(graph_mailer.load_config({"MICROSOFT_CLIENT_SECRET": "s3cr3t"}))

    assert mailer._acquire_token() == "tok-app"
    assert seen["client_secret"] == "s3cr3t"


def test_app_only_never_touches_the_shared_cache(monkeypatch, tmp_path):
    """No device-code path in app-only: the cache of the other projects stays put."""
    cache = tmp_path / "token_cache.json"
    cache.write_text("token-di-una-persona")

    class FakeAuth:
        ultimo_errore = None

        def token(self):
            return "tok-app"

    monkeypatch.setattr(graph_mailer, "app_auth", lambda **kw: FakeAuth())
    monkeypatch.setattr(
        graph_mailer.GraphMailer, "_ensure_app",
        lambda self: (_ for _ in ()).throw(AssertionError("device code path taken")),
    )
    cfg = graph_mailer.load_config(
        {"MICROSOFT_CLIENT_SECRET": "s3cr3t", "MICROSOFT_TOKEN_CACHE_FILE": str(cache)}
    )

    assert graph_mailer.GraphMailer(cfg)._acquire_token() == "tok-app"
    assert cache.read_text() == "token-di-una-persona"


def test_app_only_failure_is_logged_with_its_reason(monkeypatch, caplog):
    class FakeAuth:
        ultimo_errore = "AADSTS7000215: secret scaduto"

        def token(self):
            return None

    monkeypatch.setattr(graph_mailer, "app_auth", lambda **kw: FakeAuth())
    mailer = graph_mailer.GraphMailer(graph_mailer.load_config({"MICROSOFT_CLIENT_SECRET": "s"}))

    with caplog.at_level("ERROR"):
        assert mailer._acquire_token() is None

    assert "AADSTS7000215" in caplog.text


# --- create_draft (#24) -----------------------------------------------------
#
# Una preparazione non deve mai poter diventare un atto irreversibile verso
# l'esterno: e' il vincolo di progetto dell'azione `email_draft` di #20, e il
# motivo per cui qui si verifica anche cio' che NON deve succedere.

DRAFT_URL = f"https://graph.microsoft.com/v1.0/users/{DEFAULT_SENDER}/messages"


@responses.activate
def test_create_draft_posta_su_messages_e_non_invia(tmp_path):
    responses.add(responses.POST, DRAFT_URL, status=201, json={"id": "AAA"})
    mailer = make_mailer(tmp_path)

    assert mailer.create_draft(to="a@b.it", subject="s", body_html="<p>x</p>") is True

    (chiamata,) = responses.calls
    assert chiamata.request.url == DRAFT_URL
    corpo = json.loads(chiamata.request.body)
    # Il messaggio e' nudo: niente involucro `message`, niente saveToSentItems.
    assert corpo["subject"] == "s"
    assert corpo["toRecipients"] == [{"emailAddress": {"address": "a@b.it"}}]
    assert "saveToSentItems" not in corpo and "message" not in corpo


@responses.activate
def test_create_draft_non_chiama_mai_sendmail(tmp_path):
    """Il ripiego su send() e' escluso per progetto, anche quando fallisce."""
    responses.add(responses.POST, DRAFT_URL, status=403, json={"error": "no"})
    mailer = make_mailer(tmp_path)

    assert mailer.create_draft(to="a@b.it", subject="s", body_html="<p>x</p>") is False

    assert all(SEND_URL not in c.request.url for c in responses.calls)


@responses.activate
def test_create_draft_senza_destinatario_non_tocca_la_rete(tmp_path):
    assert make_mailer(tmp_path).create_draft(to="  ", subject="s", body_html="x") is False
    assert len(responses.calls) == 0


@responses.activate
def test_create_draft_ritenta_una_volta_sola_su_429(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_mailer, "_sleep", lambda _s: None)
    responses.add(responses.POST, DRAFT_URL, status=429, headers={"Retry-After": "1"})
    responses.add(responses.POST, DRAFT_URL, status=201, json={"id": "AAA"})

    assert make_mailer(tmp_path).create_draft(
        to="a@b.it", subject="s", body_html="x"
    ) is True
    assert len(responses.calls) == 2


@responses.activate
def test_create_draft_non_solleva_mai(tmp_path, monkeypatch):
    """Il chiamante e' una callback Telegram: deve sopravvivere a tutto."""
    mailer = make_mailer(tmp_path)

    def esplode(*a, **k):
        raise RuntimeError("imprevisto")

    monkeypatch.setattr(mailer, "_create_draft", esplode)
    assert mailer.create_draft(to="a@b.it", subject="s", body_html="x") is False


@responses.activate
def test_senza_token_la_bozza_fallisce_senza_rete(tmp_path):
    app = FakeApp(result={"error": "invalid_grant"})
    assert make_mailer(tmp_path, app=app).create_draft(
        to="a@b.it", subject="s", body_html="x"
    ) is False
    assert len(responses.calls) == 0


# --- allargamento degli scope e ripiego (#24) --------------------------------
#
# Aggiungere uno scope a un modulo che parla con msal DIRETTAMENTE e' la
# manovra che il 10/09 ha ucciso sei consumer in blocco: MSAL e' tutto-o-niente
# sull'insieme richiesto, quindi un sottoinsieme risalvato restringe il refresh
# token condiviso da tutto l'ecosistema. Qui si verificano le due meta' della
# regola, che servono a cose opposte.


class AppConScope:
    """FakeApp che concede il token solo per certi insiemi di scope."""

    def __init__(self, concessi: set[str]):
        self._concessi = concessi
        self.silent_calls: list[tuple] = []

    def get_accounts(self):
        return [{"username": DEFAULT_SENDER}]

    def acquire_token_silent(self, scopes, account=None):
        self.silent_calls.append((tuple(scopes), account))
        if set(scopes) <= self._concessi:
            return {"access_token": "tok"}
        return {"error": "invalid_grant", "error_description": "scope non concesso"}


def test_si_chiede_sempre_l_unione_per_prima(tmp_path):
    """Chiedere meno del dovuto e risalvare e' come si restringe la cache."""
    app = AppConScope({"Mail.Send", "Mail.ReadWrite.Shared"})
    mailer = make_mailer(tmp_path, app=app)

    assert mailer._acquire_token(minimi=graph_mailer._SCOPE_INVIO) == "tok"
    assert app.silent_calls[0][0] == tuple(graph_mailer._SCOPES)
    assert len(app.silent_calls) == 1  # riuscita al primo colpo: nessun ripiego


def test_se_l_allargamento_fallisce_l_invio_continua_a_funzionare(tmp_path):
    """Una funzione nuova (le bozze) non deve poter rompere una vecchia."""
    app = AppConScope({"Mail.Send"})  # Mail.ReadWrite.Shared revocato altrove
    mailer = make_mailer(tmp_path, app=app)

    assert mailer._acquire_token(minimi=graph_mailer._SCOPE_INVIO) == "tok"
    assert [c[0] for c in app.silent_calls] == [
        tuple(graph_mailer._SCOPES),
        tuple(graph_mailer._SCOPE_INVIO),
    ]


def test_il_ripiego_non_risalva_mai_una_cache_piu_povera(tmp_path):
    app = AppConScope({"Mail.Send"})
    cache = FakeCache(has_state_changed=True)
    mailer = make_mailer(tmp_path, app=app, cache=cache)

    mailer._acquire_token(minimi=graph_mailer._SCOPE_INVIO)

    # Una sola serializzazione: quella del tentativo con l'unione. Il ripiego
    # non ne aggiunge un'altra, o l'insieme piu' povero diventerebbe di tutti.
    assert cache.serialize_calls == 1


def test_senza_lo_scope_delle_bozze_la_bozza_fallisce_e_non_invia(tmp_path):
    """Il ripiego NON deve far passare un'operazione che non e' autorizzata."""
    app = AppConScope({"Mail.Send"})
    mailer = make_mailer(tmp_path, app=app)

    with responses.RequestsMock() as rsps:
        assert mailer.create_draft(
            to="a@b.it", subject="s", body_html="x"
        ) is False
        assert len(rsps.calls) == 0


def test_app_auth_factory_is_resolved_from_the_installation():
    """The engine names no auth library: GRAPH_APP_AUTH does (#43)."""
    assert graph_mailer._load_app_auth("json:loads") is __import__("json").loads


@pytest.mark.parametrize("spec", ["", "no-colon", "missing_module_xyz:f", "json:missing_attr"])
def test_app_auth_unset_or_unimportable_is_off_not_a_crash(spec):
    assert graph_mailer._load_app_auth(spec) is None


def test_app_only_without_a_factory_stays_off(monkeypatch, caplog):
    monkeypatch.setattr(graph_mailer, "app_auth", None)
    mailer = graph_mailer.GraphMailer(graph_mailer.load_config({"MICROSOFT_CLIENT_SECRET": "s"}))

    with caplog.at_level("ERROR"):
        assert mailer._acquire_token() is None

    assert "GRAPH_APP_AUTH" in caplog.text
