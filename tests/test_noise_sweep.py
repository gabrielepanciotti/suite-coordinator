import time

import message_guard as mg


def test_service_messages_are_noise_and_useful_ones_are_not():
    for t in ("❌ opencode: errore", "⏳ sto pensando…", "<b>⚠️ Attenzione</b>", "Nessun output.",
              "a member, l'allegato non mi è arrivato", "🔒 Questa chat non appartiene a un'istanza"):
        assert mg.is_noise(t), t
    for t in ("📘 La lezione di Anatomia è pronta", "💊 <b>Pranzo</b>", "Ecco la spiegazione…"):
        assert not mg.is_noise(t), t


def test_due_noise_only_after_24h(tmp_path):
    mg.configure(str(tmp_path / "sent.jsonl"))
    mg._record(1, 0, 10, "❌ vecchio")
    mg._record(1, 0, 11, "📘 utile")
    mg._record(1, 0, 12, "❌ nuovo")
    rows = mg._read_sent()
    rows[0]["ts"] -= 25 * 3600
    rows[1]["ts"] -= 25 * 3600
    mg._write_sent(rows)
    assert [r["id"] for r in mg.due_noise()] == [10]
    mg.forget(1, [10])
    assert mg.due_noise() == []
