"""TDD for Task 6: task_card renders the reminder text + tk:* keyboard for an
inbound Scheduler task.due event. See .superpowers/sdd/task-6-brief.md."""

import task_card


def test_card_text_has_time_and_name():
    txt = task_card.render_text({"name": "Cucinare cena", "start_at": "2026-07-04T19:30:00+02:00"})
    assert "Cucinare cena" in txt and "19:30" in txt


def test_card_buttons_encode_st_id():
    kb = task_card.render_keyboard({"scheduled_task_id": 9, "category": "cucina"})
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "tk:open:9" in datas and "tk:done:9" in datas
    # il bottone Apri mostra il nome dello specialista risolto
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Apri" in label for label in labels)


def test_card_buttons_include_postpone():
    kb = task_card.render_keyboard({"scheduled_task_id": 3, "category": None})
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "tk:pp:3" in datas


def test_card_text_missing_start_at_does_not_crash():
    txt = task_card.render_text({"name": "Solo nome"})
    assert "Solo nome" in txt


def test_card_text_malformed_start_at_does_not_crash():
    txt = task_card.render_text({"name": "X", "start_at": "not-a-date"})
    assert "X" in txt
