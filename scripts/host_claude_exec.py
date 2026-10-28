#!/usr/bin/env python3
"""Run ``claude`` on the HOST, as the host user, for a session woken by the bus.

Issue #46. A session the Coordinator spawns from a bus conversation must be the
same session the owner gets by opening a terminal in that repo: same user, same
HOME, same real cwd, same ``gh``/ssh credentials, same project memory (Claude
Code indexes memory by cwd, so ``/workspaces/x`` and ``~/work/X`` are
two different memories). Inside the container none of that holds, and chasing
it mount by mount is how #244 happened. So the container only decides *who* to
wake and *with what prompt*; the run itself happens here.

Invoked by sshd as a FORCED command (``authorized_keys``: ``restrict,command=``),
so the key can do this and nothing else. Protocol, all on stdin/stdout:

1. the client writes ONE JSON line: ``{"cwd": ..., "args": [...], "env": {...}}``;
2. this script starts ``claude <args>`` in ``cwd`` through a login shell (the
   same environment a terminal has: PATH, profile, ~/.config/gh);
3. claude's stdout/stderr ARE this process's stdout/stderr (stream-json flows
   straight back through ssh);
4. the client keeps stdin open for the whole run. EOF on stdin means the client
   gave up (idle watchdog, timeout, container restart): the whole process group
   is killed. Without this a killed ssh client would leave a hung claude running
   on the host, since no pty means no SIGHUP.

Guards, because a forced command is only as narrow as what it accepts:
``cwd`` must resolve under one of ``HOST_EXEC_ROOTS`` (default: the user's
``~/work``, ``~/personal``, ``~/.claude``) AND inside a git repo (optionally one of
``HOST_EXEC_REPOS``), never in a ``.git``/``.ssh``/``secrets`` directory; the binary
is fixed; the arguments are an allowlist of flags with checked values (no
``--settings``, ``--mcp-config``, ``--add-dir``, ``--dangerously-skip-permissions``
...); and only ``HUB_*`` variables may be injected into the environment.
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

ENV_PREFIX_ALLOWED = "HUB_"
DEFAULT_ROOTS = ("~/work", "~/personal", "~/.claude")
EXIT_BAD_REQUEST = 64
KILL_GRACE_S = 10


def allowed_roots() -> list[Path]:
    raw = os.environ.get("HOST_EXEC_ROOTS", "")
    items = [r for r in raw.split(os.pathsep) if r.strip()] or list(DEFAULT_ROOTS)
    return [Path(r).expanduser().resolve() for r in items]


PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._\-\[\]]{1,64}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Path components a session must never start in, whatever repo they sit under.
FORBIDDEN_PARTS = frozenset({".git", ".ssh", ".gnupg", "secrets", ".venv", "node_modules"})


def _free_text(flag: str, value: str) -> None:
    # A value starting with "-" could be read as another flag by the CLI's own
    # parser: that would smuggle `--settings`/`--mcp-config` past this list.
    if value.startswith("-"):
        raise ValueError(f"{flag}: value must not start with '-'")


def validate_args(args: list[str]) -> None:
    """Only the flags the Coordinator really sends, each with a checked value.

    ``claude`` is a program that runs anything: ``--settings`` (hooks),
    ``--mcp-config``, ``--plugin-dir``, ``--add-dir``,
    ``--dangerously-skip-permissions``... A compromised container must not be able
    to pick those, so this is an allowlist, not a denylist (Life#153 M6).
    """
    modes = tuple(
        m for m in os.environ.get("HOST_EXEC_PERMISSION_MODES", "").split(",") if m.strip()
    ) or PERMISSION_MODES
    i = 0
    while i < len(args):
        flag = args[i]
        if flag == "--verbose":
            i += 1
            continue
        if flag not in ("-p", "--permission-mode", "--output-format", "--model", "--effort",
                        "--append-system-prompt", "--resume"):
            raise ValueError(f"argument not allowed: {flag}")
        if i + 1 >= len(args):
            raise ValueError(f"{flag}: missing value")
        value = args[i + 1]
        if flag in ("-p", "--append-system-prompt"):
            _free_text(flag, value)
        elif flag == "--permission-mode" and value not in modes:
            raise ValueError(f"--permission-mode {value!r} not allowed")
        elif flag == "--output-format" and value != "stream-json":
            raise ValueError(f"--output-format {value!r} not allowed")
        elif flag == "--model" and not _MODEL_RE.match(value):
            raise ValueError("--model: invalid value")
        elif flag == "--effort" and value not in EFFORT_LEVELS:
            raise ValueError(f"--effort {value!r} not allowed")
        elif flag == "--resume" and not _UUID_RE.match(value):
            raise ValueError("--resume: not a session id")
        i += 2


def validate_cwd(cwd: Path, roots: list[Path]) -> None:
    """``cwd`` must sit inside a git repo under a root (a registered project), not
    in an arbitrary folder of the home, and not in a secrets/vcs directory."""
    root = next(r for r in roots if cwd == r or r in cwd.parents)
    parts = cwd.relative_to(root).parts
    if FORBIDDEN_PARTS & set(parts):
        raise ValueError(f"cwd {cwd} is in a forbidden directory")
    if os.environ.get("HOST_EXEC_REQUIRE_GIT", "1") != "0":
        top = next(
            (p for p in [cwd, *cwd.parents] if (p / ".git").exists() and (p == root or root in p.parents)),
            None,
        )
        if top is None:
            raise ValueError(f"cwd {cwd} is not inside a git repo")
    repos = [r.strip() for r in os.environ.get("HOST_EXEC_REPOS", "").split(",") if r.strip()]
    if repos:
        name = parts[0] if parts else root.name
        if name not in repos:
            raise ValueError(f"repo {name} is not in the allowed list")


def validate_request(req: object, roots: list[Path]) -> tuple[Path, list[str], dict]:
    """Return ``(cwd, args, env)`` or raise ``ValueError`` with the reason."""
    if not isinstance(req, dict):
        raise ValueError("request is not a JSON object")
    cwd_raw = req.get("cwd")
    if not isinstance(cwd_raw, str) or not cwd_raw:
        raise ValueError("missing cwd")
    cwd = Path(cwd_raw).expanduser().resolve()
    # Roots first: a path outside them is refused the same way whether or not
    # it exists, so the caller cannot use this key to probe the host's disk.
    if not any(cwd == r or r in cwd.parents for r in roots):
        raise ValueError(f"cwd {cwd} is outside the allowed roots")
    if not cwd.is_dir():
        raise ValueError(f"cwd {cwd} is not a directory on this host")
    validate_cwd(cwd, roots)
    args = req.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError("args must be a list of strings")
    validate_args(args)
    env = req.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError("env must be an object")
    bad = [k for k in env if not str(k).startswith(ENV_PREFIX_ALLOWED)]
    if bad:
        raise ValueError(f"env keys not allowed: {', '.join(sorted(bad))}")
    return cwd, args, {str(k): str(v) for k, v in env.items()}


def check_node_trust(cwd: Path) -> None:
    """Refuse a repo whose auto-executing files nobody approved (Life#153 M7).

    Delegates to ``node_trust.py`` of the config repo: a repo of ours is trusted,
    a fresh clone or a third party's needs its content hash approved by hand.
    HOST_EXEC_NODE_TRUST=warn logs instead of refusing, =off skips; if the script
    is not on this device the check is skipped with a notice.
    """
    mode = os.environ.get("HOST_EXEC_NODE_TRUST", "enforce")
    if mode == "off":
        return
    script = Path(
        os.environ.get("HOST_EXEC_NODE_TRUST_SCRIPT")
        or Path("~/.claude/scripts/node_trust.py").expanduser()
    )
    if not script.is_file():
        sys.stderr.write(f"host_claude_exec: node_trust not found at {script}, check skipped\n")
        return
    try:
        r = subprocess.run(
            [sys.executable, str(script), "check", str(cwd)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"node trust check failed: {exc}") from exc
    if r.returncode == 0:
        return
    if mode == "warn":
        sys.stderr.write(f"host_claude_exec: WARNING {r.stderr.strip()}\n")
        return
    raise ValueError(f"repo not approved: {r.stderr.strip() or r.stdout.strip()}")


def build_command(args: list[str]) -> list[str]:
    """``claude <args>`` behind a login shell, exec'd so the pid is claude's."""
    binary = os.environ.get("HOST_EXEC_CLAUDE_BIN", "claude")
    return ["bash", "-lc", 'exec "$0" "$@"', binary, *args]


OP_INSTANCE_TELEGRAM = "instance-telegram"
OP_SENTINELLA_SSH = "sentinella-ssh"
OP_INSTANCE_IDEA = "instance-idea"
OP_LLM_ROUTE = "llm-route"


def main() -> int:
    line = sys.stdin.buffer.readline()
    try:
        req = json.loads(line or b"null")
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"host_claude_exec: rejected: {exc}\n")
        return EXIT_BAD_REQUEST
    # The one non-claude operation this key allows (#82): link an instance's
    # Telegram group. Narrow by construction, see host_instance_link.py.
    if isinstance(req, dict) and req.get("op") == OP_INSTANCE_TELEGRAM:
        import host_instance_link

        print(json.dumps(host_instance_link.link_telegram(req)), flush=True)
        return 0
    # A voice note / /ricorda row for the queue of the chat's instance (#78):
    # the instance checkouts are read-only in the container.
    if isinstance(req, dict) and req.get("op") == OP_INSTANCE_IDEA:
        import host_instance_link

        print(json.dumps(host_instance_link.queue_idea(req)), flush=True)
        return 0
    # The sentinel's probes on the prod box (#89): the container asks, the host
    # holds the key and accepts only the sentinel's own commands.
    if isinstance(req, dict) and req.get("op") == OP_SENTINELLA_SSH:
        import host_sentinella

        return host_sentinella.run(req)
    # The rest of a wake's LLM queue (#91): same cwd/env guards as a claude run,
    # then Life's engine under Life's python (host_llm_route.py).
    if isinstance(req, dict) and req.get("op") == OP_LLM_ROUTE:
        import host_llm_route

        try:
            cwd, _, extra_env = validate_request({**req, "args": []}, allowed_roots())
            check_node_trust(cwd)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": f"rejected: {exc}", "attempts": []}), flush=True)
            return 0
        return host_llm_route.serve(req, cwd, extra_env)
    try:
        cwd, args, extra_env = validate_request(req, allowed_roots())
        check_node_trust(cwd)
        claude_dir = None
        if req.get("profile") is not None:
            import host_llm_route

            claude_dir = host_llm_route.profile_claude_dir(req["profile"])
    except ValueError as exc:
        sys.stderr.write(f"host_claude_exec: rejected: {exc}\n")
        return EXIT_BAD_REQUEST

    env = {**os.environ, **extra_env}
    if claude_dir:
        env["CLAUDE_CONFIG_DIR"] = claude_dir
    # sshd's forced command inherits SSH_ORIGINAL_COMMAND & co.: not the
    # terminal's environment, and nothing claude should see.
    for k in ("SSH_ORIGINAL_COMMAND", "SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"):
        env.pop(k, None)
    proc = subprocess.Popen(
        build_command(args),
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    def _watch_client() -> None:
        # Blocks until the client closes its end (or dies): then tear down.
        sys.stdin.buffer.read()
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
    try:
        return proc.wait()
    except KeyboardInterrupt:
        os.killpg(proc.pid, signal.SIGTERM)
        return proc.wait()


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # os._exit, not sys.exit: the watcher thread is blocked reading stdin, and a
    # normal interpreter shutdown aborts (SIGABRT) trying to lock that buffer.
    # A negative code is "killed by signal N": report it the shell way.
    os._exit(rc if rc >= 0 else 128 - rc)
