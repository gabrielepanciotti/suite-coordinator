import profiles


def test_user_ids_for_returns_mapped_ids(monkeypatch):
    monkeypatch.setenv("PROFILE_OWNER_IDS", "111")
    monkeypatch.setenv("PROFILE_MEMBER_IDS", "222,333")
    assert profiles.user_ids_for("member") == [222, 333]
    assert profiles.user_ids_for("owner") == [111]


def test_user_ids_for_unknown_or_unmapped_is_empty(monkeypatch):
    monkeypatch.setenv("PROFILE_OWNER_IDS", "111")
    monkeypatch.delenv("PROFILE_MEMBER_IDS", raising=False)
    assert profiles.user_ids_for("member") == []        # mapped to nobody -> default deny
    assert profiles.user_ids_for("nessuno") == []     # unknown profile


def test_admin_user_ids_only_admins(monkeypatch):
    monkeypatch.setenv("PROFILE_OWNER_IDS", "111")
    monkeypatch.setenv("PROFILE_MEMBER_IDS", "222")
    assert profiles.admin_user_ids() == [111]          # owner is_admin, member not
