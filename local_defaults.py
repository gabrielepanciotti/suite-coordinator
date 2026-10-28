"""Installation defaults: the values that belong to whoever runs this Coordinator.

The engine never names a company, a host or an account. Where it needs one —
the tenant on the bus, the mailbox the mailer sends from, the box the sentinel
watches — it asks here, and the answer comes from ``locale/defaults.json``, a
flat ``{ENV_VAR: value}`` map that lives next to the code but outside it.

Precedence, highest first:

1. the environment (a device can always override);
2. the local file (the installation's defaults, versioned in ``locale/``);
3. the neutral fallback the caller passes (what a fresh install gets).

A missing or unreadable local file is not an error: a fresh install has none,
and every caller already works with its neutral fallback. A malformed one is
logged once, loudly, because a typo there would otherwise silently turn off
whatever depended on it.

Override the file path with ``COORDINATOR_LOCALE_FILE``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

LOCALE_FILE_ENV = "COORDINATOR_LOCALE_FILE"
_DEFAULT_PATH = Path(__file__).resolve().parent / "locale" / "defaults.json"


def locale_path() -> Path:
    return Path(os.environ.get(LOCALE_FILE_ENV) or _DEFAULT_PATH)


@lru_cache(maxsize=4)
def _load(path: str) -> Mapping[str, object]:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("local defaults %s unreadable (%s): using neutral fallbacks", p, exc)
        return {}
    if not isinstance(data, dict):
        logger.error("local defaults %s is not a JSON object: using neutral fallbacks", p)
        return {}
    # Keys starting with "_" are comments.
    return {k: v for k, v in data.items() if not k.startswith("_")}


def defaults() -> Mapping[str, object]:
    """The whole local map (empty on a fresh install)."""
    return _load(str(locale_path()))


def get(key: str, fallback: str = "", env: Mapping[str, str] | None = None) -> str:
    """Env var ``key`` if set and non-blank, else the local default, else ``fallback``."""
    env = os.environ if env is None else env
    value = (env.get(key) or "").strip()
    if value:
        return value
    local = defaults().get(key)
    if isinstance(local, str) and local.strip():
        return local.strip()
    return fallback


def get_list(key: str) -> tuple[str, ...]:
    """A list-valued local default (no env layer: lists are installation data)."""
    local = defaults().get(key)
    if isinstance(local, list):
        return tuple(str(x).strip() for x in local if str(x).strip())
    return ()


def clear_cache() -> None:
    """For tests that swap ``COORDINATOR_LOCALE_FILE``."""
    _load.cache_clear()


def locale_dir() -> Path:
    """The installation's ``locale/`` directory (the one holding ``defaults.json``)."""
    return locale_path().parent
