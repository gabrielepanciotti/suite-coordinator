# tests/test_bot_views.py
import textwrap

import bot
import projects
from profiles import Profile

ADMIN = Profile("owner", "Owner", True, ())
MEMBER = Profile("member", "a member", False, ("x",))


def _project(tmp_path):
    d = tmp_path / ".claude" / "agents"; d.mkdir(parents=True)
    (d / "menu-planner.md").write_text(
        "---\nname: menu-planner\ndescription: Plans the weekly batch-cook menu.\n"
        "label: Menu settimanale\nemoji: \U0001F4C5\n---\nbody\n", encoding="utf-8")
    (d / "pantry-keeper.md").write_text(
        "---\nname: pantry-keeper\ndescription: Updates the pantry from a screenshot.\n"
        "label: Aggiorna dispensa\nemoji: \U0001F4F8\n---\nbody\n", encoding="utf-8")
    (tmp_path / ".claude" / "functionalities.toml").write_text(textwrap.dedent("""
        [[functionality]]
        key = "menu-settimana"
        label = "\U0001F4C5 Menu della settimana"
        agent = "menu-planner"
        seed = "[CMD:/menu]"
    """), encoding="utf-8")
    return projects.Project("x", "\U0001F373 X", "\U0001F373", "", str(tmp_path))


def test_choose_new_view_matrix():
    assert bot.choose_new_view(False, False, "new") == "raw"   # no file -> legacy
    assert bot.choose_new_view(False, True, "adv") == "raw"
    assert bot.choose_new_view(True, False, "new") == "func"
    assert bot.choose_new_view(True, False, "adv") == "func"   # non-admin can't force raw
    assert bot.choose_new_view(True, True, "new") == "func"
    assert bot.choose_new_view(True, True, "adv") == "raw"     # admin advanced


def _texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_kb_functionalities_buttons_and_toggle(tmp_path):
    p = _project(tmp_path)
    kb_admin = bot._kb_new_functionalities(p, show_toggle=True)
    assert "\U0001F4C5 Menu della settimana" in _texts(kb_admin)
    assert "act:fnew:x:menu-settimana" in _cbs(kb_admin)
    assert "\U0001F527 Vista avanzata" in _texts(kb_admin)
    kb_member = bot._kb_new_functionalities(p, show_toggle=False)
    assert "\U0001F527 Vista avanzata" not in _texts(kb_member)


def test_kb_raw_uses_human_labels_and_toggle(tmp_path):
    p = _project(tmp_path)
    kb = bot._kb_new_raw(p, with_toggle=True)
    texts = _texts(kb)
    assert "\U0001F4C5 Menu settimanale" in texts       # agent_button_label
    assert "\U0001F4F8 Aggiorna dispensa" in texts
    assert "\U0001F4AC Generale" in texts                # GENERALE annotated
    assert "\U0001FA84 Vista semplice" in texts


def test_new_view_picks_func_or_raw(tmp_path):
    p = _project(tmp_path)
    _text_member, kb_member = bot._new_view(p, MEMBER, "new")
    assert "act:fnew:x:menu-settimana" in _cbs(kb_member)
    _text_adv, kb_adv = bot._new_view(p, ADMIN, "adv")
    assert any(cb.startswith("act:new:x:") for cb in _cbs(kb_adv))  # raw agent buttons
