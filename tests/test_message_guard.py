import asyncio

import pytest

import message_guard as mg


@pytest.fixture(autouse=True)
def _clean():
    mg._recent.clear()
    mg.stats.clear()


@pytest.mark.parametrize("text", [
    "", "   \n ", "​​", "<b></b>", "<b> </b>&nbsp;", "...", "-", "?!", "----",
    "None", "null", "undefined", "{}", "[]", "(no output)", "N/A",
    "Ciao {nome}, domani %s", "valore %(x)s", "${HOME}",
    "a" * 40, "ok\nok\nok\nok\nok\nok",
    None, 5,
])
def test_blocca(text):
    assert mg.inspect(text)[0] is None


@pytest.mark.parametrize("text", [
    "👍", "Ok", "Sì", "Fatto.", "Il costo è {x} nel template, vedi la nota: " + "x " * 120,
    "```\nprint('{nome}')\n```", "<b>Fatto</b>", "a\nb\nc\nd\ne\nf",
])
def test_lascia_passare(text):
    assert mg.inspect(text)[0] == text


def test_separatori_box_drawing_passano_ma_run_normali_no():
    separator = "─" * 74
    assert mg.inspect(separator)[0] == separator
    assert mg.inspect("K" * 40) == (None, "carattere ripetuto")


def test_marker_silenzio_tolto():
    assert mg.inspect(f"Fatto.\n{mg.NO_REPLY_MARKER}")[0] == "Fatto."
    assert mg.inspect(mg.NO_REPLY_MARKER)[0] is None


def test_duplicato_stessa_scheda_in_finestra():
    t = "Il destinatario non ha risposto"
    assert mg.check(1, 7, t, now=100)[0] == t
    assert mg.check(1, 7, t, now=110)[0] is None
    assert mg.check(1, 8, t, now=110)[0] == t  # altra scheda
    # la finestra scorre: una raffica resta bloccata finche' dura, poi si riapre
    assert mg.check(1, 7, t, now=110 + mg.DUP_WINDOW_SEC + 1)[0] == t


def test_testi_brevi_ripetuti_ok():
    assert mg.check(1, 7, "Ok", now=1)[0] == "Ok"
    assert mg.check(1, 7, "Ok", now=2)[0] == "Ok"


def test_install_avvolge_e_scarta():
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)
            return "msg"

    mg.install(FakeBot)
    mg.install(FakeBot)  # idempotente
    b = FakeBot()
    assert asyncio.run(b.send_message(1, "")) is None
    assert asyncio.run(b.send_message(1, "Ciao a tutti", message_thread_id=3)) == "msg"
    assert sent == ["Ciao a tutti"]
    assert mg.stats == {"vuoto": 1}


def test_registro_e_find_bad(tmp_path):
    mg.configure(str(tmp_path / "sent.jsonl"))
    try:
        mg._record(1, 5, 10, "Risposta vera e utile")
        mg._record(1, 5, 11, "valore %s")          # segnaposto: regola di oggi
        mg._record(1, 5, 12, "Stesso testo doppio")
        mg._record(1, 5, 13, "Stesso testo doppio")
        mg._record(2, 5, 14, "None")               # altra chat
        bad = mg.find_bad(1)
        assert [r["id"] for r in bad] == [11, 13]
        assert mg.find_bad(1, now=__import__("time").time() + mg.DELETE_WINDOW_SEC + 5) == []
        mg.forget(1, [11, 13])
        assert mg.find_bad(1) == []
        assert [r["id"] for r in mg._read_sent()] == [10, 12, 14]
    finally:
        mg.configure(None)


def test_pending_si_consuma_una_volta():
    t = mg.stash_pending(1, [3, 4])
    assert mg.take_pending(t) == (1, [3, 4])
    assert mg.take_pending(t) is None


def test_install_registra_gli_invii(tmp_path):
    mg.configure(str(tmp_path / "sent.jsonl"))

    class Msg:
        message_id = 77

    class FakeBot2:
        async def send_message(self, chat_id, text, **kw):
            return Msg()

    try:
        mg.install(FakeBot2)
        asyncio.run(FakeBot2().send_message(9, "Ciao a tutti", message_thread_id=4))
        row = mg._read_sent()[0]
        assert (row["chat"], row["thread"], row["id"], row["text"]) == (9, 4, 77, "Ciao a tutti")
    finally:
        mg.configure(None)


def test_marker_silenzio_senza_parentesi():
    assert mg.inspect("NESSUNA_RISPOSTA")[0] is None
    assert mg.inspect("[[NESSUNA_RISPOSTA]]")[0] is None
