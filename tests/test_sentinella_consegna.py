"""L'allarme deve poter USCIRE.

Il difetto che questi test bloccano non e' un crash: e' una notifica costruita
bene, schedulata, e mai consegnata perche' una policy la vieta in silenzio. E'
lo stato `blocked` di notif_health — quello in cui sono rimaste mute quattordici
notifiche per un mese.

Le chiavi ammesse della sentinella sono un default dell'installazione
(`NOTIFICATION_ALLOWLIST_EXTRA` in locale/defaults.json, issue #43): qui se ne
usa uno di prova, e il modulo viene ricaricato perche' la allowlist di default
si compone all'import.
"""
import importlib

import pytest

import notifications
import projects
from project_model import Project

_EXTRA = ["acme-sentinella-prod", "acme-sentinella-riepilogo"]


@pytest.fixture
def allowlist_di_prova(locale_defaults, monkeypatch):
    """notifications ricaricato con una allowlist extra di prova, poi ripristinato."""
    monkeypatch.delenv(notifications.ALLOWLIST_ENV, raising=False)
    locale_defaults({"NOTIFICATION_ALLOWLIST_EXTRA": _EXTRA})
    # "acme" e' un progetto di lavoro: e' il caso in cui la policy vieta.
    acme = Project(key="acme", label="Acme", emoji="", host_path="", workspace="", kind="work")
    monkeypatch.setattr(projects, "resolve_project",
                        lambda token: acme if token == "acme" else None)

    snapshot = dict(vars(notifications))
    importlib.reload(notifications)
    yield notifications
    # Restore the very objects other modules imported (same dict, old values).
    vars(notifications).clear()
    vars(notifications).update(snapshot)


def _notif(mod, key: str):
    return mod.Notification(key=key, title=key, provider="sentinella",
                            schedule="*/5 * * * *", recipients=("owner",))


def test_la_allowlist_di_default_include_gli_extra_locali(allowlist_di_prova):
    assert set(_EXTRA) <= allowlist_di_prova.NOTIFICATION_ALLOWLIST


def test_allarme_sentinella_non_e_bandito(allowlist_di_prova):
    mod = allowlist_di_prova
    assert mod.is_banned(_notif(mod, "acme-sentinella-prod")) is False


def test_riepilogo_sentinella_non_e_bandito(allowlist_di_prova):
    mod = allowlist_di_prova
    assert mod.is_banned(_notif(mod, "acme-sentinella-riepilogo")) is False


def test_le_altre_notifiche_work_restano_bandite(allowlist_di_prova):
    """La policy del 2026-07-06 resta il default: l'eccezione e' esplicita."""
    mod = allowlist_di_prova
    assert mod.is_banned(_notif(mod, "acme-report-settimanale")) is True


def test_la_sentinella_e_un_provider_valido():
    assert "sentinella" in notifications.VALID_PROVIDERS
