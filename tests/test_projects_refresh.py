import projects


def test_load_projects_defers_to_life_when_no_local_config(monkeypatch, tmp_path):
    monkeypatch.setattr(projects, "load_projects_config_path", lambda: tmp_path / "absent.json")
    called = {}

    def fake_load_from_life(*, fallback, test_mounts=True):
        called["fallback"] = fallback
        return (projects.Project("x", "X", "", "/p", "/workspaces/x"),)

    import life_registry_client
    monkeypatch.setattr(life_registry_client, "load_projects_from_life", fake_load_from_life)

    result = projects.load_projects()
    assert result[0].key == "x"
    assert called["fallback"] == projects._DEFAULT_PROJECTS


def test_refresh_projects_reports_added_and_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(projects, "load_projects_config_path", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(
        projects, "PROJECTS",
        (projects.Project("old", "Old", "", "/o", "/workspaces/old"),),
    )

    def fake_load_from_life(*, fallback):
        return (projects.Project("new", "New", "", "/n", "/workspaces/new"),)

    import life_registry_client
    monkeypatch.setattr(life_registry_client, "load_projects_from_life", fake_load_from_life)

    diff = projects.refresh_projects()
    assert diff == {"added": ["new"], "removed": ["old"]}
    assert [p.key for p in projects.PROJECTS] == ["new"]


def test_refresh_projects_is_noop_with_local_config(monkeypatch, tmp_path):
    config = tmp_path / "projects.local.json"
    config.write_text('{"projects": []}', encoding="utf-8")
    monkeypatch.setattr(projects, "load_projects_config_path", lambda: config)
    monkeypatch.setattr(
        projects, "PROJECTS",
        (projects.Project("old", "Old", "", "/o", "/workspaces/old"),),
    )
    diff = projects.refresh_projects()
    assert diff == {"added": [], "removed": []}
