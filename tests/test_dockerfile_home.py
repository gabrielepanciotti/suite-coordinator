"""The app user's home must stay writable all the way down.

`install -d -o app DIR` gives `app` only the *last* component of DIR: any
parent it has to create along the way is created by root. On 2026-10-08 the
image started pre-creating `/home/app/.local/share` that way (Life#95), so
`/home/app/.local` became root-owned. OpenCode writes its state to
`~/.local/state` at startup; with the parent read-only it died on
`EACCES: mkdir '/home/app/.local/state'` before answering — every Telegram
turn and every bus wake on every group, with no other symptom than "❌".

Two checks: every app-owned dir the Dockerfile creates under /home/app has
its parents created app-owned too, and the XDG dirs the CLIs write to exist.
"""
from __future__ import annotations

import pathlib
import re
import shlex

REPO = pathlib.Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "Dockerfile"
HOME = pathlib.PurePosixPath("/home/app")

# Where claude / opencode / tailscale-pp-cli keep config, data, state, cache.
XDG_DIRS = {".config", ".cache", ".local", ".local/share", ".local/state"}


def _app_owned_dirs() -> set[pathlib.PurePosixPath]:
    """Dirs created by `install -d ... -o app ...` in RUN lines."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    dirs: set[pathlib.PurePosixPath] = set()
    for cmd in re.findall(r"\binstall\s+-d\b[^&;|\n]*", text):
        tokens = shlex.split(cmd)
        if "-o" not in tokens or tokens[tokens.index("-o") + 1] != "app":
            continue
        dirs.update(pathlib.PurePosixPath(t) for t in tokens if t.startswith("/"))
    return dirs


def test_parents_of_app_owned_dirs_are_app_owned_too():
    dirs = _app_owned_dirs()
    orphans = sorted(
        f"{parent} (parent of {d})"
        for d in dirs
        if HOME in d.parents
        for parent in d.parents
        if HOME in parent.parents and parent not in dirs
    )
    assert not orphans, (
        "install -d creates these parents as root, not app: " + ", ".join(orphans)
    )


def test_xdg_dirs_the_clis_write_to_are_app_owned():
    missing = sorted(str(HOME / d) for d in XDG_DIRS if HOME / d not in _app_owned_dirs())
    assert not missing, "not created app-owned in the image: " + ", ".join(missing)


def test_opencode_store_dir_is_pre_created_app_owned():
    """The opencode_data volume is seeded from this dir: it must exist, app-owned."""
    assert pathlib.PurePosixPath("/home/app/.local/share/opencode") in _app_owned_dirs()
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "\\n" not in text, "a literal \\n slipped into a RUN line"
