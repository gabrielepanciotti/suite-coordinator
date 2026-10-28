# tests/test_notif_sync.py
import pytest
import manifest
import notif_sync
import notif_store
import projects
from manifest import NotificationDecl


def _decl(**kw):
    base = dict(key="riepilogo", title="Riepilogo", provider="agent",
                schedule="0 8 * * *", to="group", prompt="riassumi",
                text="", project="casa", enabled=True)
    base.update(kw)
    return NotificationDecl(**base)


def test_decl_to_notification_agent_group():
    n = notif_sync.decl_to_notification(_decl())
    assert n.key == "riepilogo"
    assert n.provider == "agent"
    assert n.destination == "group"
    assert n.params == {"project": "casa", "prompt": "riassumi"}
    assert n.recipients == ()          # group destination needs no profiles


def test_decl_to_notification_static_dm():
    n = notif_sync.decl_to_notification(
        _decl(provider="static", to="dm", text="ciao", prompt=""))
    assert n.destination == "dm"
    assert n.params == {"text": "ciao"}


def test_destination_override_replaces_group(monkeypatch):
    # "group" only resolves if that project's manifest carries a group_chat_id;
    # most personal nodes have none, so the notification used to resolve to no
    # target and disappear silently. The device override gives it a real chat.
    env = {notif_sync.DEST_OVERRIDE_ENV: "chat:-1009"}
    assert notif_sync.destination_for(_decl(to="group"), env) == "chat:-1009"
    assert notif_sync.destination_for(_decl(to="dm"), env) == "chat:-1009"


def test_destination_override_leaves_explicit_chat_alone():
    env = {notif_sync.DEST_OVERRIDE_ENV: "chat:-1009"}
    assert notif_sync.destination_for(_decl(to="chat:-42:7"), env) == "chat:-42:7"


def test_destination_without_override_is_the_declared_one():
    assert notif_sync.destination_for(_decl(to="group"), {}) == "group"


def test_decl_to_notification_applies_override(monkeypatch):
    # The regression this guards: a store row hand-pointed at the report chat is
    # reset to "group" by the next startup sync, and the notification goes quiet.
    monkeypatch.setenv(notif_sync.DEST_OVERRIDE_ENV, "chat:-1009")
    assert notif_sync.decl_to_notification(_decl(to="group")).destination == "chat:-1009"


def test_sync_upserts_and_applies(monkeypatch):
    saved, applied = [], []
    p = projects.Project("casa", "Casa", "\U0001F3E0", "", "/ws")
    monkeypatch.setattr(projects, "list_projects", lambda: [p])
    monkeypatch.setattr(manifest, "notifications_of", lambda pr: [_decl()])
    monkeypatch.setattr(notif_store, "upsert", lambda n: saved.append(n.key))
    monkeypatch.setattr(notif_sync.scheduler, "apply", lambda n: applied.append(n.key))
    notif_sync.sync_from_manifests()
    assert saved == ["riepilogo"] and applied == ["riepilogo"]


def test_sync_skips_personal_projects(monkeypatch):
    saved, applied = [], []
    work = projects.Project("offerte", "Hub", "\U0001F4E6", "", "/ws", kind="work")
    personal = projects.Project("personalChef", "Toni", "\U0001F373", "", "/ws2", kind="personal")
    monkeypatch.setattr(projects, "list_projects", lambda: [work, personal])
    monkeypatch.setattr(manifest, "notifications_of",
                        lambda pr: [_decl(project=pr.key)])
    monkeypatch.setattr(notif_store, "upsert", lambda n: saved.append(n.params.get("project")))
    monkeypatch.setattr(notif_sync.scheduler, "apply", lambda n: applied.append(n.key))
    notif_sync.sync_from_manifests()
    assert saved == ["offerte"]          # personal personalChef skipped, work synced
    assert applied == ["riepilogo"]


# --- recipients: a manifest notification aimed at named profiles' DMs ---------

import logging  # noqa: E402

import notifications  # noqa: E402


def test_dm_with_profile_recipients_resolves_to_their_ids(monkeypatch):
    monkeypatch.setenv("PROFILE_OWNER_IDS", "123")
    monkeypatch.delenv(notif_sync.DEST_OVERRIDE_ENV, raising=False)
    n = notif_sync.decl_to_notification(_decl(to="dm", recipients=("owner",)))
    assert n is not None
    assert n.destination == "dm" and n.recipients == ("owner",)
    assert notifications.resolve_targets(n) == [(123, None)]
    assert notifications.NO_RECIPIENTS_MSG not in notifications.validate(n)


def test_dm_recipients_are_not_rerouted_by_the_device_override(monkeypatch):
    # The override exists for decls that cannot name a person. One that does
    # must reach that person, never the override chat (which may be a group).
    monkeypatch.setenv("PROFILE_OWNER_IDS", "123")
    monkeypatch.setenv(notif_sync.DEST_OVERRIDE_ENV, "chat:-1009")
    n = notif_sync.decl_to_notification(_decl(to="dm", recipients=("owner",)))
    assert n.destination == "dm"
    assert notifications.resolve_targets(n) == [(123, None)]


def test_unknown_profile_recipient_is_skipped_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("PROFILE_OWNER_IDS", "123")
    with caplog.at_level(logging.WARNING, logger="coordinator.notif_sync"):
        n = notif_sync.decl_to_notification(_decl(to="dm", recipients=("nessuno",)))
    assert n is None
    assert "nessuno" in caplog.text


def test_profile_without_ids_is_skipped_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("PROFILE_OWNER_IDS", "123")
    monkeypatch.delenv("PROFILE_MEMBER_IDS", raising=False)
    with caplog.at_level(logging.WARNING, logger="coordinator.notif_sync"):
        n = notif_sync.decl_to_notification(_decl(to="dm", recipients=("member",)))
    assert n is None
    assert "member" in caplog.text


