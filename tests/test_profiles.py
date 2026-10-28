# tests/test_profiles.py
"""Per-user profiles (M3): env-driven user->profile map, per-project visibility
and access, the legacy ALLOWED_USER_IDS fallback, and registry integrity.

Env is injected via monkeypatch (no dependency on the real .env)."""
import projects
import profiles


def _clear_profile_env(monkeypatch):
    """Remove every PROFILE_*_IDS and the legacy ALLOWED_* vars."""
    for p in profiles.PROFILES:
        monkeypatch.delenv(f"PROFILE_{p.key.upper()}_IDS", raising=False)
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    monkeypatch.delenv("ALLOWED_CHAT_IDS", raising=False)


# 1. profile_for resolves ids from the env, per profile.
def test_profile_for_resolves_ids_from_env(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("PROFILE_OWNER_IDS", "100")
    monkeypatch.setenv("PROFILE_MEMBER_IDS", "200, 201")
    assert profiles.profile_for(100).key == "owner"
    assert profiles.profile_for(200).key == "member"
    assert profiles.profile_for(201).key == "member"


# 2. Admin (owner) sees all projects.
def test_admin_sees_all_projects():
    admin = next(p for p in profiles.PROFILES if p.key == "owner")
    assert admin.is_admin
    assert profiles.visible_projects(admin) == projects.list_projects()


# 3. member sees exactly its projects, in registry order.
def test_member_visible_projects_in_registry_order():
    member = next(p for p in profiles.PROFILES if p.key == "member")
    keys = [p.key for p in profiles.visible_projects(member)]
    assert keys == [
        "personalChef",
        "mycorise",
        "travel",
        "experienceFinder",
        "stayFinder",
        "transportFinder",
        "scheduler",
        "academicAssistant",
    ]
    # order must follow the registry, not the order declared in Profile.projects
    registry_order = [p.key for p in projects.PROJECTS if p.key in set(member.projects)]
    assert keys == registry_order


# 4. can_access enforces per-project permission for a non-admin.
def test_can_access_for_member():
    member = next(p for p in profiles.PROFILES if p.key == "member")
    assert profiles.can_access(member, "personalChef") is True
    assert profiles.can_access(member, "db") is False
    admin = next(p for p in profiles.PROFILES if p.key == "owner")
    assert profiles.can_access(admin, "db") is True  # admin reaches everything


# 5. Unknown user -> None (default deny).
def test_unknown_user_is_none(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("PROFILE_OWNER_IDS", "100")
    assert profiles.profile_for(999) is None


# 6. Legacy fallback: no PROFILE_*_IDS -> ALLOWED_USER_IDS treated as admin.
def test_legacy_fallback_allowed_user_ids_are_admin(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("ALLOWED_USER_IDS", "111,222")
    prof = profiles.profile_for(111)
    assert prof is not None and prof.is_admin and prof.key == "owner"
    assert profiles.profile_for(222).is_admin
    assert profiles.profile_for(333) is None


def test_no_fallback_when_any_profile_env_set(monkeypatch):
    """If any PROFILE_*_IDS is set, ALLOWED_USER_IDS is NOT a fallback."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("PROFILE_MEMBER_IDS", "200")
    monkeypatch.setenv("ALLOWED_USER_IDS", "111")  # must be ignored now
    assert profiles.profile_for(200).key == "member"
    assert profiles.profile_for(111) is None  # default deny, despite ALLOWED_USER_IDS


def test_malformed_ids_are_ignored(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("PROFILE_MEMBER_IDS", "200, notanint, 201")
    assert profiles.profile_for(200).key == "member"
    assert profiles.profile_for(201).key == "member"


def test_id_in_multiple_profiles_first_wins(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("PROFILE_OWNER_IDS", "500")
    monkeypatch.setenv("PROFILE_MEMBER_IDS", "500")
    # owner comes first in PROFILES order -> wins
    assert profiles.profile_for(500).key == "owner"


# --- card_target_for (M5 task card routing) ---
def test_card_target_owner_is_coordinator_topic(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("PROFILE_OWNER_IDS", "100")
    assert profiles.card_target_for(100) == "coordinator_topic"


def test_card_target_member_is_dm(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("PROFILE_MEMBER_IDS", "200")
    assert profiles.card_target_for(200) == "dm"


def test_card_target_unmapped_id_is_dm(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("PROFILE_OWNER_IDS", "100")
    assert profiles.card_target_for(999) == "dm"


def test_card_target_invalid_target_falls_back_to_dm(monkeypatch):
    bad = profiles.Profile(
        "bad", "Bad", is_admin=False, projects=(), task_card_target="nonsense"
    )
    monkeypatch.setattr(profiles, "profile_for", lambda uid: bad)
    assert profiles.card_target_for(1) == "dm"


# 7. Registry integrity: every key referenced by a profile exists in PROJECTS.
def test_profile_project_keys_exist_in_registry():
    valid = {p.key for p in projects.PROJECTS}
    for profile in profiles.PROFILES:
        for key in profile.projects:
            assert key in valid, f"{profile.key} references unknown project {key!r}"
