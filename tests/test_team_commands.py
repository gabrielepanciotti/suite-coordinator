"""Task 14 wrappers: deterministic commands, no LLM, errors relayed verbatim."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import manifest
import projects
import team_commands


class Msg:
    def __init__(self, text="", caption=None, photo=None, message_id=42, thread_id=6):
        self.text = text
        self.caption = caption
        self.photo = photo
        self.message_id = message_id
        self.message_thread_id = thread_id
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _update(uid=77, msg=None, chat_title="ComUnity"):
    msg = msg or Msg()
    chat = SimpleNamespace(id=-1003875691522, title=chat_title, type="supergroup")
    user = SimpleNamespace(id=uid, full_name="Alice", username="alice")
    return SimpleNamespace(effective_message=msg, effective_chat=chat,
                           effective_user=user, message=msg, edited_message=None,
                           callback_query=None)


def _project(tmp_path) -> projects.Project:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "pubspec.yaml").write_text("name: comunity\nversion: 1.0.11+15\n",
                                                       encoding="utf-8")
    return projects.Project("comunity", "ComUnity", "", "", str(tmp_path))


def _team() -> manifest.TeamConf:
    return manifest.TeamConf(
        github_repo="ownerowner/comunity",
        instance="comunity",
        sector_areas={"settore-dev": ("bizProduct", "bizTech")},
    )


def test_app_version_and_note_link(tmp_path):
    assert team_commands._app_version(_project(tmp_path)) == "1.0.11+15"
    assert team_commands._telegram_link(-1003875691522, 6, 42) == \
        "https://t.me/c/3875691522/42?thread=6"


def test_clickup_person_accepts_both_shapes(monkeypatch):
    monkeypatch.setattr(team_commands, "_instance_config", lambda iid: {
        "channels": {"clickup": {"people": {
            "77": {"clickup_user_id": 1234, "name": "Alice"},
            "9": 99,
        }}}
    })
    assert team_commands._clickup_person("comunity", 77, 7) == 1234
    assert team_commands._clickup_person("comunity", 1, 9) == 99
    assert team_commands._clickup_person("comunity", 5, 5) is None


def test_incarichi_uses_the_topic_sector_areas(monkeypatch, tmp_path):
    calls = []

    async def fake_run(project, instance_path, argv):
        calls.append(argv)
        return 0, "id\ttitolo\tstato\tassegnatario", ""

    monkeypatch.setattr(team_commands, "_instance_path", lambda iid: tmp_path)
    monkeypatch.setattr(team_commands, "_instance_config", lambda iid: {
        "areas": [{"slug": "bizProduct"}, {"slug": "bizTech"}, {"slug": "bizFinance"}],
        "channels": {"clickup": {"people": {}}},
    })
    monkeypatch.setattr(team_commands, "_run_cli", fake_run)
    upd = _update(msg=Msg("qualcosa"))
    asyncio.run(team_commands.incarichi(upd, _project(tmp_path), _team(),
                                        "comunity", "settore-dev", ""))
    assert [c[2] for c in calls] == ["bizProduct", "bizTech"]
    assert "bizTech" in upd.effective_message.replies[0]
    assert "bizFinance" not in upd.effective_message.replies[0]


def test_incarichi_relays_the_italian_error_line(monkeypatch, tmp_path):
    async def fake_run(project, instance_path, argv):
        return 2, "", "ClickUp non collegato: manca CLICKUP_TOKEN in /x/.env\n"

    monkeypatch.setattr(team_commands, "_instance_path", lambda iid: tmp_path)
    monkeypatch.setattr(team_commands, "_instance_config", lambda iid: {
        "areas": [{"slug": "bizTech"}], "channels": {"clickup": {"people": {}}}})
    monkeypatch.setattr(team_commands, "_run_cli", fake_run)
    upd = _update()
    asyncio.run(team_commands.incarichi(upd, _project(tmp_path), _team(),
                                        "comunity", "settore-dev", "bizTech"))
    assert "ClickUp non collegato: manca CLICKUP_TOKEN" in upd.effective_message.replies[0]


def test_prendi_passes_the_senders_clickup_id(monkeypatch, tmp_path):
    seen = []

    async def fake_run(project, instance_path, argv):
        seen.append(argv)
        return 0, "", ""

    monkeypatch.setattr(team_commands, "_instance_path", lambda iid: tmp_path)
    monkeypatch.setattr(team_commands, "_instance_config", lambda iid: {
        "channels": {"clickup": {"people": {"77": {"clickup_user_id": 1234}}}}})
    monkeypatch.setattr(team_commands, "_run_cli", fake_run)
    upd = _update()
    grant = SimpleNamespace(person_id=7)
    asyncio.run(team_commands.prendi(upd, _project(tmp_path), _team(), "comunity",
                                    grant, "abc123"))
    assert seen == [["prendi", "abc123", "--persona", "1234"]]
    assert "preso" in upd.effective_message.replies[0]


def test_prendi_without_mapping_explains_the_config(monkeypatch, tmp_path):
    monkeypatch.setattr(team_commands, "_instance_config", lambda iid: {
        "channels": {"clickup": {"people": {}}}})
    upd = _update()
    asyncio.run(team_commands.prendi(upd, _project(tmp_path), _team(), "comunity",
                                    SimpleNamespace(person_id=7), "abc"))
    assert "channels.clickup.people" in upd.effective_message.replies[0]


def test_chiudi_uses_the_message_as_default_note(monkeypatch, tmp_path):
    seen = []

    async def fake_run(project, instance_path, argv):
        seen.append(argv)
        return 0, "", ""

    monkeypatch.setattr(team_commands, "_instance_path", lambda iid: tmp_path)
    monkeypatch.setattr(team_commands, "_run_cli", fake_run)
    upd = _update(msg=Msg(message_id=42, thread_id=6))
    asyncio.run(team_commands.chiudi(upd, _project(tmp_path), _team(), "comunity",
                                    SimpleNamespace(person_id=7), "task-1"))
    assert seen == [["chiudi", "task-1", "--nota",
                     "https://t.me/c/3875691522/42?thread=6"]]
    assert "in revisione" in upd.effective_message.replies[0]


def test_segnala_opens_the_issue_and_reports_clickup_down(monkeypatch, tmp_path):
    async def fake_issue(repo, token, title, body, **kw):
        assert repo == "ownerowner/comunity"
        assert "1.0.11+15" in body and "Telegram `77`" in body
        return {"number": 5, "html_url": "https://github.com/x/issues/5"}

    async def fake_run(project, instance_path, argv):
        assert argv[0] == "crea" and argv[2] == "bizTech"
        return 2, "", "ClickUp non collegato: manca CLICKUP_TOKEN in /x/.env\n"

    monkeypatch.setattr(team_commands, "create_issue", fake_issue)
    monkeypatch.setattr(team_commands, "_instance_path", lambda iid: tmp_path)
    monkeypatch.setattr(team_commands, "_run_cli", fake_run)
    upd = _update(msg=Msg(caption="/segnala il login si blocca"))
    asyncio.run(team_commands.segnala(upd, _project(tmp_path), _team(), "comunity",
                                      "il login si blocca"))
    reply = upd.effective_message.replies[0]
    assert "https://github.com/x/issues/5" in reply
    assert "Task ClickUp non creato: ClickUp non collegato" in reply
