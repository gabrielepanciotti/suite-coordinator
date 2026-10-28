# tests/test_groups.py
"""Two-level menu: the home picker offers groups (Work / Personal); each group
lists only its projects, and the split composes with per-user profile filtering
so a user never sees an empty group."""
import bot
import projects
from profiles import Profile

ADMIN = Profile("owner", "Owner", True, ())
MIX = Profile("mix", "Mix", False, ("personalChef", "offerte"))  # one personal + one work
PERSONAL_ONLY = Profile("po", "PO", False, ("personalChef",))


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_project_kind_assignments():
    by_key = {p.key: p for p in projects.list_projects()}
    assert by_key["personalChef"].kind == "personal"
    assert by_key["mycorise"].kind == "personal"
    assert by_key["travel"].kind == "personal"
    assert by_key["scheduler"].kind == "personal"
    assert by_key["cosmos"].kind == "personal"
    assert by_key["offerte"].kind == "work"
    assert by_key["claude"].kind == "work"           # config lives under Work


def test_projects_in_groups():
    personal = [p.key for p in projects.projects_in("personal")]
    assert personal == [
        "personalChef",
        "mycorise",
        "homeResearcher",
        "travel",
        "experienceFinder",
        "stayFinder",
        "transportFinder",
        "scheduler",
        "academicAssistant",
        "cosmos",
    ]
    assert "offerte" in {p.key for p in projects.projects_in("work")}
    assert "personalChef" not in {p.key for p in projects.projects_in("work")}


def test_home_shows_both_groups_for_admin():
    cbs = _cbs(bot._kb_home(ADMIN))
    assert "nav:g:work" in cbs
    assert "nav:g:personal" in cbs
    assert not any(cb.startswith("nav:p:") for cb in cbs)  # no projects at top level


def test_group_lists_only_its_projects():
    personal = _cbs(bot._kb_group("personal", ADMIN))
    assert "nav:p:personalChef" in personal
    assert "nav:p:travel" in personal
    assert "nav:p:offerte" not in personal
    assert "nav:home" in personal                          # back to group picker
    work = _cbs(bot._kb_group("work", ADMIN))
    assert "nav:p:offerte" in work
    assert "nav:p:claude" in work
    assert "nav:p:personalChef" not in work


def test_filtering_composes_with_groups():
    # Mix: 1 personal (personalChef) + 1 work (offerte) -> both groups visible...
    home = _cbs(bot._kb_home(MIX))
    assert "nav:g:work" in home and "nav:g:personal" in home
    # ...but each group shows only that user's project.
    assert _cbs(bot._kb_group("work", MIX)) == ["nav:p:offerte", "nav:home"]
    assert _cbs(bot._kb_group("personal", MIX)) == ["nav:p:personalChef", "nav:home"]


def test_empty_group_hidden():
    # A profile with only personal projects must not see a (empty) Work button.
    home = _cbs(bot._kb_home(PERSONAL_ONLY))
    assert home == ["nav:g:personal"]
