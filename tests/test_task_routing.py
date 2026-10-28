import manifest
import projects
import task_routing


def test_resolve_mapped_category():
    assert task_routing.resolve_specialist("cucina") == ("personalChef", "generale")


def test_resolve_mapped_category_case_insensitive():
    assert task_routing.resolve_specialist("CuCiNa") == ("personalChef", "generale")


def test_resolve_unmapped_falls_back_to_scheduler():
    assert task_routing.resolve_specialist("bucato") == ("scheduler", "generale")


def test_resolve_none_falls_back():
    assert task_routing.resolve_specialist(None) == ("scheduler", "generale")


def test_build_seed_contains_task_fields():
    seed = task_routing.build_seed(
        {
            "name": "Cucinare cena",
            "category": "cucina",
            "duration_min": 45,
            "start_at": "2026-07-04T19:30:00+02:00",
            "description": "pasta",
        }
    )
    assert "Cucinare cena" in seed and "cucina" in seed and "45" in seed
    assert "19:30" in seed
    assert "pasta" in seed


def test_build_seed_missing_fields_does_not_crash():
    seed = task_routing.build_seed({"name": "Task senza dettagli"})
    assert "Task senza dettagli" in seed


def test_resolve_card_target_dm_mode_returns_owner_topic0():
    evt = {"category": "cucina", "user_telegram_chat_id": 123}
    assert task_routing.resolve_card_target(evt, "dm") == (123, 0)


def test_resolve_card_target_coordinator_topic_returns_none():
    evt = {"category": "cucina", "user_telegram_chat_id": 123}
    assert task_routing.resolve_card_target(evt, "coordinator_topic") is None


def test_resolve_card_target_unknown_mode_returns_none():
    evt = {"category": "cucina", "user_telegram_chat_id": 123}
    assert task_routing.resolve_card_target(evt, "bogus") is None


def test_resolve_card_target_project_group_uses_node_group_when_configured(monkeypatch):
    fake_project = object()
    monkeypatch.setattr(
        task_routing, "resolve_specialist", lambda cat: ("personalChef", "generale")
    )
    monkeypatch.setattr(projects, "resolve_project", lambda key: fake_project)
    monkeypatch.setattr(
        manifest,
        "telegram_of",
        lambda proj: manifest.TelegramConf(group_chat_id=-100999, group_thread_id=42),
    )
    evt = {"category": "cucina", "user_telegram_chat_id": 123}
    assert task_routing.resolve_card_target(evt, "project_group") == (-100999, 42)


def test_resolve_card_target_project_group_falls_back_to_dm_when_no_group(monkeypatch):
    fake_project = object()
    monkeypatch.setattr(
        task_routing, "resolve_specialist", lambda cat: ("scheduler", "generale")
    )
    monkeypatch.setattr(projects, "resolve_project", lambda key: fake_project)
    monkeypatch.setattr(manifest, "telegram_of", lambda proj: None)
    evt = {"category": None, "user_telegram_chat_id": 123}
    assert task_routing.resolve_card_target(evt, "project_group") == (123, 0)


def test_resolve_card_target_project_group_falls_back_when_group_chat_id_none(monkeypatch):
    fake_project = object()
    monkeypatch.setattr(
        task_routing, "resolve_specialist", lambda cat: ("scheduler", "generale")
    )
    monkeypatch.setattr(projects, "resolve_project", lambda key: fake_project)
    monkeypatch.setattr(
        manifest, "telegram_of", lambda proj: manifest.TelegramConf(group_chat_id=None)
    )
    evt = {"category": None, "user_telegram_chat_id": 123}
    assert task_routing.resolve_card_target(evt, "project_group") == (123, 0)


def test_resolve_card_target_project_group_falls_back_when_project_unresolved(monkeypatch):
    monkeypatch.setattr(
        task_routing, "resolve_specialist", lambda cat: ("ghost", "generale")
    )
    monkeypatch.setattr(projects, "resolve_project", lambda key: None)
    evt = {"category": "ghost", "user_telegram_chat_id": 123}
    assert task_routing.resolve_card_target(evt, "project_group") == (123, 0)
