import bot
from notifications import Notification


def test_notif_line_shows_status_and_title():
    on = Notification("a", "Ciao", "static", "0 8 * * *", ("owner",), True, {"text": "x"})
    off = Notification("b", "Muto", "static", "0 8 * * *", ("owner",), False, {"text": "x"})
    assert "✅" in bot._notif_line(on) and "Ciao" in bot._notif_line(on)
    assert "🔕" in bot._notif_line(off)


def test_wizard_recipients_toggle_and_keys():
    draft = {"provider": "agent", "params": {}, "recipients": set()}
    bot._wizard_toggle_recipient(draft, "member")
    assert draft["recipients"] == {"member"}
    bot._wizard_toggle_recipient(draft, "member")               # toggle off
    assert draft["recipients"] == set()


def test_wizard_build_notification_generates_unique_key():
    draft = {"provider": "static", "params": {"text": "ciao"},
             "schedule": "0 8 * * *", "recipients": {"owner"}, "title": "Test"}
    n = bot._wizard_build(draft, existing_keys={"static"})
    assert n.key == "static-2" and n.provider == "static"
    assert n.recipients == ("owner",) and n.enabled is True
