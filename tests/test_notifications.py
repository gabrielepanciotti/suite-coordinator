import notifications as N


def _n(**kw):
    base = dict(key="k1", title="T", provider="static", schedule="0 8 * * *",
               recipients=("owner",), enabled=True, params={"text": "ciao"})
    base.update(kw)
    return N.Notification(**base)


def test_roundtrip_to_from_dict():
    n = _n()
    d = n.to_dict()
    assert d["recipients"] == ["owner"]            # tuple -> list for JSON
    assert N.Notification.from_dict(d) == n


def test_validate_ok_static():
    assert N.validate(_n()) == []


def test_validate_bad_provider_schedule_recipient():
    n = _n(provider="nope", schedule="not a cron", recipients=("ghost",), params={})
    errs = N.validate(n)
    assert any("provider" in e for e in errs)
    assert any("crontab" in e for e in errs)
    assert any("destinatari" in e for e in errs)


def test_validate_agent_requires_project_and_prompt():
    assert N.validate(_n(provider="agent", params={})) != []
    ok = _n(provider="agent", params={"project": "personalChef", "prompt": "x"})
    assert N.validate(ok) == []


def test_validate_static_requires_text():
    assert N.validate(_n(provider="static", params={})) != []


def test_validate_group_needs_no_recipients():
    # group/chat destinations resolve their target from params/chat id, so the
    # M3 profile recipients are irrelevant and must not be required.
    assert N.validate(_n(destination="group", recipients=())) == []
    assert N.validate(_n(destination="chat:-100123", recipients=())) == []


def test_validate_dm_still_requires_recipients():
    errs = N.validate(_n(destination="dm", recipients=()))
    assert N.NO_RECIPIENTS_MSG in errs


def test_resolve_recipients_dedups(monkeypatch):
    monkeypatch.setenv("PROFILE_OWNER_IDS", "111")
    monkeypatch.setenv("PROFILE_MEMBER_IDS", "111,222")   # 111 shared
    assert N.resolve_recipients(("owner", "member")) == [111, 222]


def test_preset_to_cron():
    assert N.preset_to_cron("hourly") == "0 * * * *"
    assert N.preset_to_cron("m8") == "0 8 * * *"
    assert N.preset_to_cron("sun11") == "0 11 * * sun"
    assert N.preset_to_cron("zzz") is None


def test_gen_key_unique():
    assert N.gen_key("agent", set()) == "agent"
    assert N.gen_key("agent", {"agent"}) == "agent-2"
    assert N.gen_key("agent", {"agent", "agent-2"}) == "agent-3"


def test_destination_defaults_to_dm_and_roundtrips():
    n = _n()
    assert n.destination == "dm"
    assert N.Notification.from_dict(n.to_dict()).destination == "dm"


def test_from_dict_missing_destination_is_dm():
    d = _n().to_dict()
    d.pop("destination", None)            # legacy JSON without the field
    assert N.Notification.from_dict(d).destination == "dm"


def test_resolve_targets_dm(monkeypatch):
    monkeypatch.setattr(N, "resolve_recipients", lambda r: [111, 222])
    n = _n(destination="dm")
    assert N.resolve_targets(n) == [(111, None), (222, None)]


def test_resolve_targets_explicit_chat_and_thread():
    assert N.resolve_targets(_n(destination="chat:-100999")) == [(-100999, None)]
    assert N.resolve_targets(_n(destination="chat:-100999:7")) == [(-100999, 7)]
    assert N.resolve_targets(_n(destination="chat:notanint")) == []


def test_resolve_targets_group(monkeypatch):
    import manifest, projects
    proj = projects.Project("casa", "Casa", "\U0001F3E0", "", "/ws")
    monkeypatch.setattr(projects, "resolve_project", lambda t: proj if t == "casa" else None)
    monkeypatch.setattr(manifest, "telegram_of",
                        lambda p: manifest.TelegramConf(group_chat_id=-100555))
    n = _n(destination="group", params={"project": "casa", "text": "x"})
    assert N.resolve_targets(n) == [(-100555, None)]


def test_resolve_targets_group_unknown_project_is_empty(monkeypatch):
    import projects
    monkeypatch.setattr(projects, "resolve_project", lambda t: None)
    assert N.resolve_targets(_n(destination="group", params={"project": "ghost"})) == []


def test_project_of_from_params():
    assert N.project_of(_n(params={"project": "personalChef", "text": "x"})) == "personalChef"


def test_project_of_falls_back_to_key_prefix():
    assert N.project_of(_n(key="personalchef-buongiorno", params={"text": "x"})) == "personalchef"


