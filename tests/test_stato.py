import stato


def test_render_ok():
    out = stato.render({"status": "ok", "summary": {"ok": 2}, "nodes": [{"id": "a", "status": "ok"}]})
    assert "Nessuna degradazione" in out


def test_render_degraded_and_unreachable_named():
    data = {
        "status": "unreachable",
        "summary": {"unreachable": 2, "degraded": 1, "ok": 1},
        "nodes": [
            {"id": "x", "status": "unreachable"},
            {"id": "y", "status": "unreachable"},
            {"id": "z", "status": "degraded", "degradations": [
                {"component": "reg", "subject": "grafo", "reason": "fallback", "fallback": "cache"}]},
            {"id": "ok1", "status": "ok"},
        ],
    }
    out = stato.render(data)
    assert "Non rispondono (2)" in out and "x, y" in out
    assert "reg/grafo" in out and "ripiego: cache" in out
    assert "ok1" not in out


def test_render_empty_graph_is_not_ok():
    out = stato.render({"status": "unreachable", "summary": {}, "nodes": [], "detail": "grafo vuoto"})
    assert "grafo vuoto" in out and "Nessuna degradazione" not in out


def test_render_escapes_html():
    out = stato.render({"status": "degraded", "nodes": [
        {"id": "n", "status": "degraded", "degradations": [{"component": "<b>"}]}]})
    assert "&lt;b&gt;" in out


def test_fetch_off_and_failure():
    import pytest
    with pytest.raises(RuntimeError):
        stato.fetch("off")
    with pytest.raises(RuntimeError):
        stato.fetch("http://127.0.0.1:1", timeout=1)
    assert "non risponde" in stato.render_failure("x")
