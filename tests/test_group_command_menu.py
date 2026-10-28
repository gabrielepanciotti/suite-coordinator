"""Il ``/`` di Telegram non mostra le capability group/both di un progetto se
nessuno chiama set_my_commands con lo scope di quella chat: registrare il
CommandHandler non basta, il menu e' un'altra chiamata (vista live su
ComUnity con /invito: il comando rispondeva ma non compariva digitando "/")."""
import manifest
import projects

import bot

_FAKE_PROJECT = projects.Project(
    key="zzz_fake_menu",
    label="Fake",
    emoji="🧪",
    host_path="/hosts/zzz_fake_menu",
    workspace="/workspaces/zzz_fake_menu",
)


def _man(caps, chat_id=-100999):
    return manifest.Manifest(
        telegram=manifest.TelegramConf(group_chat_id=chat_id),
        capabilities=list(caps),
    )


def test_group_capability_commands_keeps_only_group_and_both():
    caps = (
        manifest.Capability(key="invito", label="Nuovo invito", cli="x", scope="group"),
        manifest.Capability(key="smoke", label="Verifica API", cli="x", scope="command-room"),
        manifest.Capability(key="cena", label="Dinner", cli="x", scope="both"),
    )
    cmds = bot._group_capability_commands(_man(caps))
    assert [c.command for c in cmds] == ["invito", "cena"]


def test_group_capability_commands_empty_without_group_caps():
    caps = (manifest.Capability(key="smoke", label="Verifica API", cli="x", scope="command-room"),)
    assert bot._group_capability_commands(_man(caps)) == []


async def test_on_startup_sets_chat_scoped_menu_for_projects_with_group_caps(monkeypatch):
    caps = (manifest.Capability(key="invito", label="Nuovo invito", cli="x", scope="group"),)
    man = _man(caps, chat_id=-100888)

    monkeypatch.setattr(bot, "list_projects", lambda: [_FAKE_PROJECT])
    monkeypatch.setattr(manifest, "load", lambda p: man)

    calls = []

    class _Bot:
        async def set_my_commands(self, commands, scope=None):
            calls.append((tuple(c.command for c in commands), scope))

    class _App:
        bot = _Bot()

    monkeypatch.setattr(bot, "_BOTS", {})

    await bot.on_startup(_App())

    scoped = [c for c in calls if c[1] is not None]
    assert len(scoped) == 1
    cmds, scope = scoped[0]
    assert "invito" in cmds
    assert scope.chat_id == -100888


async def _scoped_calls(monkeypatch, man):
    monkeypatch.setattr(bot, "list_projects", lambda: [_FAKE_PROJECT])
    monkeypatch.setattr(manifest, "load", lambda p: man)
    calls = []

    class _Bot:
        async def set_my_commands(self, commands, scope=None):
            calls.append(scope)

    class _App:
        bot = _Bot()

    monkeypatch.setattr(bot, "_BOTS", {})
    await bot.on_startup(_App())
    return [s for s in calls if s is not None]


_CAPS = [manifest.Capability(key="invito", label="Nuovo invito", cli="x", scope="group")]


def _owned_by(bot_name):
    return manifest.Manifest(
        telegram=manifest.TelegramConf(group_chat_id=-100777, bot=bot_name),
        capabilities=list(_CAPS),
    )


async def test_group_owned_by_a_dedicated_bot_gets_no_menu_from_us(monkeypatch):
    # Scheduler's room is served by its own bot: the Coordinator token is not a
    # member, and Telegram answered every boot with "Chat not found".
    monkeypatch.setattr(bot.bot_registry, "configured_names", lambda: {"default", "sched"})
    monkeypatch.setattr(bot, "_ABSENT_OWNERS", set())
    assert await _scoped_calls(monkeypatch, _owned_by("sched")) == []


async def test_group_of_a_bot_not_configured_here_gets_no_menu_either(monkeypatch):
    # dev-linux: `scheduler` is declared but its token is polled by Scheduler's
    # own bot, so it is not configured here — and our token is not in the room.
    monkeypatch.setattr(bot.bot_registry, "configured_names", lambda: {"default"})
    monkeypatch.setattr(bot, "_ABSENT_OWNERS", set())
    assert await _scoped_calls(monkeypatch, _owned_by("sched")) == []


async def test_group_whose_owner_bot_is_absent_is_ours_again_menu_included(monkeypatch):
    monkeypatch.setattr(bot.bot_registry, "configured_names", lambda: {"default", "sched"})
    monkeypatch.setattr(bot, "_ABSENT_OWNERS", {("sched", -100777)})
    scoped = await _scoped_calls(monkeypatch, _owned_by("sched"))
    assert [s.chat_id for s in scoped] == [-100777]


def test_placeholder_group_id_zero_means_no_group():
    # Launcher's manifest ships group_chat_id = 0 until the group is created.
    tg = manifest._parse_telegram({"telegram": {"group_chat_id": 0}})
    assert tg.group_chat_id is None
    assert tg.effective_mode == "dm"
