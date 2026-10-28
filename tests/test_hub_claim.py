import importlib


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_CLAIMS_DIR", str(tmp_path))
    import hub_store
    importlib.reload(hub_store)
    import hub_claim
    importlib.reload(hub_claim)
    return hub_claim


def test_claim_status_release_roundtrip(monkeypatch, tmp_path):
    hc = _fresh(monkeypatch, tmp_path)
    assert hc.main(["status", "a1", "--tenant", "t"]) == 1   # not yet claimed
    assert hc.main(["claim", "a1", "--tenant", "t"]) == 0
    assert hc.main(["status", "a1", "--tenant", "t"]) == 0   # claimed
    assert hc.main(["release", "a1", "--tenant", "t"]) == 0
    assert hc.main(["status", "a1", "--tenant", "t"]) == 1   # released
    # default tenant path (no --tenant) is reachable
    assert hc.main(["status", "a1"]) == 1
