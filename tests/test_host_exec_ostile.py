"""ComUnity#199: prompt e system prompt ostili sono DATI, mai comandi della shell.

Due fronti: il wrapper sull'host (`build_command`) e il comando remoto che il
client manda accanto alla richiesta JSON quando manca il `command=` forzato.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import claude_runner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_claude_exec  # noqa: E402

posix_only = pytest.mark.skipif(os.name == "nt", reason="bash sull'host Linux")

PAYLOADS = [
    "`touch {m}` $(touch {m}) ; touch {m}",
    "\"doppi\" 'singoli' \\ backslash $HOME ${PATH} !! * ?",
    "riga1\nriga2\n`settore-dev` ⟦CONFIRM⟧ main backend/ scripts/hotfix.sh",
    " -- --dangerously-skip-permissions | tee {m} & >{m} <{m}",
    "unicode: àèìòù 日本語 🚀 ⟦x⟧",
]


@posix_only
@pytest.mark.parametrize("raw", PAYLOADS)
def test_wrapper_passa_argomenti_identici_senza_interpretazione(tmp_path, monkeypatch, raw):
    marker = tmp_path / "eseguito"
    payload = raw.replace("{m}", str(marker))
    fake = tmp_path / "fake-claude"
    fake.write_text(
        "#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("HOST_EXEC_CLAUDE_BIN", str(fake))
    args = ["-p", payload, "--append-system-prompt", payload]
    host_claude_exec.validate_args(args)
    out = subprocess.run(
        host_claude_exec.build_command(args), capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(out) == args
    assert not marker.exists()


@pytest.mark.parametrize("raw", PAYLOADS)
def test_la_richiesta_e_una_sola_riga_json_che_torna_identica(raw):
    req = claude_runner.host_exec_request(
        ["-p", raw, "--append-system-prompt", raw], host_cwd="/srv/x", extra_env=None
    )
    assert req.endswith(b"\n") and req.count(b"\n") == 1
    assert json.loads(req)["args"] == ["-p", raw, "--append-system-prompt", raw]


def test_il_comando_remoto_e_fisso_e_non_contiene_testo_utente():
    cmd = claude_runner.host_exec_command()
    assert cmd[-1] == claude_runner.HOST_EXEC_FAIL_CLOSED
    assert "-p" not in cmd and "--append-system-prompt" not in cmd


@posix_only
@pytest.mark.parametrize("raw", PAYLOADS)
def test_senza_command_forzato_lo_stdin_non_e_mai_eseguito(tmp_path, raw):
    """Simula sshd senza `command=`: il comando remoto gira in bash, lo stdin
    (la richiesta JSON ostile) deve restare dato."""
    marker = tmp_path / "eseguito"
    req = claude_runner.host_exec_request(
        ["-p", raw.replace("{m}", str(marker))], host_cwd="/srv/x", extra_env=None
    )
    r = subprocess.run(
        ["bash", "-c", claude_runner.HOST_EXEC_FAIL_CLOSED],
        input=req, capture_output=True,
    )
    assert r.returncode == 97
    assert b"forced command" in r.stderr
    assert b"command not found" not in r.stderr
    assert not marker.exists()
