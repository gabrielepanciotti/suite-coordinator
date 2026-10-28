# tests/test_manifest.py
import textwrap

import manifest as M
import projects


def _ws(tmp_path, toml: str):
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True)
    (claude / "coordinator.toml").write_text(textwrap.dedent(toml), encoding="utf-8")
    return projects.Project("x", "X", "\U0001F539", "", str(tmp_path))


VALID = """
    [telegram]
    group_chat_id = -100123
    agent = "casa"
    pin = "Tabella: http://x"

    [[capability]]
    key = "aggiungi"
    label = "Aggiungi"
    cli = "python -m casa.cli ingest {url}"
    scope = "both"
    hint = "incolla un link"

    [[capability]]
    key = "refactor"
    label = "Refactor"
    cli = "x"
    scope = "command-room"

    [[notification]]
    key = "riepilogo"
    title = "Riepilogo"
    provider = "agent"
    schedule = "0 8 * * *"
    to = "group"
    prompt = "riassumi"
"""


def test_load_parses_telegram_capabilities_notifications(tmp_path):
    p = _ws(tmp_path, VALID)
    man = M.load(p)
    assert man.telegram.group_chat_id == -100123
    assert man.telegram.agent == "casa"
    assert man.telegram.pin == "Tabella: http://x"
    assert [c.key for c in man.capabilities] == ["aggiungi", "refactor"]
    assert man.capabilities[0].scope == "both"
    assert man.capabilities[1].scope == "command-room"
    assert man.notifications[0].to == "group"
    assert man.notifications[0].provider == "agent"


def test_absent_file_returns_empty_manifest(tmp_path):
    (tmp_path / ".claude").mkdir(parents=True)
    p = projects.Project("y", "Y", "\U0001F539", "", str(tmp_path))
    man = M.load(p)
    assert man.telegram is None
    assert man.capabilities == [] and man.notifications == []
    assert M.telegram_of(p) is None
    assert M.capabilities_of(p) == []


def test_malformed_toml_returns_empty(tmp_path):
    p = _ws(tmp_path, "this is = = not toml [[[")
    man = M.load(p)
    assert man.telegram is None and man.capabilities == []


def test_capability_missing_required_field_skipped(tmp_path):
    p = _ws(tmp_path, """
        [[capability]]
        label = "no key"
        cli = "x"

        [[capability]]
        key = "ok"
        label = "Ok"
        cli = "x"
    """)
    assert [c.key for c in M.capabilities_of(p)] == ["ok"]


def test_capability_default_scope_is_command_room(tmp_path):
    p = _ws(tmp_path, """
        [[capability]]
        key = "k"
        label = "L"
        cli = "x"
    """)
    assert M.capabilities_of(p)[0].scope == "command-room"


def test_invalid_scope_falls_back_to_command_room(tmp_path):
    p = _ws(tmp_path, """
        [[capability]]
        key = "k"
        label = "L"
        cli = "x"
        scope = "everywhere"
    """)
    assert M.capabilities_of(p)[0].scope == "command-room"


def test_cache_invalidated_on_mtime(tmp_path):
    import os, time
    p = _ws(tmp_path, VALID)
    assert len(M.capabilities_of(p)) == 2
    toml = tmp_path / ".claude" / "coordinator.toml"
    toml.write_text('[[capability]]\nkey="solo"\nlabel="S"\ncli="x"\n', encoding="utf-8")
    os.utime(toml, (time.time() + 5, time.time() + 5))
    assert [c.key for c in M.capabilities_of(p)] == ["solo"]


def test_group_bindings_collects_groups(monkeypatch, tmp_path):
    p = _ws(tmp_path, VALID)
    monkeypatch.setattr(M.projects, "list_projects", lambda: [p])
    assert M.group_bindings() == [(-100123, 0, "x", "casa")]


def test_session_capability_defaults_action(tmp_path):
    p = _ws(tmp_path, """
        [[capability]]
        key = "k"
        label = "L"
        cli = "x"
    """)
    cap = M.capabilities_of(p)[0]
    assert cap.action == "session" and cap.kind == ""


def test_scheduler_capability_parsed_without_cli(tmp_path):
    p = _ws(tmp_path, """
        [[capability]]
        key = "hw-riposo"
        label = "Riposo"
        action = "scheduler_proposal"
        kind = "preference"
        scope = "both"
    """)
    caps = M.capabilities_of(p)
    assert len(caps) == 1
    assert caps[0].action == "scheduler_proposal"
    assert caps[0].kind == "preference"
    assert caps[0].scope == "both"


def test_scheduler_capability_bad_kind_skipped(tmp_path):
    p = _ws(tmp_path, """
        [[capability]]
        key = "hw-bad"
        label = "Bad"
        action = "scheduler_proposal"
        kind = "nonsense"
    """)
    assert M.capabilities_of(p) == []


def test_session_capability_without_cli_skipped(tmp_path):
    p = _ws(tmp_path, """
        [[capability]]
        key = "nocli"
        label = "No CLI"
    """)
    assert M.capabilities_of(p) == []


def test_photo_dir_defaults_to_none(tmp_path):
    p = _ws(tmp_path, """
        [telegram]
        group_chat_id = -100123
    """)
    assert M.load(p).telegram.photo_dir is None


def test_photo_dir_relative_is_kept(tmp_path):
    p = _ws(tmp_path, """
        [telegram]
        group_chat_id = -100123
        photo_dir = "vault/raw"
    """)
    assert M.load(p).telegram.photo_dir == "vault/raw"


def test_photo_dir_normalises_separators_and_trailing_slash(tmp_path):
    p = _ws(tmp_path, """
        [telegram]
        group_chat_id = -100123
        photo_dir = 'vault\\raw/'
    """)
    assert M.load(p).telegram.photo_dir == "vault/raw"


def test_photo_dir_rejects_windows_drive(tmp_path):
    p = _ws(tmp_path, """
        [telegram]
        group_chat_id = -100123
        photo_dir = 'C:/Windows'
    """)
    assert M.load(p).telegram.photo_dir is None


def test_photo_dir_rejects_escaping_the_workspace(tmp_path):
    p = _ws(tmp_path, """
        [telegram]
        group_chat_id = -100123
        photo_dir = "../../etc"
    """)
    assert M.load(p).telegram.photo_dir is None


def test_photo_dir_rejects_absolute_path(tmp_path):
    p = _ws(tmp_path, """
        [telegram]
        group_chat_id = -100123
        photo_dir = "/etc/passwd"
    """)
    assert M.load(p).telegram.photo_dir is None


def test_team_section_and_requires_parse(tmp_path):
    p = _ws(tmp_path, """
        [team]
        github_repo = "ownerowner/comunity"
        instance = "comunity"
        sector_areas = { "settore-dev" = ["bizProduct", "bizTech"] }

        [[capability]]
        key = "segnala"
        label = "Segnala"
        action = "team"
        kind = "segnala"
        scope = "group"
        requires = "life:use"
    """)
    man = M.load(p)
    assert man.team is not None
    assert man.team.github_repo == "ownerowner/comunity"
    assert man.team.instance == "comunity"
    assert man.team.sector_areas["settore-dev"] == ("bizProduct", "bizTech")
    assert man.capabilities[0].requires == "life:use"
    assert man.capabilities[0].action == "team"
    assert man.capabilities[0].kind == "segnala"
    assert M.team_of(p).instance == "comunity"


def test_unknown_action_is_skipped(tmp_path):
    p = _ws(tmp_path, """
        [[capability]]
        key = "boh"
        label = "Boh"
        action = "teleport"
    """)
    assert M.capabilities_of(p) == []
