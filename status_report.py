# status_report.py
"""Child -> parent status reporting for the Network overview.

Gathers, for each node this Coordinator hosts, a real status snapshot — git
ahead/behind/dirty of the node's workspace + the up/down state of its Docker
containers — and POSTs it to Network's ``POST /api/status-reports``. Network's
``GET /api/overview/network`` then joins each report onto its project by name, so
the overview stops being empty ("child -> parent a zero traffico", issue #25) and
reflects the actual state of the fleet.

Pure logic like overview_snapshot.py: no bot / Telegram imports. I/O is limited
to ``git`` / ``docker`` subprocesses (run off the event loop via to_thread) and
one ``httpx`` POST. Every path is fail-safe: a broken workspace or a missing
docker socket degrades to partial data, never an exception that breaks the loop.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess

import httpx

import hub_client
import projects

logger = logging.getLogger("coordinator.hub")

STATUS_REPORT_ENABLED_ENV = "STATUS_REPORT_ENABLED"
STATUS_REPORT_INTERVAL_S = 15 * 60  # 15 minutes, mirrors the overview snapshot


def _enabled() -> bool:
    return os.environ.get(STATUS_REPORT_ENABLED_ENV, "1").strip() not in (
        "0", "false", "no", "off",
    )


def device_name() -> str:
    """Stable identifier for this host. Network dedups reports per device, so it
    must NOT change across container recreation — prefer an explicit env over the
    container hostname (which is the ephemeral short container id)."""
    return os.environ.get("STATUS_DEVICE") or socket.gethostname()


def _run(args: list[str], cwd: str | None = None, timeout: int = 15) -> str:
    """Run a command, returning stdout stripped. '' on any failure (fail-safe)."""
    try:
        out = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _git_status(workspace: str) -> dict:
    """git ahead/behind vs upstream + dirty flag. Missing values stay None so the
    overview can distinguish 'unknown' from 0 (e.g. a detached HEAD / no upstream)."""
    result: dict = {"git_ahead": None, "git_behind": None, "dirty": None}
    if not os.path.isdir(os.path.join(workspace, ".git")):
        return result
    counts = _run(
        ["git", "rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd=workspace
    )
    # "<behind>\t<ahead>" when an upstream exists; empty on no upstream/detached.
    parts = counts.split()
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        result["git_behind"], result["git_ahead"] = int(parts[0]), int(parts[1])
    # dirty: any tracked/untracked change. porcelain is empty <=> clean.
    porcelain = _run(["git", "status", "--porcelain"], cwd=workspace)
    result["dirty"] = bool(porcelain)
    return result


def _docker_services(slug: str, all_containers: list[tuple[str, str]]) -> list[dict]:
    """Containers belonging to a node (name == slug or slug- prefix), with up/down.

    Matching is case-insensitive: project keys are camelCase (``homeResearcher``)
    while container names are lowercase (``homeresearcher-db``). ``all_containers`` is a
    prefetched list of (name, state) so we shell out to docker once per cycle,
    not once per node."""
    s = slug.lower()
    services: list[dict] = []
    for name, state in all_containers:
        ln = name.lower()
        if ln == s or ln.startswith(f"{s}-"):
            services.append({"name": name, "up": state == "running"})
    return services


def _all_containers() -> list[tuple[str, str]]:
    """(name, state) for every container (running or not). [] if docker is absent."""
    raw = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}"])
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if "\t" in line:
            name, state = line.split("\t", 1)
            out.append((name.strip(), state.strip()))
    return out


def build_status_report(project_keys: list[str]) -> dict:
    """Assemble the POST /api/status-reports payload for the given node keys.

    One entry per resolvable node: its Network project name (== the Coordinator
    project key == the Network agent's ``project``), git status of its workspace,
    and its Docker services. Unresolvable keys are skipped."""
    containers = _all_containers()
    seen: set[str] = set()
    out_projects: list[dict] = []
    for key in project_keys:
        if key in seen:
            continue
        seen.add(key)
        proj = projects.resolve_project(key)
        if proj is None:
            continue
        entry = {"name": proj.key, **_git_status(proj.workspace),
                 "services": _docker_services(proj.key, containers)}
        out_projects.append(entry)
    return {"device": device_name(), "projects": out_projects}


async def push_status_report(
    hub_url: str, api_key: str, agent_id: str, project_keys: list[str],
    agent_key: str = "",
) -> bool:
    """Build + POST the status report. Fail-safe: returns False (never raises) on
    a disabled flag, empty roster, or any HTTP/connection error."""
    if not _enabled():
        return True
    import asyncio

    payload = await asyncio.to_thread(build_status_report, project_keys)
    if not payload["projects"]:
        logger.debug("status report: no resolvable nodes to report")
        return True
    headers = hub_client.auth_headers(api_key, agent_id, agent_key)
    url = f"{hub_url.rstrip('/')}/api/status-reports"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("status report: HTTP %s from %s — will retry later",
                       exc.response.status_code, url)
        return False
    except Exception as exc:
        logger.warning("status report: push failed (%s) — will retry later", exc)
        return False
    logger.debug("status report pushed: device=%s nodes=%d",
                 payload["device"], len(payload["projects"]))
    return True
