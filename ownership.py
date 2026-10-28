"""Dynamic Telegram polling ownership between peer devices.

Only getUpdates is exclusive on Telegram (one poller per token, the second
gets 409 Conflict); sending is always allowed from any device. Each instance
exposes a tiny HTTP status endpoint on the tailnet and probes its peer: the
alive instance with the highest priority that *wants* ownership polls, the
other stands by. Telegram's 409 is the ultimate arbiter, so a transient
double-poll is harmless (updates queue server-side for 24h).

Dependency-injection like scheduler.py/hub.py: never imports bot.py. The
acquire/release callables (wired in bot.py) start/stop PTB's updater and the
hub SSE listeners. Disabled (no-op) unless PEER_OWNERSHIP_URL is set, so
existing single-device deployments keep today's run_polling() path.
See docs/superpowers/specs/2026-06-10-telegram-dynamic-ownership-design.md.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from atomic_io import replace_with_retry

logger = logging.getLogger("coordinator.ownership")

# Shared secret between the ownership peers (Coordinator#88). The status GET
# stays open (peers probe it), the handoff POST needs it: whoever reaches the
# port must not be able to move the poller. Unset = handoff refused, never open.
TOKEN_HEADER = "X-Ownership-Token"


def ownership_token() -> str:
    return (os.environ.get("OWNERSHIP_TOKEN") or "").strip()


def handoff_authorized(got: str | None) -> tuple[int, str]:
    """``(200, "")`` when ``got`` is the shared secret, else the refusal."""
    expected = ownership_token()
    if not expected:
        return 503, "OWNERSHIP_TOKEN non configurato: handoff rifiutato"
    if not got or not hmac.compare_digest(got.encode(), expected.encode()):
        return 401, "token di ownership mancante o errato"
    return 200, ""


@dataclass(frozen=True)
class OwnershipConfig:
    device: str
    priority: int
    port: int
    peer_url: str
    probe_interval: float
    fail_threshold: int
    state_dir: str
    bind_host: str = "0.0.0.0"
    # "" is the historical, process-wide ownership. A named domain (a bot that
    # lives on its own ladder, e.g. "comunity") has its own priority, state file
    # and status path, so one device can be first for some bots and last for
    # others.
    domain: str = ""
    # Poller ladder from the instances (#83, F4). ``eligible=False``: some instance
    # served by the token does not admit this device as a poller — it must never poll,
    # whatever its priority or a /handoff says. ``ladder`` is shown in /ownership.
    eligible: bool = True
    reason: str = ""
    ladder: tuple[str, ...] = ()

    @property
    def lease_ttl(self) -> float:
        """How long a holder's claim lives without renewal: the takeover window."""
        return self.probe_interval * self.fail_threshold

    @property
    def peers(self) -> tuple[str, ...]:
        """Every peer to probe. ``peer_url`` may list several, comma-separated."""
        return _split_urls(self.peer_url)

    @property
    def enabled(self) -> bool:
        # An ineligible device still runs the ownership supervisor (standby, never
        # acquiring): "no peers" must not fall through to plain run_polling.
        return bool(self.peers) or not self.eligible

    @property
    def status_path(self) -> str:
        return "/ownership" + (f"/{self.domain}" if self.domain else "")

    @property
    def state_path(self) -> str:
        # Per-device file: data/ is Syncthing-shared between devices, so a
        # single shared filename would let one device clobber the other's flag.
        suffix = f"-{self.domain}" if self.domain else ""
        return os.path.join(self.state_dir, f"ownership-{self.device}{suffix}.json")


def _split_urls(raw: str) -> tuple[str, ...]:
    """Normalised, de-duplicated URLs from a comma/space separated string."""
    out: list[str] = []
    for chunk in re.split(r"[,\s]+", raw or ""):
        url = chunk.strip().rstrip("/")
        if url and url not in out:
            out.append(url)
    return tuple(out)


def _domain_env(name: str, domain: str) -> str | None:
    """``NAME_<DOMAIN>`` when set, else ``NAME``; None when neither is."""
    if domain:
        specific = os.environ.get(f"{name}_{domain.upper()}")
        if specific is not None and specific.strip() != "":
            return specific
    return os.environ.get(name)


