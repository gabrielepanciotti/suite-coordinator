#!/usr/bin/env python3
"""Every module a container imports must be mounted into it (#103).

The compose files mount the source files one by one. A module nobody mounted
is either missing (crash at import, `ModuleNotFoundError`) or, worse, served
from the image's older copy. The old check looked only at what bot.py imports
directly: `competenza_routing` is imported by projects.py, so it slipped
through and the minipc's bot crash-looped on 2026-10-27.

Here the check follows the imports transitively, from the entry point of
every service that mounts single .py files, in every compose file. Imports
inside functions count too: a lazy import that is not mounted crashes later,
on the first call. Services that mount the whole checkout (`.:/app`) or no
source file at all (they run the image) are skipped.

Standard library only: ricrea-bot.sh runs it with the host's python3 before
recreating, and refuses to recreate when a mount is missing.

Usage: check_mounts.py [compose files...]   (default: the repo's compose files)
Exit 1 and one line per missing mount when something is missing.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml")

_SERVICE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")
_COMMAND = re.compile(r"^\s+command:.*?\"python\",\s*\"([A-Za-z0-9_]+)\.py\"")
_FILE_MOUNT = re.compile(r"^\s+-\s+\./([A-Za-z0-9_]+)\.py:/app/\1\.py(:ro)?\s*$")
_CHECKOUT_MOUNT = re.compile(r"^\s+-\s+\.:/app(:ro)?\s*$")


def services(compose_text: str) -> dict[str, dict]:
    """``{service: {"entry": name|None, "mounted": set, "checkout": bool}}``."""
    out: dict[str, dict] = {}
    current = None
    in_services = False
    for line in compose_text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^\S", line) and not line.startswith("#"):
            in_services = False
        if not in_services:
            continue
        m = _SERVICE.match(line)
        if m:
            current = out.setdefault(m.group(1), {"entry": None, "mounted": set(), "checkout": False})
            continue
        if current is None:
            continue
        if m := _COMMAND.match(line):
            current["entry"] = m.group(1)
        elif m := _FILE_MOUNT.match(line):
            current["mounted"].add(m.group(1))
        elif _CHECKOUT_MOUNT.match(line):
            current["checkout"] = True
    return out


def local_imports(module: str, repo: pathlib.Path = REPO) -> set[str]:
    """Repo-root modules imported anywhere in ``module``.py (function bodies too)."""
    tree = ast.parse((repo / f"{module}.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return {n for n in names if (repo / f"{n}.py").is_file()}


def closure(entry: str, repo: pathlib.Path = REPO) -> set[str]:
    """``entry`` and every repo-root module reachable from it."""
    seen: set[str] = set()
    todo = [entry]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo.extend(local_imports(name, repo) - seen)
    return seen


def missing_mounts(compose: pathlib.Path, repo: pathlib.Path = REPO) -> dict[str, list[str]]:
    """``{service: [module, ...]}`` for the services that import an unmounted module."""
    out: dict[str, list[str]] = {}
    for name, svc in services(compose.read_text(encoding="utf-8")).items():
        if svc["checkout"] or not svc["mounted"] or not svc["entry"]:
            continue
        lacking = sorted(closure(svc["entry"], repo) - svc["mounted"])
        if lacking:
            out[name] = lacking
    return out


def main(argv: list[str]) -> int:
    files = [pathlib.Path(a) for a in argv] or [REPO / f for f in COMPOSE_FILES]
    bad = False
    for compose in files:
        for service, modules in missing_mounts(compose).items():
            bad = True
            for mod in modules:
                print(f"{compose.name}: {service} importa {mod}.py ma non lo monta "
                      f"(aggiungi `- ./{mod}.py:/app/{mod}.py:ro`)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
