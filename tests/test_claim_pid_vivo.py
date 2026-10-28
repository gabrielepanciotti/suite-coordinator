"""Un watcher ucciso smette di zittire il worker headless nello stesso istante.

Il modo tipico in cui `hub_watch.py` muore e' l'uccisione dall'esterno: nessun
handler gira, il claim resta indietro con un timestamp fresco, e per tutto il
WATCH_TTL l'anti-gemello crede che qualcuno stia ascoltando. Proprio nei
secondi in cui il worker headless era l'unico rimasto (issue #36).
"""
from __future__ import annotations

import json
import os
import socket
import time

import hub_store


def _scrivi_claim(tmp_path, monkeypatch, **campi):
    monkeypatch.setattr(hub_store, "CLAIMS_DIR", str(tmp_path))
    rec = {"source": "watch", "ts": time.time(), "host": socket.gethostname()}
    rec.update(campi)
    (tmp_path / "acme__db-dev-linux.json").write_text(json.dumps(rec), encoding="utf-8")


def test_un_pid_vivo_e_un_ascolto(tmp_path, monkeypatch):
    _scrivi_claim(tmp_path, monkeypatch, pid=os.getpid())
    assert hub_store.is_watched("acme", "db-dev-linux") is True


def test_un_pid_morto_non_lo_e_anche_col_timestamp_fresco(tmp_path, monkeypatch):
    """E' il caso che conta: ucciso un secondo fa, claim freschissimo."""
    _scrivi_claim(tmp_path, monkeypatch, pid=_pid_inesistente())
    assert hub_store.is_watched("acme", "db-dev-linux") is False


def test_un_claim_di_un_altro_box_ricade_sul_timestamp(tmp_path, monkeypatch):
    """Li' il pid non significa niente: l'unica misura che resta e' l'eta'."""
    _scrivi_claim(tmp_path, monkeypatch, pid=_pid_inesistente(), host="un-altro-host")
    assert hub_store.is_watched("acme", "db-dev-linux") is True

    _scrivi_claim(
        tmp_path, monkeypatch, pid=_pid_inesistente(), host="un-altro-host",
        ts=time.time() - hub_store.WATCH_TTL - 1,
    )
    assert hub_store.is_watched("acme", "db-dev-linux") is False


def test_un_claim_senza_pid_ricade_sul_timestamp(tmp_path, monkeypatch):
    _scrivi_claim(tmp_path, monkeypatch)
    assert hub_store.is_watched("acme", "db-dev-linux") is True


def test_il_claim_del_solo_hook_non_e_mai_un_ascolto(tmp_path, monkeypatch):
    """Regola preesistente, che il controllo sul pid non deve indebolire."""
    _scrivi_claim(tmp_path, monkeypatch, pid=os.getpid(), source="hook")
    assert hub_store.is_watched("acme", "db-dev-linux") is False


def _pid_inesistente() -> int:
    # Probed through hub_store, never os.kill: on Windows os.kill(pid, 0) is
    # TerminateProcess, so the search itself could kill whatever it found.
    # Multiple of 4 on purpose: Windows pids are, so this one is really absent.
    pid = 2**22 - 4  # oltre il pid_max tipico di Linux
    while hub_store._processo_vivo({"pid": pid}) is not False:
        pid -= 4
    return pid


def test_chi_muore_non_ritira_il_claim_di_un_altro(tmp_path, monkeypatch):
    """Visto succedere: due watcher sullo stesso nome, ne muore uno, e il
    claim del vivo sparisce — cioe' l'agente risulta libero mentre qualcuno lo
    sta ascoltando, che e' l'invito a svegliare un gemello headless."""
    _scrivi_claim(tmp_path, monkeypatch, pid=4242)
    percorso = tmp_path / "acme__db-dev-linux.json"

    hub_store.release("acme", "db-dev-linux", only_pid=9999)
    assert percorso.exists(), "il claim di un altro processo non si tocca"

    hub_store.release("acme", "db-dev-linux", only_pid=4242)
    assert not percorso.exists()


def test_senza_pid_il_rilascio_resta_incondizionato(tmp_path, monkeypatch):
    """L'hook di fine sessione non ha un pid da confrontare: deve poter pulire."""
    _scrivi_claim(tmp_path, monkeypatch, pid=4242)
    hub_store.release("acme", "db-dev-linux")
    assert not (tmp_path / "acme__db-dev-linux.json").exists()