def _instance_ladder() -> tuple[list[str] | None, list[str], bool]:
    """The poller ladder declared by the instances of this device (``pollers``).

    ``(ladder or None, problems, complete)``. ``complete`` is False when the ladder
    could not be computed from every registered instance: the registry is missing
    or empty, an instance checkout is not mounted here, or reading failed. A poller
    of the one bot must not trust a partial ladder (#83 F5: integra's poller did
    not see ``life-acme``, fell back to "no ladder" and fought dev-linux).
    """
    if (os.environ.get("OWNERSHIP_LADDER") or "auto").strip().lower() == "off":
        return None, [], True
    try:
        import instances
        import poller_ladder

        registered, _ = instances.load_registry()
        problems: list[str] = []
        complete = bool(registered)
        if not registered:
            problems.append(f"nessuna istanza registrata in {instances.instances_file()}")
        configs: dict[str, dict] = {}
        known: set[str] = set()
        for iid, inst in registered.items():
            if not (inst.path / instances.INSTANCE_CONFIG_NAME).is_file():
                complete = False
                problems.append(f"{iid}: {inst.path} non e' leggibile qui (checkout non montato?)")
                continue
            configs[iid] = instances.instance_config(inst)
            # The instance's device catalogue (``devices: {id: {name, tailnet}}``)
            # names devices that host no node of its graph — the minipc, a VPS — but
            # may still poll the one bot: they are known too (#83, conv 720).
            catalogue = configs[iid].get("devices")
            if isinstance(catalogue, dict):
                known.update(k for k in catalogue if isinstance(k, str))
            try:
                graph = json.loads((inst.path / "life.graph.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            nodes = graph.get("nodes") if isinstance(graph, dict) else None
            for node in (nodes.values() if isinstance(nodes, dict) else []):
                if isinstance(node, dict) and isinstance(node.get("paths"), dict):
                    known.update(node["paths"])
        found, more = poller_ladder.poller_ladder(configs, known)
        return found, problems + more, complete
    except Exception as exc:
        logger.warning("poller ladder unavailable, priorities from .env: %s", exc)
        return None, [f"lettura del registro fallita: {exc}"], False


def _ladder_required() -> bool:
    """This process polls the one bot's token, so it may poll only by the ladder.

    The one bot (#83) reads its token from the device secret
    (``TELEGRAM_BOT_TOKEN_FILE``); a legacy bot with its own token in ``.env``
    keeps the old .env priorities.
    """
    role = (os.environ.get("COORDINATOR_ROLE") or "bot").strip().lower()
    return role == "bot" and bool((os.environ.get("TELEGRAM_BOT_TOKEN_FILE") or "").strip())


def load_config(domain: str = "") -> OwnershipConfig:
    state_dir = (
        os.path.dirname(os.environ.get("STATE_PATH", "/app/data/state.json"))
        or "/app/data"
    )
    peers = _split_urls(_domain_env("PEER_OWNERSHIP_URL", domain) or "")
    # One source for the device name, shared with the poller ladder (#83 F5):
    # DEVICE_NAME wins, else LIFE_DEVICE, else "device". With two different
    # names /handoff <device> and the fencing with peers missed this device.
    device = (
        (os.environ.get("DEVICE_NAME") or "").strip()
        or (os.environ.get("LIFE_DEVICE") or "").strip()
        or "device"
    )
    priority = int(_domain_env("OWNER_PRIORITY", domain) or "0")
    eligible, reason, ladder = True, "", ()
    if not domain:
        import poller_ladder

        found, problems, complete = _instance_ladder()
        for problem in problems:
            logger.warning("poller ladder: %s", problem)
        if _ladder_required() and (found is None or not complete):
            # Fail-safe: the one bot's token without a trustworthy ladder never
            # polls, whatever OWNER_PRIORITY or PEER_OWNERSHIP_URL say.
            eligible, priority = False, -1
            reason = "scala dei poller non calcolabile: " + (
                "; ".join(problems) or "nessuna istanza dichiara pollers")
            logger.error("poller ladder: %s -> this device will NOT poll", reason)
        elif found is not None:
            me = device
            ladder = tuple(found)
            prio = poller_ladder.priority_for(me, found)
            if prio is None:
                eligible, priority = False, -1
                reason = f"{me} non e' fra i poller ammessi da tutte le istanze ({' > '.join(found) or '-'})"
            else:
                priority = prio
            if not peers:
                peers = tuple(poller_ladder.peers_of(me, found, os.environ))
            logger.info("poller ladder: %s | this device %s: %s",
                        " > ".join(found) or "-", me,
                        f"priority {priority}" if eligible else "not eligible")
    return OwnershipConfig(
        device=device,
        priority=priority,
        port=int(os.environ.get("OWNERSHIP_PORT", "8013")),
        peer_url=",".join(peers),
        # Clamp: a 0 threshold would defeat hysteresis, a 0 interval would spin.
        probe_interval=max(
            1.0, float(os.environ.get("OWNERSHIP_PROBE_INTERVAL", "15"))
        ),
        fail_threshold=max(1, int(os.environ.get("OWNERSHIP_FAIL_THRESHOLD", "3"))),
        state_dir=state_dir,
        bind_host=os.environ.get("OWNERSHIP_BIND_HOST", "0.0.0.0"),
        domain=domain,
        eligible=eligible,
        reason=reason,
        ladder=ladder,
    )


class OwnershipManager:
    """STANDBY <-> OWNER state machine, driven by tick() every probe interval.

    - STANDBY -> OWNER: peer dead for fail_threshold consecutive probes
      (hysteresis), or peer alive but weaker/yielding — and I want ownership.
    - OWNER -> STANDBY: a stronger claiming peer appears (immediate, no
      hysteresis going up), my wants flag is flipped off, or Telegram keeps
      answering 409 (the peer is polling and I should not fight it).
    """

    def __init__(
        self,
        config: OwnershipConfig,
        *,
        acquire: Callable[[], Awaitable[None]],
        release: Callable[[], Awaitable[None]],
        probe: Callable[[], Awaitable[dict | list | None]] | None = None,
    ) -> None:
        self.cfg = config
        self._acquire = acquire
        self._release = release
        self._probe = probe or self._probe_peers
        self.is_polling = False
        self.wants_ownership = self._load_wants()
        # Lease (#83, F4): the claim this device publishes while it polls. ``epoch``
        # grows at every change of hand and survives restarts, so a holder that
        # wakes from a suspension with an old epoch learns it is a zombie.
        self.epoch = self._load_epoch()
        self.lease_until = 0.0
        self._clock = time.time
        # Per-peer bookkeeping, grown on demand (a probe may script any number
        # of peers): consecutive failed probes, and the last priority seen.
        self._fails: list[int] = []
        self._last_priority: list[int | None] = []
        self._conflicts = 0
        self._client: httpx.AsyncClient | None = None
        # One tick at a time: a 409 triggers an immediate tick (#83 F5, D3) that
        # may overlap the supervisor's periodic one.
        self._tick_lock = asyncio.Lock()
        self._last_conflict_tick = 0.0

    # --- status & persistence -------------------------------------------------

    def status(self) -> dict:
        out = {
            "device": self.cfg.device,
            "priority": self.cfg.priority,
            "wants_ownership": self.wants_ownership and self.cfg.eligible,
            "is_polling": self.is_polling,
            "lease": {
                "holder": self.cfg.device if self.is_polling else None,
                "epoch": self.epoch,
                "until": self.lease_until if self.is_polling else 0.0,
            },
        }
        if self.cfg.ladder:
            out["ladder"] = list(self.cfg.ladder)
        if not self.cfg.eligible:
            out["eligible"] = False
            out["reason"] = self.cfg.reason
        return out

    def _load_epoch(self) -> int:
        try:
            with open(self.cfg.state_path, encoding="utf-8") as fh:
                return int(json.load(fh).get("epoch", 0))
        except (OSError, ValueError, TypeError):
            return 0

    def _load_wants(self) -> bool:
        try:
            with open(self.cfg.state_path, encoding="utf-8") as fh:
                return bool(json.load(fh).get("wants_ownership", True))
        except (OSError, ValueError):
            return True

    def _save_wants(self) -> None:
        try:
            os.makedirs(self.cfg.state_dir, exist_ok=True)
            tmp = self.cfg.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"wants_ownership": self.wants_ownership,
                           "epoch": getattr(self, "epoch", 0)}, fh)
            replace_with_retry(tmp, self.cfg.state_path)
        except OSError:
            logger.exception("ownership state save failed")

    async def set_wants(self, value: bool) -> None:
        self.wants_ownership = value
        self._save_wants()
        if not value and self.is_polling:
            await self._do_release()

    async def apply_handoff(self, target_device: str) -> dict:
        """/handoff semantics: I want ownership iff I am the target."""
        await self.set_wants(target_device == self.cfg.device)
        return self.status()

    # --- transitions ------------------------------------------------------------

    async def on_conflict(self) -> None:
        """Telegram 409 while polling: persistent -> the peer is polling, yield.

        The supervisor re-acquires later if the peer disappears (probes fail).
        """
        self._conflicts += 1
        # A 409 means someone else is polling right now: look at the peers at once
        # instead of waiting for the next probe, so a holder that sees a newer
        # epoch (or a stronger claimant) yields at the first conflict (#83 F5, D3:
        # integra used to cede ~30 s after dev-linux came back). Rate-limited:
        # PTB retries a 409 every few seconds.
        now = self._clock()
        if self.is_polling and now - self._last_conflict_tick >= 2.0:
            self._last_conflict_tick = now
            await self.tick()
        if self.is_polling and self._conflicts >= self.cfg.fail_threshold:
            logger.warning("persistent 409 Conflict: peer is polling, yielding")
            await self._do_release()

    async def tick(self) -> None:
        async with self._tick_lock:
            await self._tick()

    async def _tick(self) -> None:
        raw = await self._probe()
        peers: list[dict | None] = list(raw) if isinstance(raw, list) else [raw]
        while len(self._fails) < len(peers):
            self._fails.append(0)
            self._last_priority.append(None)
        for i, peer in enumerate(peers):
            self._fails[i] = self._fails[i] + 1 if peer is None else 0
            if peer is not None:
                self._last_priority[i] = int(peer.get("priority", 0))
        if not self.cfg.eligible:
            # Not admitted by some instance: never poll, not even after a handoff.
            if self.is_polling:
                await self._do_release()
            return
        now = self._clock()
        # The highest epoch ever seen, not just now: a holder that just went
        # silent still counts, or the next taker would reuse its epoch.
        self._seen_epoch = max(
            [self.epoch, getattr(self, "_seen_epoch", 0)]
            + [int((p.get("lease") or {}).get("epoch") or 0) for p in peers if p]
        )
        if self.is_polling and self._superseded(peers, now):
            logger.warning("lease superseded by a newer epoch: I am a stale holder, yielding")
            await self._do_release()
            return
        if self.is_polling:
            if self._seen_epoch > self.epoch:
                # Back from a partition during which someone else took (and gave
                # back) the lease: re-claim with a fresh epoch, never an old one.
                self.epoch = self._seen_epoch + 1
                self._save_wants()
            self.lease_until = now + self.cfg.lease_ttl  # renewal
        mine = self.cfg.priority
        peer_claims = any(
            p and p.get("wants_ownership") and int(p.get("priority", 0)) > mine
            for p in peers
        )
        # Hysteresis: a peer that just stopped answering may be a blip, so the
        # takeover waits fail_threshold probes. Only peers that could outrank me
        # matter — one never seen (unknown) counts, a known weaker one does not.
        unsure = any(
            p is None
            and self._fails[i] < self.cfg.fail_threshold
            and self._could_outrank(i)
            for i, p in enumerate(peers)
        )
        if self.is_polling:
            if peer_claims or not self.wants_ownership:
                await self._do_release()
            return
        if not self.wants_ownership or peer_claims:
            return
        if unsure:
            return
        await self._do_acquire()

    def _superseded(self, peers: list, now: float) -> bool:
        """A live peer holds a lease with a newer epoch than mine."""
        for p in peers:
            lease = (p or {}).get("lease") or {}
            if (
                p and p.get("is_polling")
                and lease.get("holder") not in (None, self.cfg.device)
                and int(lease.get("epoch") or 0) > self.epoch
                and float(lease.get("until") or 0) > now
            ):
                return True
        return False

    def _could_outrank(self, i: int) -> bool:
        seen = self._last_priority[i]
        return seen is None or seen > self.cfg.priority

    async def _do_acquire(self) -> None:
        self.epoch = max(self.epoch, getattr(self, "_seen_epoch", 0)) + 1
        logger.info("acquiring Telegram polling (device=%s, epoch %d)", self.cfg.device, self.epoch)
        self._conflicts = 0
        await self._acquire()
        self.is_polling = True
        self.lease_until = self._clock() + self.cfg.lease_ttl
        self._save_wants()

    async def _do_release(self) -> None:
        logger.info("releasing Telegram polling (device=%s)", self.cfg.device)
        await self._release()
        self.is_polling = False
        self._conflicts = 0  # next acquisition must start with a clean slate

    # --- I/O ---------------------------------------------------------------------

    async def _probe_one(self, url: str) -> dict | None:
        assert self._client is not None
        try:
            r = await self._client.get(url + self.cfg.status_path)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    async def _probe_peers(self) -> list[dict | None]:
        """One answer per configured peer, in order; None where it is silent."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=4)
        return list(await asyncio.gather(*(self._probe_one(u) for u in self.cfg.peers)))

    async def aclose(self) -> None:
        """Release the probe HTTP client (call on shutdown)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def run(self) -> None:
        """Supervisor loop. First tick immediately (a strong PC boots fast)."""
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("ownership tick failed")
            await asyncio.sleep(self.cfg.probe_interval)


