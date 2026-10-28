"""Gruppo aperto ai suoi membri + archivio di ogni messaggio.

Due cambiamenti nati insieme il 31/08/2026, e collaudati insieme perche' si
reggono a vicenda: il primo allarga CHI puo' parlare agli agenti, il secondo
garantisce che di quel che viene detto resti traccia. Aprire senza archiviare
sarebbe stato il modo piu' rapido di perdere il controllo di entrambi.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import bot
import profiles


# --- Doppi di scena minimi ---------------------------------------------------
# Volutamente non oggetti PTB: qui interessa il contratto letto dagli handler
# (chat.type, chat.id, message_thread_id, from_user...), non la libreria.
def _chat(chat_id=-100999, tipo="supergroup", titolo="ComUnity"):
    return SimpleNamespace(id=chat_id, type=tipo, title=titolo)


def _msg(chat=None, topic=None, testo=None, **kw):
    base = dict(
        chat=chat or _chat(),
        message_thread_id=topic,
        message_id=kw.pop("message_id", 1),
        text=testo,
        caption=kw.pop("caption", None),
        photo=kw.pop("photo", None),
        document=kw.pop("document", None),
        reply_to_message=kw.pop("reply_to_message", None),
        from_user=kw.pop(
            "from_user",
            SimpleNamespace(id=42, full_name="Socio", username="socio"),
        ),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _update(msg, edited=False):
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=msg.chat if msg is not None else None,
        # `_profile` parte da qui: senza, si collauderebbe solo la meta' ospite.
        effective_user=getattr(msg, "from_user", None) if msg is not None else None,
        edited_message=msg if edited else None,
    )


# --- Chi puo' scrivere agli agenti dal gruppo ---------------------------------
def _apri(monkeypatch, progetti=("comunity",), binding=("comunity", "settore-dev")):
    monkeypatch.setattr(bot, "OPEN_GROUP_PROJECTS", set(progetti))
    monkeypatch.setitem(bot.TOPIC_BINDING, (-100999, 6), binding)


def test_membro_di_un_gruppo_aperto_diventa_ospite(monkeypatch):
    _apri(monkeypatch)
    p = bot._group_guest_profile(_update(_msg(topic=6, testo="ciao")))
    assert p is not None
    assert p.is_admin is False
    assert p.projects == ("comunity",)
    assert p.task_card_target in profiles.CARD_TARGETS


def test_ospite_vede_solo_il_progetto_del_suo_gruppo(monkeypatch):
    _apri(monkeypatch)
    p = bot._group_guest_profile(_update(_msg(topic=6, testo="ciao")))
    assert profiles.can_access(p, "comunity") is True
    assert profiles.can_access(p, "casa") is False


def test_topic_senza_binding_resta_chiuso(monkeypatch):
    """Un topic nuovo creato da un membro non risveglia nessun agente."""
    _apri(monkeypatch)
    assert bot._group_guest_profile(_update(_msg(topic=99, testo="ciao"))) is None


def test_progetto_non_dichiarato_aperto_resta_chiuso(monkeypatch):
    """L'apertura e' opt-in: gli altri nodi non la ereditano."""
    _apri(monkeypatch, progetti=("comunity",), binding=("casa", "casa"))
    assert bot._group_guest_profile(_update(_msg(topic=6, testo="ciao"))) is None


def test_nessuna_apertura_configurata_nessun_ospite(monkeypatch):
    """Default deny: a variabile vuota il comportamento e' quello di prima."""
    _apri(monkeypatch, progetti=())
    assert bot._group_guest_profile(_update(_msg(topic=6, testo="ciao"))) is None


def test_chat_privata_non_apre_mai(monkeypatch):
    """Scrivere in privato al bot non e' "essere nel gruppo"."""
    _apri(monkeypatch)
    monkeypatch.setitem(bot.TOPIC_BINDING, (-100999, 0), ("comunity", "settore-dev"))
    priv = _msg(chat=_chat(tipo="private", titolo=None), testo="ciao")
    assert bot._group_guest_profile(_update(priv)) is None


# --- L'archivio di ogni messaggio --------------------------------------------
def _archivia(monkeypatch, tmp_path, update):
    import asyncio

    dest = tmp_path / "messages.jsonl"
    monkeypatch.setattr(bot, "MSG_LOG_PATH", str(dest))
    asyncio.run(bot.archive_message(update, None))
    if not dest.exists():
        return []
    return [json.loads(r) for r in dest.read_text(encoding="utf-8").splitlines()]


def test_archivia_un_messaggio_normale(monkeypatch, tmp_path):
    righe = _archivia(monkeypatch, tmp_path, _update(_msg(topic=6, testo="ciao")))
    assert len(righe) == 1
    r = righe[0]
    assert r["testo"] == "ciao" and r["tipo"] == "testo"
    assert r["chat"] == -100999 and r["topic"] == 6
    assert r["da_id"] == 42 and r["username"] == "socio"


