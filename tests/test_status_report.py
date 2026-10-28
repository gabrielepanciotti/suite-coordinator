import status_report
from projects import Project


def test_docker_services_matches_by_slug_boundary_case_insensitive():
    containers = [
        ("homeresearcher-db", "running"),
        ("homeresearcher-backend", "exited"),
        ("cosmos-db", "running"),
        ("cosmosdocs-db", "running"),  # different node: must NOT match "cosmos"
        ("network-backend", "running"),
    ]
    # camelCase key vs lowercase container names
    camel = status_report._docker_services("homeResearcher", containers)
    assert {s["name"]: s["up"] for s in camel} == {
        "homeresearcher-db": True,
        "homeresearcher-backend": False,
    }
    cosmos = status_report._docker_services("cosmos", containers)
    assert [s["name"] for s in cosmos] == ["cosmos-db"]  # boundary excludes cosmosdocs


def test_build_status_report_assembles_dedups_and_skips_unresolvable(monkeypatch):
    proj = Project("cosmos", "Cosmos", "", r"X:\srv\Cosmos", "/workspaces/cosmos")
    monkeypatch.setattr(status_report, "device_name", lambda: "minipc")
    monkeypatch.setattr(
        status_report, "_all_containers", lambda: [("cosmos-db", "running")]
    )
    monkeypatch.setattr(
        status_report, "_git_status",
        lambda ws: {"git_ahead": 1, "git_behind": 0, "dirty": True},
    )
    monkeypatch.setattr(
        status_report.projects, "resolve_project",
        lambda k: proj if k == "cosmos" else None,
    )

    # "cosmos" twice (dedup) + "ghost" (unresolvable -> skipped)
    payload = status_report.build_status_report(["cosmos", "cosmos", "ghost"])

    assert payload["device"] == "minipc"
    assert len(payload["projects"]) == 1
    entry = payload["projects"][0]
    assert entry["name"] == "cosmos"
    assert entry["git_ahead"] == 1 and entry["dirty"] is True
    assert entry["services"] == [{"name": "cosmos-db", "up": True}]


def test_git_status_unknown_when_not_a_repo(tmp_path):
    # a plain dir with no .git -> all None (unknown, not a false "clean/0")
    res = status_report._git_status(str(tmp_path))
    assert res == {"git_ahead": None, "git_behind": None, "dirty": None}
