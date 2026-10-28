# deployer_ops.py
"""Deployer operations: pull / recreate-worker / set-env / deploy.
I/O (subprocess, file) isolated here so the verbs are unit-testable with fakes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass

import deployer_config
from atomic_io import replace_with_retry

logger = logging.getLogger("coordinator.deployer_ops")


@dataclass
class OpResult:
    ok: bool
    summary: str
    log_tail: str = ""

    def format(self) -> str:
        icon = "✅" if self.ok else "❌"
        body = f"{icon} {self.summary}"
        if self.log_tail:
            body += f"\n```\n{self.log_tail}\n```"
        return body


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


async def _run(argv: list[str], cwd: str | None = None) -> tuple[int, str]:
    """Run argv (no shell), merge stderr into stdout, return (exit_code, output)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace")


def upsert_env(text: str, key: str, value: str) -> str:
    """Return .env text with KEY=value upserted, preserving every other line.
    Exact-key match only (a KEY=... line whose key equals `key`)."""
    out: list[str] = []
    found = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if stripped.split("=", 1)[0].strip() == key:
                out.append(f"{key}={value}")
                found = True
                continue
        out.append(line)
    if not found:
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


async def op_pull(cfg: deployer_config.DeployerConfig) -> OpResult:
    code, out = await _run(
        ["git", "-C", cfg.project_dir, "pull", "--ff-only", cfg.remote, cfg.branch]
    )
    if code != 0:
        return OpResult(False, f"pull failed (exit {code})", _tail(out))
    return OpResult(True, f"pulled {cfg.remote}/{cfg.branch}", _tail(out, 5))


async def op_recreate_worker(cfg: deployer_config.DeployerConfig) -> OpResult:
    code, out = await _run(
        [
            "docker",
            "compose",
            "-f",
            cfg.compose_file,
            "up",
            "-d",
            "--force-recreate",
            cfg.worker_service,
        ],
        cwd=cfg.project_dir,
    )
    if code != 0:
        return OpResult(False, f"recreate-worker failed (exit {code})", _tail(out))
    return OpResult(True, f"recreated {cfg.worker_service}", _tail(out, 8))


async def op_set_env(
    cfg: deployer_config.DeployerConfig, key: str, value: str
) -> OpResult:
    if not cfg.env_key_allowed(key):
        logger.warning("set-env refused: %r not in whitelist", key)
        return OpResult(False, f"set-env refused: '{key}' not in whitelist")
    path = os.path.join(cfg.project_dir, cfg.env_file)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    new = upsert_env(text, key, value)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new)
        replace_with_retry(tmp, path)  # atomic
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return OpResult(True, f"set {key} (recreate worker to apply)")


async def op_deploy(cfg: deployer_config.DeployerConfig) -> OpResult:
    pull = await op_pull(cfg)
    if not pull.ok:
        return OpResult(False, f"deploy aborted: {pull.summary}", pull.log_tail)
    rec = await op_recreate_worker(cfg)
    return OpResult(rec.ok, f"deploy: {pull.summary}; {rec.summary}", rec.log_tail)


async def op_verify(cfg: deployer_config.DeployerConfig) -> OpResult:
    """Check that the worker service is up. `docker compose ps --format json`
    emits one JSON object per line on most Compose v2 builds, but some versions
    emit a single-line JSON array — handle both — with a plain exit-code
    fallback if the output isn't parseable JSON."""
    code, out = await _run(
        [
            "docker",
            "compose",
            "-f",
            cfg.compose_file,
            "ps",
            "--format",
            "json",
            cfg.worker_service,
        ],
        cwd=cfg.project_dir,
    )
    rows = []
    parse_ok = True
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            parse_ok = False
            break
        # A line may be a single object (NDJSON) or an array of objects.
        rows.extend(parsed if isinstance(parsed, list) else [parsed])
    if parse_ok and rows:
        for row in rows:
            if not isinstance(row, dict):
                continue
            state = row.get("State") or row.get("state") or ""
            health = row.get("Health") or row.get("health") or ""
            if state == "running" and health != "unhealthy":
                return OpResult(
                    True, f"worker {cfg.worker_service} running", _tail(out)
                )
        return OpResult(False, f"worker {cfg.worker_service} not healthy", _tail(out))
    # Fallback: parse failed or produced no rows — trust exit code + non-empty output.
    ok = code == 0 and bool((out or "").strip())
    summary = (
        f"worker {cfg.worker_service} running"
        if ok
        else f"worker {cfg.worker_service} not healthy"
    )
    return OpResult(ok, summary, _tail(out))


async def head_sha(cfg: deployer_config.DeployerConfig) -> str:
    """Current HEAD short sha of the project repo, or "" on any error (non-fatal)."""
    code, out = await _run(
        ["git", "-C", cfg.project_dir, "rev-parse", "--short", "HEAD"]
    )
    if code != 0:
        return ""
    return out.strip()


async def op_rollback(cfg: deployer_config.DeployerConfig, to_sha: str) -> OpResult:
    """Roll the deployed code back to an earlier commit, then recreate the worker.
    Two network-free safety checks (real commit + ancestor of the deploy branch)
    ensure we only ever return to code that genuinely lived on this branch — never
    arbitrary or injected code. Detached checkout (non-destructive: the branch ref
    is never moved); the next forward `deploy` fast-forwards back up to the tip."""
    sha = str(to_sha or "").strip()
    if not sha:
        return OpResult(False, "rollback refused: no to_sha given")
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        return OpResult(False, f"rollback refused: not a valid sha: {sha!r}")
    d = cfg.project_dir
    code, _ = await _run(["git", "-C", d, "cat-file", "-e", f"{sha}^{{commit}}"])
    if code != 0:
        return OpResult(False, f"rollback refused: unknown commit {sha}")
    code, _ = await _run(
        ["git", "-C", d, "merge-base", "--is-ancestor", sha, cfg.branch]
    )
    if code != 0:
        return OpResult(
            False, f"rollback refused: {sha} is not an ancestor of {cfg.branch}"
        )
    code, out = await _run(["git", "-C", d, "checkout", "--quiet", "--detach", sha])
    if code != 0:
        return OpResult(False, f"rollback checkout failed (exit {code})", _tail(out))
    rec = await op_recreate_worker(cfg)
    return OpResult(rec.ok, f"rolled back to {sha[:12]}; {rec.summary}", rec.log_tail)
