#!/usr/bin/env python3
"""LLM queue of bus wakes, host side (Coordinator#91, Life#164).

Two roles, one file:

* imported by ``host_claude_exec.py`` (system python, no Life packages):
  ``profile_claude_dir`` resolves the ``CLAUDE_CONFIG_DIR`` of the primary run's
  profile, and ``serve`` handles the ``llm-route`` op — it validates the request
  and starts this same file under Life's python;
* run as ``python host_llm_route.py --engine`` under Life's python: reads the
  validated request on stdin, walks the queue with
  ``suite_backend_core.llm_routing.route`` and prints one JSON line
  ``{"ok", "text", "attempts", "error"}``.

The client names the instance by its host path, which must be a path of the
device registry (``$LIFE_HOME/instances.json``), and the profile by NAME: paths
always come from the host's own files, never from the container.
``ANTHROPIC_API_KEY`` is never read here: the engine puts it in its child's env.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

CONFIG_REL = Path("config") / "llm-routing.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_FORZA_RE = re.compile(r"^[a-z-]+(:[A-Za-z0-9_.-]{1,64})?(/[A-Za-z0-9_.:@+/\[\]-]{0,128})?$")
MAX_SKIP = 10
KILL_GRACE_S = 10


def life_home() -> Path:
    raw = os.environ.get("LIFE_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".life"


def registered_instance(path: str) -> Path:
    """``path`` resolved, if it is an instance of this device's registry."""
    if not isinstance(path, str) or not path:
        raise ValueError("missing instance")
    want = Path(path).expanduser().resolve()
    try:
        reg = json.loads((life_home() / "instances.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"device registry unreadable: {exc}") from exc
    for v in (reg.get("instances") or {}).values():
        if isinstance(v, dict) and Path(str(v.get("path", ""))).expanduser().resolve() == want:
            return want
    raise ValueError(f"instance {want} is not registered on this device")


def _config(inst: Path) -> dict:
    try:
        return json.loads((inst / CONFIG_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{inst / CONFIG_REL} unreadable: {exc}") from exc


def profile_claude_dir(profile: object) -> str | None:
    """``CLAUDE_CONFIG_DIR`` for ``{"instance", "name"}``; None = the default ``~/.claude``.

    The default dir is left unset on purpose: with CLAUDE_CONFIG_DIR set, Claude
    Code also moves ``.claude.json`` inside it, and the run would stop being the
    terminal's session (#46)."""
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    name = profile.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError("profile: invalid name")
    inst = registered_instance(profile.get("instance"))  # type: ignore[arg-type]
    p = (_config(inst).get("profili") or {}).get(name)
    if not isinstance(p, dict):
        raise ValueError(f"profile {name} not defined in {inst / CONFIG_REL}")
    raw = p.get("claudeDir")
    if not raw:
        return None
    d = Path(os.path.expanduser(str(raw))).resolve()
    if d == (Path.home() / ".claude").resolve():
        return None
    if not d.is_dir():
        raise ValueError(f"profile {name}: {d} does not exist on this host")
    return str(d)


def life_framework() -> Path:
    """Life's checkout: LIFE_FRAMEWORK_DIR, else the usual places of a device."""
    roots = [os.environ.get("LIFE_FRAMEWORK_DIR"), "~/work/Life", "~/Life", "~/personal/Life"]
    for root in filter(None, roots):
        base = Path(root).expanduser()
        if (base / "packages" / "backend-core" / "suite_backend_core").is_dir():
            return base
    raise ValueError("Life framework not found on this host (set LIFE_FRAMEWORK_DIR)")


def _imports_engine(python: str, base: Path) -> bool:
    """Can ``python`` import the engine? Existing is not enough (#95: the shared
    venv has no alembic, and suite_backend_core imports it at package level)."""
    env = {**os.environ, "PYTHONPATH": str(base / "packages" / "backend-core")}
    try:
        return subprocess.run([python, "-c", "import suite_backend_core.llm_routing"], env=env,
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def life_python() -> str:
    """The python for the engine: the first venv that imports it (same rule as Life's llm-run)."""
    explicit = os.environ.get("LLM_ROUTING_PYTHON")
    if explicit:
        return explicit
    base = life_framework()
    for rel in ("packages/backend-core/.venv/bin/python", "packages/shared/.venv/bin/python",
                "backend/.venv/bin/python"):
        if (base / rel).exists() and _imports_engine(str(base / rel), base):
            return str(base / rel)
    raise ValueError(f"no venv under {base} imports the engine (set LLM_ROUTING_PYTHON)")


def engine_env(base: dict) -> dict:
    """``base`` + Life's backend-core on PYTHONPATH (the venv does not install it)."""
    pkg = str(life_framework() / "packages" / "backend-core")
    old = base.get("PYTHONPATH")
    env = {**base, "PYTHONPATH": f"{pkg}{os.pathsep}{old}" if old else pkg}
    # A forced command gets sshd's bare PATH, and opencode's installer puts the
    # CLI in ~/.opencode/bin, outside even a login PATH: without these the engine
    # skips every opencode/codex entry as "not installed here".
    path = env.get("PATH", os.defpath).split(os.pathsep)
    extra = [str(Path(d).expanduser()) for d in ("~/.opencode/bin", "~/.local/bin", "~/.npm-global/bin")]
    env["PATH"] = os.pathsep.join([d for d in extra if Path(d).is_dir() and d not in path] + path)
    return env


def validate(req: dict) -> dict:
    """The engine request, checked; raises ValueError. cwd is checked by the caller."""
    out: dict = {"instance": str(registered_instance(req.get("instance"))), "cwd": req["cwd"]}
    prompt = req.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("missing prompt")
    out["prompt"] = prompt
    forza = req.get("forza")
    if forza:
        if not isinstance(forza, str) or not _FORZA_RE.match(forza):
            raise ValueError("forza: invalid value")
        out["forza"] = forza
    else:
        kind = "servizio" if req.get("servizio") else "categoria"
        name = req.get(kind)
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"{kind}: invalid value")
        out[kind] = name
        skip = req.get("skip", 0)
        if not isinstance(skip, int) or not 0 <= skip <= MAX_SKIP:
            raise ValueError("skip: invalid value")
        out["skip"] = skip
    return out


def serve(req: dict, cwd: Path, extra_env: dict) -> int:
    """The ``llm-route`` op: run the engine under Life's python, killed on client EOF."""
    try:
        clean = validate({**req, "cwd": str(cwd)})
        py = life_python()
        env = engine_env({**os.environ, **extra_env})
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "attempts": []}), flush=True)
        return 0
    for k in ("SSH_ORIGINAL_COMMAND", "SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY", "ANTHROPIC_API_KEY"):
        env.pop(k, None)
    proc = subprocess.Popen(
        [py, str(Path(__file__).resolve()), "--engine"],
        cwd=str(cwd), env=env, stdin=subprocess.PIPE, start_new_session=True,
    )
    proc.stdin.write((json.dumps(clean) + "\n").encode())  # type: ignore[union-attr]
    proc.stdin.close()  # type: ignore[union-attr]

    def _watch_client() -> None:
        sys.stdin.buffer.read()  # the container gave up -> tear the engine down
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if proc.poll() is not None:
                return
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                continue

    threading.Thread(target=_watch_client, daemon=True).start()
    return proc.wait()


def engine_main() -> int:
    """Under Life's python: walk the queue and print the outcome as one JSON line."""
    from suite_backend_core.llm_routing import load_config, parse_force, route

    req = json.loads(sys.stdin.readline())
    inst = Path(req["instance"])
    cfg = load_config(inst)
    force = parse_force(req["forza"]) if req.get("forza") else None
    cat, serv = req.get("categoria"), req.get("servizio")
    if force is None:
        cfg = cfg.model_copy(deep=True)
        compito = cfg.resolve(cat, serv)
        # A v2 service names a matrix `tipo` and has no queue of its own: the engine
        # computes it, and there is nothing to skip into.
        if getattr(compito, "tipo", None):
            if int(req.get("skip") or 0):
                print(json.dumps({"ok": False, "error": "skip non supportato per un servizio con tipo", "attempts": []}))
                return 0
        else:
            compito.coda = list(compito.coda)[int(req.get("skip") or 0):]
        if not compito.tipo and not compito.coda:
            print(json.dumps({"ok": False, "error": "nessun elemento successivo in coda", "attempts": []}))
            return 0
    elif not cat and not serv:
        cat = "sessione"
    res = route(cfg, categoria=cat, servizio=serv, task=req["prompt"], force=force, inst=inst,
                directory=Path(req["cwd"]), say=lambda s: print(s, file=sys.stderr, flush=True))
    print(json.dumps({
        "ok": res.ok,
        "text": res.text,
        "attempts": [{"voce": a.entry, "esito": a.outcome, "motivo": a.reason[:300]} for a in res.attempts],
        "duration_s": round(res.duration_s, 1),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--engine"]:
        raise SystemExit(engine_main())
    print("usage: host_llm_route.py --engine  (request on stdin)", file=sys.stderr)
    raise SystemExit(2)
