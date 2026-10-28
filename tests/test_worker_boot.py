"""worker.py boots headless: validates OAuth + refuses ANTHROPIC_API_KEY."""

import json
import time

import pytest
import worker


@pytest.fixture(autouse=True)
def _no_env_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


def test_long_lived_env_token_is_usable_without_a_credentials_file(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    empty = tmp_path / ".credentials.json"
    empty.write_text("")
    assert worker.stato_credenziali(empty)[0] is True
    worker.validate_auth(creds_path=empty)  # no raise


def test_refuses_anthropic_api_key(monkeypatch, tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text("{}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-paid")
    with pytest.raises(SystemExit) as e:
        worker.validate_auth(creds_path=creds)
    assert "ANTHROPIC_API_KEY" in str(e.value)


def test_requires_oauth_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    missing = tmp_path / "nope.json"
    with pytest.raises(SystemExit) as e:
        worker.validate_auth(creds_path=missing)
    assert "credenziali" in str(e.value).lower()


def _scrivi(tmp_path, oauth: dict):
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    return creds


def test_validate_auth_ok(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds = _scrivi(tmp_path, {"refreshToken": "r", "expiresAt": 0})
    worker.validate_auth(creds_path=creds)  # no raise


def test_un_token_scaduto_ma_rinnovabile_e_normale(tmp_path):
    """E' il caso di ogni giorno: la CLI lo rinnova da sola."""
    creds = _scrivi(tmp_path, {"refreshToken": "r", "expiresAt": 1})
    assert worker.stato_credenziali(creds)[0] is True


def test_scaduto_senza_refresh_e_morto(tmp_path):
    """La forma esatta osservata in prod: expiresAt=0, nessun refresh."""
    creds = _scrivi(tmp_path, {"expiresAt": 0, "subscriptionType": "max"})
    utilizzabili, perche = worker.stato_credenziali(creds)
    assert utilizzabili is False
    assert "expiresAt=0" in perche


def test_un_file_che_esiste_non_basta(monkeypatch, tmp_path):
    """Il guasto stesso riscrive il file a ogni tentativo: la sua esistenza,
    e persino la sua freschezza, non dicono niente (issue #34)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds = _scrivi(tmp_path, {"expiresAt": 0})
    with pytest.raises(SystemExit) as e:
        worker.validate_auth(creds_path=creds)
    assert "/login" in str(e.value)


def test_scadenza_in_millisecondi(tmp_path):
    """La CLI e' passata ai millisecondi: si accettano entrambi i formati."""
    futuro_ms = (time.time() + 3600) * 1000
    assert worker.stato_credenziali(_scrivi(tmp_path, {"expiresAt": futuro_ms}))[0]
    passato_ms = (time.time() - 3600) * 1000
    assert not worker.stato_credenziali(_scrivi(tmp_path, {"expiresAt": passato_ms}))[0]
