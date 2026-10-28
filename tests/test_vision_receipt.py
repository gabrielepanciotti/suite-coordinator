"""POST /api/vision/receipt: fake runner, no model, no network."""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api
import scheduler
import vision_receipt

JSON_OK = (
    '{"lines":[{"raw_text":"LATTE UHT 1L","name":"Latte UHT","quantity":1,"unit":"l"},'
    '{"raw_text":"PASTA 500G","name":"Pasta","quantity":500,"unit":"g"},'
    '{"raw_text":"BANANE","name":"Banane","quantity":null,"unit":null}]}'
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("VISION_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("VISION_TOKEN", "s3cret")
    box = {"reply": JSON_OK, "seen": []}

    async def fake(prompt, *, workspace, persona, **kw):
        box["seen"].append((prompt, workspace, persona, kw))
        reply = box["reply"]
        if isinstance(reply, Exception):
            raise reply
        return (reply, None)

    monkeypatch.setattr(scheduler, "get_run_claude", lambda: fake)
    box["client"] = TestClient(api.app)
    box["ws"] = tmp_path / "ws"
    return box


def _post(env, data=b"\xff\xd8img", ctype="image/jpeg", headers=None):
    return env["client"].post(
        "/api/vision/receipt", content=data,
        headers={"Content-Type": ctype, "X-Vision-Token": "s3cret", **(headers or {})}
    )


def test_success(env):
    r = _post(env)
    assert r.status_code == 200
    lines = r.json()["lines"]
    assert [l["name"] for l in lines] == ["Latte UHT", "Pasta", "Banane"]
    assert lines[1] == {"raw_text": "PASTA 500G", "name": "Pasta", "quantity": 500.0, "unit": "g"}
    # a unit outside g/ml/pz is dropped together with its quantity
    assert lines[0]["quantity"] is None and lines[0]["unit"] is None
    prompt, _, persona, kw = env["seen"][0]
    assert "Read" in prompt and "receipt" in prompt and persona
    assert not kw.get("resume_id")


def test_fenced_json_with_prose(env):
    env["reply"] = "Ecco:\n```json\n" + JSON_OK + "\n```\nfatto"
    assert len(_post(env).json()["lines"]) == 3


def test_garbage_is_502(env):
    env["reply"] = "non riesco a leggere"
    r = _post(env)
    assert r.status_code == 502 and "detail" in r.json()


def test_auth_error_is_503(env):
    env["reply"] = "❌ Not logged in · Please run /login"
    r = _post(env)
    assert r.status_code == 503 and "detail" in r.json()


def test_transport_error_and_crash_are_503(env):
    env["reply"] = "❌ claude: timeout"
    assert _post(env).status_code == 503
    env["reply"] = RuntimeError("boom")
    assert _post(env).status_code == 503


def test_timeout_is_504(env, monkeypatch):
    import asyncio
    env["reply"] = asyncio.TimeoutError()
    assert _post(env).status_code == 504


def test_non_image_is_415(env):
    r = _post(env, ctype="application/pdf")
    assert r.status_code == 415 and r.json()["detail"]
    assert not env["seen"]


def test_oversize_is_413(env):
    r = _post(env, data=b"0" * (vision_receipt.MAX_IMAGE_BYTES + 1))
    assert r.status_code == 413 and r.json()["detail"]
    assert not env["seen"]


def test_token_required(env):
    assert _post(env, headers={"X-Vision-Token": ""}).status_code == 401
    assert _post(env, headers={"X-Vision-Token": "wrong"}).status_code == 401
    assert _post(env).status_code == 200
    assert len(env["seen"]) == 1


def test_unset_token_refuses_instead_of_accepting(env, monkeypatch):
    """#88: a device without VISION_TOKEN closes the route, it does not open it."""
    monkeypatch.delenv("VISION_TOKEN")
    r = _post(env, headers={"X-Vision-Token": "anything"})
    assert r.status_code == 503
    assert not env["seen"]


def test_temp_dir_removed_on_success_and_failure(env):
    paths = []
    orig = env["seen"]

    _post(env)
    ws = Path(orig[0][1])
    assert not ws.exists()
    env["reply"] = "garbage"
    _post(env)
    assert not Path(orig[1][1]).exists()
    assert list(env["ws"].iterdir()) == []
