"""#89 / Life#153 C3: the bot container holds no docker, no MSAL cache, no ssh key.

What it needs from those now goes through the host bridge: the sentinel's probes
(op ``sentinella-ssh``, accepted only for the sentinel's own commands).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import sentinella_config
import sentinella_probes
from sentinella_config import Servizio

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_COMPOSE = ROOT / "docker-compose.yml"
sys.path.insert(0, str(ROOT / "scripts"))

import host_sentinella  # noqa: E402


def _bot_service() -> dict:
    if not PRIVATE_COMPOSE.is_file():
        pytest.skip("the credential-free production compose file is intentionally absent from the public export")
    data = yaml.safe_load(PRIVATE_COMPOSE.read_text(encoding="utf-8"))
    return data["services"]["coordinator-bot"]


def _volume_targets(svc: dict) -> list[str]:
    out = []
    for v in svc.get("volumes", []):
        if isinstance(v, str):
            out.append(v.rsplit(":", 2)[1] if v.count(":") >= 2 else v.split(":")[-1])
        else:
            out.append(v.get("target", ""))
    return out


# --- compose ------------------------------------------------------------------


def test_bot_has_no_docker_socket_nor_docker_group():
    svc = _bot_service()
    assert not any("docker.sock" in str(v) for v in svc.get("volumes", []))
    assert svc.get("group_add") == ["101"]


def test_bot_has_no_msal_cache_and_masks_workspace_secrets():
    svc = _bot_service()
    assert "/m365" not in _volume_targets(svc)
    assert "MICROSOFT_TOKEN_CACHE_FILE" not in (svc.get("environment") or {})
    masked = {t.split(":")[0] for t in svc.get("tmpfs", [])}
    assert "/workspaces/db/api/secrets" in masked
    assert any(target.startswith("/workspaces/") and target.endswith("Graph/secrets")
               for target in masked)


def test_bot_mounts_only_the_host_exec_key():
    keys = [t for t in _volume_targets(_bot_service()) if "/.ssh/" in t]
    assert keys == ["/home/app/.ssh/host_exec_key"]


# --- one place for the sentinel's commands -------------------------------------


def _servizio(**kw) -> Servizio:
    base = dict(chiave="portale", titolo="P", dominio="p.it", url_salute="",
                container=("a",), compose_file="docker-compose.prod.yml",
                project_dir="/mnt/c/srv/P", critico=True,
                comando_salute='curl -s http://127.0.0.1:8003/api/health',
                comando_vivacita="echo 2026", motore="wsl")
    base.update(kw)
    return Servizio(**base)


def test_repair_command_is_unchanged():
    s = _servizio()
    assert sentinella_config.comando_riparazione(s) == (
        "wsl -d Ubuntu-24.04 -u root --exec bash -c "
        "'cd \"/mnt/c/srv/P\" && docker compose -f docker-compose.prod.yml up -d'")
    d = _servizio(motore="desktop")
    assert sentinella_config.comando_riparazione(d) == (
        'cd "/mnt/c/srv/P" && docker compose -f docker-compose.prod.yml up -d')


def test_allowlist_covers_every_probe_and_nothing_else():
    s = _servizio()
    ammessi = sentinella_config.comandi_ammessi([s])
    pre = sentinella_config.PREFISSO_WSL
    for cmd in (pre + "docker info --format ok", pre + 'docker ps --format "{{.Names}}"',
                sentinella_config.comando_riparazione(s), s.comando_salute,
                s.comando_vivacita, sentinella_config.COMANDO_DISCO):
        assert cmd in ammessi
    assert "rm -rf /" not in ammessi
    assert "docker ps" not in ammessi


# --- container side: via the host bridge ----------------------------------------


def test_via_host_when_the_key_is_gone(monkeypatch):
    monkeypatch.delenv("SENTINELLA_VIA_HOST", raising=False)
    monkeypatch.setenv("HOST_EXEC_SSH", "dev@host.docker.internal")
    monkeypatch.setattr(sentinella_probes, "SSH_KEY", "/nonexistent/key")
    assert sentinella_probes.via_host() is True
    monkeypatch.setenv("SENTINELLA_VIA_HOST", "0")
    assert sentinella_probes.via_host() is False
    monkeypatch.delenv("SENTINELLA_VIA_HOST")
    monkeypatch.delenv("HOST_EXEC_SSH")
    assert sentinella_probes.via_host() is False


# --- host side: the bridge op ----------------------------------------------------


def _host_config(tmp_path: Path, servizi: list[dict]) -> Path:
    (tmp_path / "sentinella_servizi.json").write_text(json.dumps(servizi), encoding="utf-8")
    cfg = tmp_path / "sentinella.json"
    cfg.write_text(json.dumps({"ssh_host": "Administrator@box", "ssh_key": "~/.ssh/k"}),
                   encoding="utf-8")
    return cfg


SERVIZIO = {"chiave": "portale", "dominio": "p.it", "container": ["a"],
            "project_dir": "/srv/p", "comando_salute": "curl -s http://127.0.0.1:1/h",
            "motore": "wsl"}


def test_host_accepts_a_sentinel_command_and_targets_its_own_box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_SENTINELLA_CONFIG", str(_host_config(tmp_path, [SERVIZIO])))
    cfg = host_sentinella.load_config()
    cmd = host_sentinella.validate({"cmd": SERVIZIO["comando_salute"]}, cfg)
    argv = host_sentinella.ssh_argv(cfg, cmd)
    assert argv[-2:] == ["Administrator@box", SERVIZIO["comando_salute"]]
    assert argv[0] == "ssh"


@pytest.mark.parametrize("req", [
    {"cmd": "cat ~/.ssh/id_ed25519"},
    {"cmd": ""},
    {"cmd": ["docker", "ps"]},
    {"cmd": "docker info --format ok; rm -rf /"},
    "not a dict",
])
def test_host_refuses_anything_else(tmp_path, monkeypatch, req):
    monkeypatch.setenv("HOST_SENTINELLA_CONFIG", str(_host_config(tmp_path, [SERVIZIO])))
    with pytest.raises(ValueError):
        host_sentinella.validate(req, host_sentinella.load_config())


def test_host_ignores_a_target_in_the_request(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_SENTINELLA_CONFIG", str(_host_config(tmp_path, [SERVIZIO])))
    cfg = host_sentinella.load_config()
    req = {"cmd": "docker info --format ok", "ssh_host": "root@evil", "ssh_key": "/x"}
    argv = host_sentinella.ssh_argv(cfg, host_sentinella.validate(req, cfg))
    assert "root@evil" not in argv and "Administrator@box" in argv


def test_forced_command_rejects_a_bad_sentinel_request(tmp_path):
    cfg = _host_config(tmp_path, [SERVIZIO])
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "host_claude_exec.py")],
        input=json.dumps({"op": "sentinella-ssh", "cmd": "id"}) + "\n",
        capture_output=True, text=True, timeout=30,
        env={"HOST_SENTINELLA_CONFIG": str(cfg), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == host_sentinella.EXIT_BAD_REQUEST
    assert "allowlist" in r.stderr
