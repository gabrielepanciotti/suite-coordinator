"""Watchdog esterno di coordinator-bot (#76): docker finto, stato su tmp."""
import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/watchdog/coordinator-watchdog.sh"


def _fake_docker(tmp: Path, ps_rows: str) -> Path:
    log = tmp / "docker.log"
    exe = tmp / "docker"
    exe.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {log}\n'
        'case "$1" in\n'
        f'  ps) if [[ "$*" == *"{{{{.ID}}}}"* ]]; then printf "%s" "{ps_rows}"; '
        f'else printf "%s\\n" "$(cut -d" " -f2 <<< "{ps_rows}" | grep -m1 running)"; fi;;\n'
        '  inspect) exit 1;;\n'
        'esac\n'
    )
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return log


def _run(tmp: Path, n: int = 1) -> str:
    env = {**os.environ, "WATCHDOG_DOCKER": str(tmp / "docker"), "WATCHDOG_STATE_DIR": str(tmp / "st"),
           "WATCHDOG_DRY_RUN": "0", "HOME": str(tmp), "WATCHDOG_FAIL_THRESHOLD": "2"}
    out = ""
    for _ in range(n):
        out += subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True).stdout
    return out


def test_up_non_fa_nulla(tmp_path):
    log = _fake_docker(tmp_path, "abc running coordinator-bot\n")
    _run(tmp_path, 3)
    assert "start" not in log.read_text()


def test_created_con_nome_hash_viene_rinominato_e_avviato(tmp_path):
    log = _fake_docker(tmp_path, "abc created d7759f537b6c_coordinator-bot\n")
    out = _run(tmp_path, 2)
    calls = log.read_text()
    assert "rename abc coordinator-bot" in calls
    assert "start abc" in calls
    assert "ripristino (tentativo 1/3)" in out


def test_sotto_soglia_non_agisce(tmp_path):
    log = _fake_docker(tmp_path, "abc created coordinator-bot\n")
    _run(tmp_path, 1)
    assert "start" not in log.read_text()


def test_tetto_tentativi_poi_needs_human(tmp_path):
    _fake_docker(tmp_path, "abc exited coordinator-bot\n")
    out = _run(tmp_path, 12)
    assert out.count("ripristino (tentativo") == 3
    assert out.count("NEEDS HUMAN") == 1