_REASONS = {200: "OK", 401: "Unauthorized", 404: "Not Found", 503: "Service Unavailable"}


async def serve_status(
    manager: OwnershipManager,
    port: int | None = None,
    domains: dict[str, OwnershipManager] | None = None,
):
    """Minimal stdlib HTTP server: GET /ownership, POST /ownership/handoff.

    The port is published on the tailnet, where a non-peer device (a VPS
    exposed to internet) can reach it: GET is open (state only, peers probe
    it), POST handoff wants ``X-Ownership-Token`` (Coordinator#88). Bodies are
    tiny JSON. Inside Docker the container
    port is only reachable as published by compose, so the bind host
    (cfg.bind_host, OWNERSHIP_BIND_HOST) matters mainly for non-container runs.
    Returns the asyncio.Server (caller closes it on shutdown).

    ``domains`` adds one status/handoff pair per named domain:
    GET /ownership/<domain>, POST /ownership/<domain>/handoff.
    """
    routes = {"": manager, **(domains or {})}

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            head = raw_head[:-4]
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            method, path, *_ = request_line.split(" ")
            length = 0
            token = None
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = min(
                        int(value.strip() or 0), 65536
                    )  # cap: bodies are tiny JSON
                elif name.strip().lower() == TOKEN_HEADER.lower().encode():
                    token = value.strip().decode("latin-1")
            body = (
                await asyncio.wait_for(reader.readexactly(length), timeout=5)
                if length
                else b""
            )
            parts = path.strip("/").split("/")
            target = None
            is_handoff = False
            if parts[0] == "ownership":
                if parts[-1] == "handoff":
                    is_handoff = True
                    parts = parts[:-1]
                target = routes.get(parts[1] if len(parts) > 1 else "")
                if len(parts) > 2:
                    target = None
            if target is not None and method == "GET" and not is_handoff:
                payload, code = target.status(), 200
            elif target is not None and method == "POST" and is_handoff:
                code, refusal = handoff_authorized(token)
                if code != 200:
                    logger.warning("handoff refused (%d): %s", code, refusal)
                    payload = {"error": refusal}
                else:
                    data = json.loads(body or b"{}")
                    payload = await target.apply_handoff(
                        str(data.get("target_device", ""))
                    )
            else:
                payload, code = {"error": "not found"}, 404
            out = json.dumps(payload).encode()
            writer.write(
                (
                    f"HTTP/1.1 {code} {_REASONS.get(code, 'Error')}\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(out)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                + out
            )
            await writer.drain()
        except Exception:
            logger.exception("ownership http handler failed")
        finally:
            writer.close()

    bind_port = manager.cfg.port if port is None else port
    server = await asyncio.start_server(_handle, manager.cfg.bind_host, bind_port)
    logger.info("ownership status endpoint on :%d", bind_port)
    return server


async def forward_handoff(cfg: OwnershipConfig, target_device: str) -> dict | None:
    """Push the handoff to every peer (best-effort).

    Returns the first peer's answer, or None if none is reachable — the peers
    converge anyway at their next probe.
    """
    async def _one(client: httpx.AsyncClient, url: str) -> dict | None:
        try:
            r = await client.post(
                url + cfg.status_path + "/handoff",
                json={"target_device": target_device},
                headers={TOKEN_HEADER: ownership_token()},
            )
            if r.status_code in (401, 503):
                logger.warning("handoff refused by %s (%d): set the same "
                               "OWNERSHIP_TOKEN on every peer", url, r.status_code)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    if not cfg.peers:
        return None
    async with httpx.AsyncClient(timeout=4) as client:
        answers = await asyncio.gather(*(_one(client, u) for u in cfg.peers))
    return next((a for a in answers if a), None)