def test_sync_skips_a_decl_with_bad_recipients_never_the_group(monkeypatch):
    saved, applied = [], []
    p = projects.Project("casa", "Casa", "\U0001F3E0", "", "/ws")
    monkeypatch.setenv("PROFILE_OWNER_IDS", "123")
    monkeypatch.setattr(projects, "list_projects", lambda: [p])
    monkeypatch.setattr(manifest, "notifications_of",
                        lambda pr: [_decl(key="ok"),
                                    _decl(key="bad", to="dm", recipients=("x",))])
    monkeypatch.setattr(notif_store, "get", lambda k: None)
    monkeypatch.setattr(notif_store, "upsert", lambda n: saved.append(n.key))
    monkeypatch.setattr(notif_sync.scheduler, "apply", lambda n: applied.append(n.key))
    notif_sync.sync_from_manifests()
    assert saved == ["ok"] and applied == ["ok"]


def test_no_recipients_is_unchanged():
    n = notif_sync.decl_to_notification(_decl(to="group"))
    assert n.recipients == () and n.destination == "group"


def test_manifest_parses_notification_recipients(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "coordinator.toml").write_text(
        '[[notification]]\nkey = "s"\nprovider = "static"\nschedule = "0 8 * * *"\n'
        'to = "dm"\ntext = "x"\nrecipients = ["owner", " ", "member"]\n\n'
        '[[notification]]\nkey = "t"\nprovider = "static"\nschedule = "0 8 * * *"\n'
        'text = "y"\n',
        encoding="utf-8",
    )
    p = projects.Project("q", "q", "", "/h", str(tmp_path))
    decls = manifest.load(p).notifications
    assert decls[0].recipients == ("owner", "member")
    assert decls[1].recipients == ()


@pytest.mark.parametrize("to", ["group", "chat:-1009", "GROUP"])
def test_recipients_with_non_dm_destination_is_skipped(monkeypatch, caplog, to):
    monkeypatch.setenv("PROFILE_OWNER_IDS", "123")
    with caplog.at_level(logging.WARNING, logger="coordinator.notif_sync"):
        n = notif_sync.decl_to_notification(_decl(to=to, recipients=("owner",)))
    assert n is None
    assert "recipients require" in caplog.text


# --- notify_in_group: a project with a real group opts out of the override ---

import logging as _logging  # noqa: E402


def _stub_project(monkeypatch, tg):
    monkeypatch.setattr(projects, "resolve_project", lambda name: object())
    monkeypatch.setattr(manifest, "telegram_of", lambda proj: tg)


def test_notify_in_group_bypasses_override(monkeypatch):
    _stub_project(monkeypatch, manifest.TelegramConf(group_chat_id=-1004, notify_in_group=True))
    env = {notif_sync.DEST_OVERRIDE_ENV: "chat:-1009"}
    assert notif_sync.destination_for(_decl(to="group"), env) == "group"


def test_notify_in_group_resolves_to_project_group(monkeypatch):
    _stub_project(monkeypatch, manifest.TelegramConf(group_chat_id=-1004, notify_in_group=True))
    monkeypatch.setenv(notif_sync.DEST_OVERRIDE_ENV, "chat:-1009")
    n = notif_sync.decl_to_notification(_decl(to="group"))
    assert n.destination == "group"
    assert notifications.resolve_targets(n) == [(-1004, None)]


def test_notify_in_group_does_not_touch_dm_decls(monkeypatch):
    _stub_project(monkeypatch, manifest.TelegramConf(group_chat_id=-1004, notify_in_group=True))
    env = {notif_sync.DEST_OVERRIDE_ENV: "chat:-1009"}
    assert notif_sync.destination_for(_decl(to="dm"), env) == "chat:-1009"


def test_without_notify_in_group_override_applies(monkeypatch):
    _stub_project(monkeypatch, manifest.TelegramConf(group_chat_id=-1004))
    env = {notif_sync.DEST_OVERRIDE_ENV: "chat:-1009"}
    assert notif_sync.destination_for(_decl(to="group"), env) == "chat:-1009"


def test_notify_in_group_without_group_id_falls_back_with_warning(monkeypatch, caplog):
    _stub_project(monkeypatch, manifest.TelegramConf(notify_in_group=True))
    env = {notif_sync.DEST_OVERRIDE_ENV: "chat:-1009"}
    with caplog.at_level(_logging.WARNING):
        assert notif_sync.destination_for(_decl(to="group"), env) == "chat:-1009"
    assert "notify_in_group" in caplog.text


def test_manifest_parses_notify_in_group():
    tg = manifest._parse_telegram({"telegram": {"group_chat_id": -5, "notify_in_group": True}})
    assert tg.notify_in_group is True
    assert manifest._parse_telegram({"telegram": {"group_chat_id": -5}}).notify_in_group is False


def test_report_decl_carries_its_params():
    # Il provider report legge `path` da params: se la sync li butta, la
    # notifica parte ogni volta con «no path» e non consegna nulla.
    n = notif_sync.decl_to_notification(_decl(
        provider="report", prompt="", params={"path": "scheduler/data/reports/x.md", "max_age_h": 2}))
    assert n.params == {"path": "scheduler/data/reports/x.md", "max_age_h": 2}


def test_parse_notification_params_table(tmp_path):
    data = {"notification": [{"key": "r", "provider": "report", "schedule": "0 1 * * *",
                              "params": {"path": "a/b.md", "max_age_h": 2}}]}
    proj = type("P", (), {"key": "scheduler"})()
    [decl] = manifest._parse_notifications(data, proj)
    assert decl.params == {"path": "a/b.md", "max_age_h": 2}
