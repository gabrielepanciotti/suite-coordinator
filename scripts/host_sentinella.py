"""The sentinel's ssh probes, run by the HOST on behalf of the container (#89).

The container used to hold the prod box's ssh key (an Administrator key on
integra). Now it only *asks*: through the host-exec forced command it sends
``{"op": "sentinella-ssh", "cmd": "..."}`` and this module runs the command on
the box, with a key and a target the container never sees.

Narrow by construction:

- the box and the key come from a host-only file (``HOST_SENTINELLA_CONFIG``,
  default ``~/.config/coordinator/sentinella.json``), never from the request;
- ``cmd`` must be one of ``sentinella_config.comandi_ammessi(servizi)``, where the
  services list is read from a host-only path too (``servizi_path`` in that file).
  The container's own ``data/sentinella_servizi.json`` does NOT count: the
  container can write it, so trusting it would let it pick any command;
- the command travels as one argv item to ``ssh``: the box's shell parses it,
  exactly as the in-container probe did, and nothing parses it here.

Exit code and output are the remote ones (255 = box unreachable), so the probes
read them unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sentinella_config  # noqa: E402

EXIT_BAD_REQUEST = 64
TIMEOUT_S = 45
DEFAULT_CONFIG = "~/.config/coordinator/sentinella.json"


def load_config() -> dict:
    path = Path(os.environ.get("HOST_SENTINELLA_CONFIG") or DEFAULT_CONFIG).expanduser()
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict) or not cfg.get("ssh_host") or not cfg.get("ssh_key"):
        raise ValueError(f"{path}: ssh_host and ssh_key are required")
    servizi = Path(str(cfg.get("servizi_path") or path.with_name("sentinella_servizi.json")))
    cfg["servizi"] = sentinella_config.carica(str(servizi.expanduser()))
    return cfg


def validate(req: object, cfg: dict) -> str:
    if not isinstance(req, dict):
        raise ValueError("request is not a JSON object")
    cmd = req.get("cmd")
    if not isinstance(cmd, str) or not cmd:
        raise ValueError("missing cmd")
    if cmd not in sentinella_config.comandi_ammessi(cfg["servizi"]):
        raise ValueError("command not in the sentinel allowlist")
    return cmd


def ssh_argv(cfg: dict, cmd: str) -> list[str]:
    return [
        "ssh", "-i", str(Path(cfg["ssh_key"]).expanduser()),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        str(cfg["ssh_host"]), cmd,
    ]


def run(req: object) -> int:
    try:
        cfg = load_config()
        cmd = validate(req, cfg)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"host_sentinella: rejected: {exc}\n")
        return EXIT_BAD_REQUEST
    try:
        r = subprocess.run(ssh_argv(cfg, cmd), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        sys.stdout.write("host_sentinella: timeout\n")
        return 255
    sys.stdout.buffer.write(r.stdout)
    return r.returncode