def test_archivia_anche_i_comandi(monkeypatch, tmp_path):
    """Il buco che ha fatto nascere l'archivio: i comandi non lasciavano traccia."""
    righe = _archivia(monkeypatch, tmp_path, _update(_msg(topic=6, testo="/contatta x")))
    assert righe[0]["tipo"] == "comando"
    assert righe[0]["testo"] == "/contatta x"


def test_archivia_i_topic_senza_binding(monkeypatch, tmp_path):
    """L'altra meta' del buco: un topic che non sveglia nessun agente."""
    righe = _archivia(monkeypatch, tmp_path, _update(_msg(topic=99, testo="fra noi")))
    assert righe[0]["topic"] == 99 and righe[0]["testo"] == "fra noi"


def test_una_foto_con_didascalia_conserva_il_testo(monkeypatch, tmp_path):
    m = _msg(topic=6, photo=[object()], caption="il volantino")
    righe = _archivia(monkeypatch, tmp_path, _update(m))
    assert righe[0]["tipo"] == "foto" and righe[0]["testo"] == "il volantino"


def test_i_messaggi_di_servizio_non_si_perdono(monkeypatch, tmp_path):
    """Ingressi e uscite: e' cosi' che si legge chi e' entrato nel gruppo."""
    righe = _archivia(monkeypatch, tmp_path, _update(_msg(topic=None)))
    assert righe[0]["tipo"] == "servizio" and righe[0]["topic"] is None


def test_una_modifica_e_marcata_come_tale(monkeypatch, tmp_path):
    righe = _archivia(monkeypatch, tmp_path, _update(_msg(topic=6, testo="corretto"), edited=True))
    assert righe[0]["modificato"] is True


def test_l_archivio_accoda_non_sovrascrive(monkeypatch, tmp_path):
    import asyncio

    dest = tmp_path / "messages.jsonl"
    monkeypatch.setattr(bot, "MSG_LOG_PATH", str(dest))
    for i, n in enumerate(("uno", "due", "tre"), start=1):
        asyncio.run(bot.archive_message(_update(_msg(topic=6, testo=n, message_id=i)), None))
    assert [json.loads(r)["testo"] for r in dest.read_text(encoding="utf-8").splitlines()] \
        == ["uno", "due", "tre"]


def test_un_update_rotto_non_fa_cadere_il_bot(monkeypatch, tmp_path):
    """Best-effort: un archivio che fa cadere il bot e' peggio di uno incompleto."""
    import asyncio

    dest = tmp_path / "messages.jsonl"
    monkeypatch.setattr(bot, "MSG_LOG_PATH", str(dest))
    rotto = SimpleNamespace(effective_message=SimpleNamespace(), edited_message=None)
    asyncio.run(bot.archive_message(rotto, None))          # non solleva
    asyncio.run(bot.archive_message(_update(None), None))  # nemmeno senza messaggio


def test_senza_data_dir_scrivibile_si_salta(monkeypatch, tmp_path):
    """Sotto test/immagini senza data dir l'handler tace invece di rumoreggiare."""
    import asyncio

    monkeypatch.setattr(bot, "MSG_LOG_PATH", str(tmp_path / "assente" / "messages.jsonl"))
    asyncio.run(bot.archive_message(_update(_msg(topic=6, testo="ciao")), None))
    assert not (tmp_path / "assente").exists()