def test_is_personal_true_for_personal_kind(monkeypatch):
    import projects
    proj = projects.Project("personalChef", "Toni", "\U0001F373", "", "/ws", kind="personal")
    monkeypatch.setattr(projects, "resolve_project", lambda t: proj if t == "personalChef" else None)
    assert N.is_personal(_n(params={"project": "personalChef", "text": "x"})) is True


def test_is_personal_false_for_work_kind(monkeypatch):
    import projects
    proj = projects.Project("offerte", "Hub", "\U0001F4E6", "", "/ws", kind="work")
    monkeypatch.setattr(projects, "resolve_project", lambda t: proj if t == "offerte" else None)
    assert N.is_personal(_n(params={"project": "offerte", "text": "x"})) is False


def test_is_personal_false_for_unknown_project(monkeypatch):
    import projects
    monkeypatch.setattr(projects, "resolve_project", lambda t: None)  # fail-open
    assert N.is_personal(_n(params={"project": "ghost", "text": "x"})) is False


def test_is_banned_true_for_personal_not_allowlisted(monkeypatch):
    # Key deliberately absent from every allowlist (was mycorise-scadenze-batch
    # until 2026-08-09, when that key was re-enabled and joined the default set).
    monkeypatch.setattr(N, "is_personal", lambda n: True)
    assert N.is_banned(_n(key="ghost-notifica", params={"text": "x"})) is True


def test_is_banned_false_for_allowlisted_personal(monkeypatch):
    monkeypatch.setattr(N, "is_personal", lambda n: True)
    for key in N.NOTIFICATION_ALLOWLIST:
        assert N.is_banned(_n(key=key, params={"text": "x"})) is False


def test_load_allowlist_falls_back_to_default_when_unset():
    # Unset must NOT mean "empty": an unconfigured device silently banning every
    # personal notification is exactly the 2026-07-31 regression this guards.
    assert N.load_allowlist({}) == N._DEFAULT_ALLOWLIST
    assert "life-deriva-rete" in N._DEFAULT_ALLOWLIST


def test_load_allowlist_parses_commas_and_whitespace():
    got = N.load_allowlist({N.ALLOWLIST_ENV: " a-b, c-d\ne-f ,, "})
    assert got == frozenset({"a-b", "c-d", "e-f"})


def test_load_allowlist_set_but_empty_means_no_personal_key():
    # Distinct from unset: an explicit empty value is a real "ban them all".
    assert N.load_allowlist({N.ALLOWLIST_ENV: "  "}) == frozenset()


def test_is_banned_honours_wildcard_allowlist(monkeypatch):
    monkeypatch.setattr(N, "is_personal", lambda n: True)
    monkeypatch.setattr(N, "NOTIFICATION_ALLOWLIST", frozenset({N.ALLOW_ALL}))
    assert N.is_banned(_n(key="brand-new-key", params={"text": "x"})) is False


def test_is_banned_true_for_personal_when_allowlist_emptied(monkeypatch):
    monkeypatch.setattr(N, "is_personal", lambda n: True)
    monkeypatch.setattr(N, "NOTIFICATION_ALLOWLIST", frozenset())
    assert N.is_banned(_n(key="life-deriva-rete", params={"text": "x"})) is True


def test_default_allowlist_covers_the_reenabled_personal_keys(monkeypatch):
    # The 2026-08-09 decision, pinned: these keys must survive is_banned() on a
    # device that sets no env var. growbox-orario stays out on purpose (hourly).
    monkeypatch.setattr(N, "is_personal", lambda n: True)
    monkeypatch.setattr(N, "NOTIFICATION_ALLOWLIST", N._DEFAULT_ALLOWLIST)
    for key in ("personalchef-buongiorno", "mycorise-digest-kb",
                "homeresearcher-riepilogo", "scheduler-piano-giorno",
                "cosmos-report-lunedi", "academicassistant-scadenze",
                "travel-briefing-partenza", "travel-digest-domenica"):
        assert N.is_banned(_n(key=key, params={"text": "x"})) is False
    assert N.is_banned(_n(key="growbox-orario", params={"text": "x"})) is True


def test_is_banned_true_for_work_notification(monkeypatch):
    # Policy 2026-07-06 (commit edf850c): work-project notifications are banned
    # too, not just non-allowlisted personal ones. Before that date this asserted
    # False — the policy changed, the test hadn't followed.
    monkeypatch.setattr(N, "is_personal", lambda n: False)
    monkeypatch.setattr(N, "is_work", lambda n: True)
    assert N.is_banned(_n(key="offerte-report", params={"text": "x"})) is True


def test_is_banned_false_for_neither_work_nor_personal(monkeypatch):
    # A notification tied to no known project stays deliverable (fail-open).
    monkeypatch.setattr(N, "is_personal", lambda n: False)
    monkeypatch.setattr(N, "is_work", lambda n: False)
    assert N.is_banned(_n(key="offerte-report", params={"text": "x"})) is False
