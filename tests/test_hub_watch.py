import asyncio
import importlib


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_CLAIMS_DIR", str(tmp_path))
    import hub_store
    importlib.reload(hub_store)
    import hub_watch
    importlib.reload(hub_watch)
    return hub_store, hub_watch


async def _fake_stream(events):
    for e in events:
        yield e


async def _drop_stream():
    """An SSE stream that drops mid-flight like Network closing the connection."""
    import httpx

    raise httpx.RemoteProtocolError("peer closed connection without complete body")
    yield  # unreachable, but makes this an async generator


def test_watch_exits_on_first_wake_event_and_keeps_claim(monkeypatch, tmp_path, capsys):
    hub_store, hub_watch = _fresh(monkeypatch, tmp_path)
    events = [("noise", {"x": 1}, 1), ("new_message", {"conversation_id": 5}, 2)]
    asyncio.run(
        hub_watch.watch("t", "a1", "http://x", "k", stream=_fake_stream(events))
    )
    out = capsys.readouterr().out
    assert '"new_message"' in out
    assert '"conversation_id": 5' in out
    # heartbeat ran at least once before the event -> claim present
    assert hub_store.is_claimed("t", "a1") is True
    # ...and it is a real watcher claim (issue #26), not a hook-style one
    assert hub_store.is_watched("t", "a1") is True


def test_consegna_dice_che_il_nodo_ha_smesso_di_ascoltare(monkeypatch, tmp_path, capsys):
    """Consegnare l'evento significa uscire: il nodo da quel momento e' sordo.

    L'hint del SessionStart ha gia' parlato all'apertura e non parlera' piu',
    quindi se non lo dice il programma nessuno rilancia il watcher -- e il
    claim-hook continua a scrivere l'heartbeat, cosi' il bus mostra un nodo in
    ascolto che non ascolta (la forma di ETL_V2#88).
    """
    _, hub_watch = _fresh(monkeypatch, tmp_path)
    asyncio.run(
        hub_watch.watch(
            "t", "offerte-dev-linux", "http://x", "k",
            stream=_fake_stream([("new_message", {"conversation_id": 7}, 1)]),
        )
    )
    cap = capsys.readouterr()
    # stdout resta il solo payload: e' quello che rientra nella sessione viva.
    assert cap.out.strip().startswith("{") and cap.out.strip().endswith("}")
    assert "hub_watch:" not in cap.out
    # il promemoria sta su stderr, e porta il comando gia' scritto per intero
    assert "watcher USCITO" in cap.err
    assert 'hub_watch.py "offerte-dev-linux"' in cap.err


def test_nessun_promemoria_senza_consegna(monkeypatch, tmp_path, capsys):
    """Se non e' arrivato niente il nodo sta ancora ascoltando: non c'e' nulla da dire."""
    _, hub_watch = _fresh(monkeypatch, tmp_path)
    asyncio.run(
        hub_watch.watch("t", "a1", "http://x", "k", stream=_fake_stream([("noise", {}, 1)]))
    )
    assert "watcher USCITO" not in capsys.readouterr().err


def test_watch_ignores_non_wake_events(monkeypatch, tmp_path, capsys):
    hub_store, hub_watch = _fresh(monkeypatch, tmp_path)
    asyncio.run(
        hub_watch.watch("t", "a1", "http://x", "k", stream=_fake_stream([("noise", {}, 1)]))
    )
    assert capsys.readouterr().out.strip() == ""


def test_watch_reconnects_after_stream_drop(monkeypatch, tmp_path, capsys):
    """A dropped SSE connection must reconnect, not crash the watcher."""
    hub_store, hub_watch = _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(hub_watch, "RECONNECT_BACKOFF_S", (0,))  # no real sleeps
    calls = {"n": 0}

    def connect():
        calls["n"] += 1
        if calls["n"] == 1:
            return _drop_stream()  # raises RemoteProtocolError on first read
        return _fake_stream([("new_message", {"conversation_id": 9}, 3)])

    asyncio.run(hub_watch.watch("t", "a1", "http://x", "k", connect=connect))
    out = capsys.readouterr().out
    assert '"conversation_id": 9' in out
    assert calls["n"] == 2  # reconnected once after the drop
    assert hub_store.is_claimed("t", "a1") is True


def test_watch_reconnects_after_clean_keepalive_close(monkeypatch, tmp_path, capsys):
    """A stream that ends cleanly without a wake event must reconnect too."""
    hub_store, hub_watch = _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(hub_watch, "RECONNECT_BACKOFF_S", (0,))
    calls = {"n": 0}

    def connect():
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_stream([("noise", {}, 1)])  # ends with no wake event
        return _fake_stream([("conversation_created", {"conversation_id": 7}, 4)])

    asyncio.run(hub_watch.watch("t", "a1", "http://x", "k", connect=connect))
    out = capsys.readouterr().out
    assert '"conversation_created"' in out
    assert '"conversation_id": 7' in out
    assert calls["n"] == 2


def test_valore_env_drops_inline_comments_and_quotes():
    import hub_watch

    assert hub_watch._valore_env("3600     # tetto assoluto") == "3600"
    assert hub_watch._valore_env("x\t# commento") == "x"
    assert hub_watch._valore_env('"a # b"  # vero commento') == "a # b"
    assert hub_watch._valore_env("'http://h:1/x#frag'") == "http://h:1/x#frag"
    assert hub_watch._valore_env("abc#def") == "abc#def"


def test_importing_hub_watch_does_not_load_the_repo_env(monkeypatch):
    """Imported, the watcher must not pour the deploy .env into os.environ."""
    import importlib
    import os

    import hub_watch

    monkeypatch.delenv("AI_BACKEND", raising=False)
    importlib.reload(hub_watch)
    assert "AI_BACKEND" not in os.environ
