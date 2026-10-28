import notif_store as store
from notifications import Notification


def _n(key="k1"):
    return Notification(
        key, "T", "static", "0 8 * * *", ("owner",), True, {"text": "x"}
    )


def test_load_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "NOTIF_PATH", str(tmp_path / "n.json"))
    assert store.load() == []


def test_upsert_get_delete_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "NOTIF_PATH", str(tmp_path / "n.json"))
    store.upsert(_n("a"))
    store.upsert(_n("b"))
    assert {n.key for n in store.load()} == {"a", "b"}
    assert store.get("a") == _n("a")
    store.upsert(
        Notification("a", "T2", "static", "0 9 * * *", ("owner",), False, {"text": "y"})
    )
    assert (
        store.get("a").title == "T2" and store.get("a").enabled is False
    )  # replace, not dup
    assert len(store.load()) == 2
    assert store.delete("a") is True
    assert store.get("a") is None
    assert store.delete("ghost") is False


def test_save_skipped_when_dir_missing(monkeypatch):
    monkeypatch.setattr(store, "NOTIF_PATH", "/no_such_dir_zzz/n.json")
    store.upsert(_n("a"))  # must not raise


def test_runs_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "RUNS_PATH", str(tmp_path / "runs.json"))
    assert store.load_runs() == {}


def test_record_run_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "RUNS_PATH", str(tmp_path / "runs.json"))
    store.record_run("a", "2026-06-14T08:00:00+02:00")
    store.record_run("b", "2026-06-14T11:00:00+02:00")
    store.record_run("a", "2026-06-15T08:00:00+02:00")  # overwrite, not append
    runs = store.load_runs()
    assert runs == {"a": "2026-06-15T08:00:00+02:00", "b": "2026-06-14T11:00:00+02:00"}


def test_record_run_skipped_when_dir_missing(monkeypatch):
    monkeypatch.setattr(store, "RUNS_PATH", "/no_such_dir_zzz/runs.json")
    store.record_run("a", "2026-06-14T08:00:00+02:00")  # must not raise


def test_seed_if_empty_seeds_nothing(tmp_path, monkeypatch):
    """life-work carries no hardcoded defaults: an empty store stays empty.

    The seed used to reinstate three personal-Life rows, so deleting them never
    stuck. Notifications now arrive only from the project manifests.
    """
    monkeypatch.setattr(store, "NOTIF_PATH", str(tmp_path / "n.json"))
    assert store.seed_if_empty() == []
    assert store.seed_if_empty() == []  # idempotent, still empty


# --- remap_chat: following a group promoted to a supergroup -----------------


def _at(key, destination):
    return Notification(key, "T", "static", "0 8 * * *", (), True, {"text": "x"},
                        destination)


def test_remap_moves_only_the_promoted_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "NOTIF_PATH", str(tmp_path / "n.json"))
    store.upsert(_at("mine", "chat:-5480477264"))
    store.upsert(_at("other", "chat:-999"))
    store.upsert(_at("dm", "dm"))

    assert store.remap_chat(-5480477264, -1001234567890) == ["mine"]
    dests = {n.key: n.destination for n in store.load()}
    assert dests["mine"] == "chat:-1001234567890"
    assert dests["other"] == "chat:-999" and dests["dm"] == "dm"


def test_remap_drops_the_thread_suffix(tmp_path, monkeypatch):
    # A basic group has no topics, so a thread cannot survive the promotion;
    # the supergroup's General topic is thread 0.
    monkeypatch.setattr(store, "NOTIF_PATH", str(tmp_path / "n.json"))
    store.upsert(_at("k", "chat:-555:7"))
    assert store.remap_chat(-555, -100777) == ["k"]
    assert store.get("k").destination == "chat:-100777"


def test_remap_does_not_match_an_id_that_merely_starts_the_same(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "NOTIF_PATH", str(tmp_path / "n.json"))
    store.upsert(_at("k", "chat:-5551234"))
    assert store.remap_chat(-555, -100777) == []
    assert store.get("k").destination == "chat:-5551234"


def test_remap_with_nothing_to_move_leaves_the_file_alone(tmp_path, monkeypatch):
    path = tmp_path / "n.json"
    monkeypatch.setattr(store, "NOTIF_PATH", str(path))
    store.upsert(_at("k", "dm"))
    before = path.read_text(encoding="utf-8")
    assert store.remap_chat(-1, -2) == []
    assert path.read_text(encoding="utf-8") == before
