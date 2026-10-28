# poller_ladder.py
"""Which devices may poll the one bot token, in which order (#83, F4).

The poller reads the text of every message of every instance, so a device may poll
only if **every** instance served by the token admits it. An instance restricts its
pollers by declaring ``"pollers": [<device id>, …]`` in ``instance.config.json``;
an instance that declares nothing restricts nothing. Where an instance runs is its
own ``executors`` ladder, never a rule here.
The ladder is the first restricting instance's list, filtered by all the others: the
intersection, in an order someone actually wrote down.

Device ids are identities, like the rest of ``instance.config.json``: they must be
keys of some instance graph's ``paths`` (``life.graph.json``) or of some instance's
``devices`` catalogue (devices hosting no node). An unknown id is
dropped with a problem line, never guessed. Addresses (the tailnet URL of each
device's ``/ownership``) are per-device and stay in the ``.env``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


def poller_ladder(
    configs: Mapping[str, Mapping],
    known_devices: Iterable[str] = (),
) -> tuple[list[str] | None, list[str]]:
    """``(ladder or None, problems)``. None = no instance restricts: today's priorities."""
    known = {d.lower(): d for d in known_devices}
    problems: list[str] = []
    restricting: list[tuple[str, list[str]]] = []
    for iid in sorted(configs):
        raw = configs[iid].get("pollers")
        if raw is None:
            continue
        if not isinstance(raw, list):
            problems.append(f"{iid}: pollers non e' una lista, ignorato")
            continue
        devices = []
        for d in raw:
            if not isinstance(d, str) or not d.strip():
                continue
            if known and d.lower() not in known:
                problems.append(f"{iid}: device {d!r} non e' in nessun grafo, saltato")
                continue
            devices.append(known.get(d.lower(), d))
        restricting.append((iid, devices))
    if not restricting:
        return None, problems
    first = restricting[0][1]
    allowed = [{d.lower() for d in devs} for _, devs in restricting[1:]]
    ladder = [d for d in first if all(d.lower() in a for a in allowed)]
    if not ladder:
        problems.append("nessun device ammesso da tutte le istanze: nessuno puo' fare da poller")
    return ladder, problems


def priority_for(device: str, ladder: list[str]) -> int | None:
    """Higher = earlier in the ladder; None = not admitted (must never poll)."""
    low = [d.lower() for d in ladder]
    if device.lower() not in low:
        return None
    return 10 * (len(ladder) - low.index(device.lower()))


def peer_env(device: str) -> str:
    """Env var with a device's ``/ownership`` base URL: ``OWNERSHIP_PEER_<DEVICE>``."""
    return "OWNERSHIP_PEER_" + re.sub(r"[^A-Z0-9]", "_", device.upper())


def peers_of(device: str, ladder: list[str], env: Mapping[str, str]) -> list[str]:
    """The URLs of the other ladder devices that this device's ``.env`` knows."""
    out = []
    for d in ladder:
        if d.lower() == device.lower():
            continue
        url = (env.get(peer_env(d)) or "").strip().rstrip("/")
        if url:
            out.append(url)
    return out