# --- Rotazione: nessun archivio cresce all'infinito ---------------------------
def test_sotto_soglia_non_ruota(monkeypatch, tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text("x" * 10, encoding="utf-8")
    monkeypatch.setattr(bot, "ARCHIVE_MAX_BYTES", 100)
    bot._ruota_archivio(str(f))
    assert f.exists() and not (tmp_path / "a.jsonl.1").exists()


def test_sopra_soglia_sposta_in_prima_generazione(monkeypatch, tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text("x" * 200, encoding="utf-8")
    monkeypatch.setattr(bot, "ARCHIVE_MAX_BYTES", 100)
    bot._ruota_archivio(str(f))
    assert not f.exists()
    assert (tmp_path / "a.jsonl.1").read_text(encoding="utf-8") == "x" * 200


def test_le_generazioni_scalano_e_la_piu_vecchia_cade(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "ARCHIVE_MAX_BYTES", 100)
    f = tmp_path / "a.jsonl"
    for etichetta in ("uno", "due", "tre", "quattro"):
        f.write_text(etichetta + "x" * 200, encoding="utf-8")
        bot._ruota_archivio(str(f))
    # .1 e' il piu' recente ruotato, .3 il piu' vecchio ancora vivo; "uno" e' caduto.
    assert (tmp_path / "a.jsonl.1").read_text(encoding="utf-8").startswith("quattro")
    assert (tmp_path / "a.jsonl.2").read_text(encoding="utf-8").startswith("tre")
    assert (tmp_path / "a.jsonl.3").read_text(encoding="utf-8").startswith("due")
    assert not (tmp_path / "a.jsonl.4").exists()


def test_file_inesistente_non_e_un_errore(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "ARCHIVE_MAX_BYTES", 1)
    bot._ruota_archivio(str(tmp_path / "mai-scritto.jsonl"))  # non solleva


def test_soglia_a_zero_disattiva_la_rotazione(monkeypatch, tmp_path):
    """Serve a chi vuole l'archivio intero e gestisce la rotazione da fuori."""
    f = tmp_path / "a.jsonl"
    f.write_text("x" * 200, encoding="utf-8")
    monkeypatch.setattr(bot, "ARCHIVE_MAX_BYTES", 0)
    bot._ruota_archivio(str(f))
    assert f.exists()


def test_l_archivio_dei_messaggi_ruota_da_solo(monkeypatch, tmp_path):
    """La rotazione e' agganciata all'append, non a un job esterno."""
    import asyncio

    dest = tmp_path / "messages.jsonl"
    monkeypatch.setattr(bot, "MSG_LOG_PATH", str(dest))
    monkeypatch.setattr(bot, "ARCHIVE_MAX_BYTES", 200)
    for n in range(12):
        asyncio.run(bot.archive_message(
            _update(_msg(topic=6, testo=f"riga {n}", message_id=n + 1)), None))
    assert (tmp_path / "messages.jsonl.1").exists()
    assert dest.stat().st_size < 400  # il file vivo e' ripartito da poco


# --- Cosa risponde a un ospite, e cosa no ------------------------------------
def test_help_risponde_anche_a_un_ospite(monkeypatch):
    """Il primo gesto di chiunque e' /help: il silenzio sarebbe la peggior
    accoglienza, ed e' quello che faceva."""
    _apri(monkeypatch)
    monkeypatch.setattr(bot, "profile_for", lambda uid: None)
    assert bot._profile_or_guest(_update(_msg(topic=6, testo="/help"))) is not None


def test_la_guida_dell_ospite_non_promette_comandi_chiusi(monkeypatch):
    """Elencargli /progetti o /nuovo sarebbe peggio del silenzio: li proverebbe
    e non otterrebbe risposta."""
    for chiuso in ("/progetti", "/nuovo", "/riprendi", "/chiudi", "/dispatch"):
        assert chiuso not in bot.HELP_OSPITE_HTML, chiuso
    for aperto in ("/reset", "/modello", "/help"):
        assert aperto in bot.HELP_OSPITE_HTML, aperto


def test_chiudi_resta_chiuso_a_un_ospite():
    """`/chiudi` scollega la scheda dal progetto: in un topic di settore
    lascerebbe il gruppo senza quell'agente, e il collegamento vive solo in
    state.json. Deve restare dell'amministratore."""
    import inspect

    sorgente = inspect.getsource(bot.cmd_chiudi)
    assert "_allowed(update)" in sorgente
    assert "_profile_or_guest" not in sorgente


def test_la_dashboard_resta_dell_amministratore():
    import inspect

    for f in (bot.cmd_progetti, bot.cmd_nuovo, bot.cmd_riprendi):
        sorgente = inspect.getsource(f)
        assert "_profile_or_guest" not in sorgente, f.__name__


def test_due_bot_nello_stesso_gruppo_archiviano_una_volta_sola(monkeypatch, tmp_path):
    """#64: il bot default e quello del nodo ricevono lo stesso update; una voce sola."""
    import asyncio

    dest = tmp_path / "messages.jsonl"
    monkeypatch.setattr(bot, "MSG_LOG_PATH", str(dest))
    m = _msg(topic=6, testo="ok curry", message_id=69, edit_date=None)
    asyncio.run(bot.archive_message(_update(m), None))  # Application default
    asyncio.run(bot.archive_message(_update(m), None))  # Application del nodo
    assert len(dest.read_text(encoding="utf-8").splitlines()) == 1


def test_una_modifica_dello_stesso_messaggio_resta_una_voce_nuova(monkeypatch, tmp_path):
    import asyncio
    import datetime as dt

    dest = tmp_path / "messages.jsonl"
    monkeypatch.setattr(bot, "MSG_LOG_PATH", str(dest))
    asyncio.run(bot.archive_message(_update(_msg(topic=6, testo="a", message_id=7)), None))
    mod = _msg(topic=6, testo="b", message_id=7, edit_date=dt.datetime(2026, 10, 6, 15, 4))
    asyncio.run(bot.archive_message(_update(mod, edited=True), None))
    asyncio.run(bot.archive_message(_update(mod, edited=True), None))  # secondo bot
    righe = [json.loads(r) for r in dest.read_text(encoding="utf-8").splitlines()]
    assert [r["testo"] for r in righe] == ["a", "b"]
