"""Hub integration config, parsed from env. No bot import.

The one I/O is the local agent list, computed from Life by ``bus_agents``
when HUB_LOCAL_AGENTS is unset or ``auto`` (#81).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HubConfig:
    hub_url: str
    api_keys: dict[str, str]  # tenant -> api_key (legacy shared tenant key)
    telegram_agent: str  # agent name Coordinator acts as (human)
    telegram_chat_id: int  # forum supergroup where topics live (0 = unset)
    local_agents: dict[str, str]  # Network agent name -> Coordinator project key
    # agent name -> that agent's own bus key (sent as X-Agent-Key).
    #
    # ONE map in ONE env var (HUB_AGENT_KEYS="agent:key,agent:key"), not one
    # HUB_AGENT_KEY_<AGENT> variable per agent, because bus agent names are not
    # safely expressible as env var names: they carry dashes and camelCase
    # (`personalChef-minipc`, `homeResearcher-minipc`), and the usual
    # upper+underscore mangling makes `homeResearcher-minipc` and
    # `homeresearcher-minipc` — two distinct, coexisting agents on this bus —
    # collide on the same variable. The map keeps the agent name byte-exact,
    # which is what the bus compares against, and reuses the `a:b,c:d` shape
    # HUB_LOCAL_AGENTS already uses. Agent keys are urlsafe tokens
    # ([A-Za-z0-9_-]), so neither `,` nor `:` can appear inside a value and the
    # parse stays unambiguous.
    agent_keys: dict[str, str] = field(default_factory=dict)
    turn_cap: int = 12
    role: str = "hub"  # "hub" (Telegram face) | "worker" (headless executor)
    prod_dispatch: dict[str, str] = field(
        default_factory=dict
    )  # project_key -> prod agent
    prod_device: str = "minipc"  # default device that hosts the prod identity
    dev_dispatch: dict[str, str] = field(
        default_factory=dict
    )  # project_key -> dev agent
    dev_device: str = ""  # device that hosts the dev identity ("" = bare key, legacy)
    agent_tenants: dict[str, str] = field(
        default_factory=dict
    )  # agent name -> tenant it belongs to ("" / absent = every tenant)
    # Where local_agents came from: "grafo" | "override" | "vuoto" | "non disponibile".
    local_agents_source: str = ""
    recovery_window_h: int = 24  # boot recovery: re-wake convs updated within N hours
    recovery_max: int = 20  # boot recovery: max convs re-woken per agent
    pipeline_gate_timeout_h: int = 24  # auto-abort a gate left undecided this long
    claude_max_concurrency: int = 3   # max simultaneous local-agent wakes (process-wide)
    use_a2a: bool = False  # route outbound sends through the A2A protocol (vs custom REST)
    # A2A push webhook (inbound). When a2a_webhook_url is set, this node receives
    # bus events via Network push notifications instead of SSE: it registers an
    # agent-level push config per listened agent and SKIPS the SSE listeners for
    # them (mutual exclusion — never both, to avoid double delivery).
    a2a_webhook_url: str = ""  # base URL Network POSTs to, e.g. http://host:8018
    a2a_webhook_token: str = ""  # shared secret echoed as X-A2A-Notification-Token
    # Sidecar deployer bus identities (bridge pipeline deploy/verify/rollback to a
    # deterministic sidecar instead of a Claude `<key>-prod` agent). Empty =
    # not configured -> callers fall back to prod_agent().
    #
    # A LIST because the Coordinator's own worker runs on every prod server
    # (one for internal products, one for public ones) and they must not
    # drift apart. Products stay single-target: each is deployed by its own
    # `<key>-prod` agent, which already lives on the right server.
    deployer_agents: tuple[str, ...] = ()
    # Base URL of the Network web UI (e.g. http://100.101.15.47:3336). Every
    # Telegram post about a bus conversation links <base>/conversations/<id>
    # (#66). Empty = no link: per-instance, never hardcoded.
    network_ui_url: str = ""
    # Old tenant name -> canonical one (HUB_TENANT_ALIASES="old:new,..."). Records
    # persisted before a tenant rename (hub_topics.json `tenant`) keep the old
    # name; without the alias a Telegram reply on such a conversation looked up
    # api_keys[<old>] and raised KeyError (conv 672: life-work -> life-acme).
    tenant_aliases: dict[str, str] = field(default_factory=dict)

    def canonical_tenant(self, tenant: str) -> str:
        """The configured tenant a (possibly old) tenant name stands for."""
        if tenant in self.api_keys:
            return tenant
        alias = self.tenant_aliases.get(tenant, "")
        return alias if alias in self.api_keys else tenant

    @property
    def deployer_agent(self) -> str:
        """First deploy target, or "" — kept for callers that want just one."""
        return self.deployer_agents[0] if self.deployer_agents else ""

    @property
    def a2a_webhook_enabled(self) -> bool:
        return bool(self.a2a_webhook_url)

    def agent_key(self, agent: str, tenant: str | None = None) -> str:
        """This agent's own bus key, or "" when it still rides the tenant key.

        An identity that exists in several tenants under one name (the telegram
        agent, #83) has one key per tenant: ``HUB_AGENT_KEYS`` then carries
        ``<tenant>/<agent>:<key>``, which wins over the bare ``<agent>:<key>``.
        """
        if tenant:
            scoped = self.agent_keys.get(f"{self.canonical_tenant(tenant)}/{agent}")
            if scoped:
                return scoped
        return self.agent_keys.get(agent, "")

    @property
    def enabled(self) -> bool:
        if not (self.hub_url and self.api_keys):
            return False
        if self.role == "worker":
            return bool(self.local_agents)
        return bool(self.telegram_chat_id)

    def _override(self, table: dict[str, str], names: tuple[str, ...]) -> str:
        """The first explicit override matching any of a project's names.

        The tables are written by hand in `.env` and keyed by whatever name the
        project had then (`segnalatori:`). Since the live registry renamed the
        keys to the graph ids (`portaleSegnalazioni`), an exact lookup on the
        key silently stopped matching: the override was there and nobody read
        it (#79). Any spelling of the project — slug, key, alias — finds it.
        """
        for name in names:
            if name and name in table:
                return table[name]
        lowered = {k.lower(): v for k, v in table.items()}
        for name in names:
            if name and name.lower() in lowered:
                return lowered[name.lower()]
        return ""

    def _heard_here(self, names: tuple[str, ...], device: str) -> str:
        """The identity this machine really listens on for the project, or "".

        HUB_LOCAL_AGENTS maps each bus identity to the project it serves, so it
        already says what the bus calls a project on this device — including
        the identities Network#42 exempts from `<slug>-<device>`
        (`acme-auth-dev-linux` for the `acmeAuth` project). Deriving the
        name instead would address an agent that does not exist (#79).
        """
        if not device:
            return ""
        wanted = {n.lower() for n in names if n}
        suffix = f"-{device}"
        for agent, project in self.local_agents.items():
            if agent.endswith(suffix) and project.lower() in wanted:
                return agent
        return ""

    def prod_agents(
        self, project_key: str, aliases: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        """Tutte le identità prod di un progetto, in ordine di dispatch.

        Quasi ogni prodotto ne ha una sola: vive su un server e basta. Ma alcuni
        girano su ENTRAMBI i prod — Network ha un'istanza privata sul box
        interno (rete locale) e una pubblica su quello pubblico — e allora vanno raggiunti tutti,
        altrimenti se ne aggiorna uno e l'altro resta indietro in silenzio.
        Si dichiarano separandoli con `|` in HUB_PROD_DISPATCH:

            HUB_PROD_DISPATCH=network:network-prod-private|network-prod-public

        `project_key` è lo slug del nodo (`Project.bus_slug`): il nome sul bus
        è `<slug>-<device>` (Network#42), e la chiave storica del progetto
        (`offerte`) vi sopravvive solo come alias da ritirare. `aliases` sono
        gli altri nomi del progetto, usati solo per trovare un override.
        """
        explicit = self._override(self.prod_dispatch, (project_key, *aliases))
        if explicit:
            return tuple(a for a in (s.strip() for s in explicit.split("|")) if a)
        return (f"{project_key}-{self.prod_device}",)

    def prod_agent(self, project_key: str, aliases: tuple[str, ...] = ()) -> str:
        """Prima identità prod — per i chiamanti che ne vogliono una sola."""
        return self.prod_agents(project_key, aliases)[0]

    def dev_agent(self, project_key: str, aliases: tuple[str, ...] = ()) -> str:
        """Dev bus identity for a project: explicit override, else the identity
        this machine listens on for it, else `<slug>-<dev_device>` — symmetric
        to prod_agent(). `project_key` is the node slug (`Project.bus_slug`).

        Until 2026-07-31 this returned the bare key, because the dev device was
        the primary one and its agents carried no suffix. Since the suffix rule
        (`-dev-linux` / `-dev`, see the installation's DEVICES.md under docs/) no bare-key agent
        exists on the bus any more: a bare name addresses nobody, and a pipeline
        built on it stalls at its first stage without an error. `dev_device`
        stays empty by default so a device that really has unsuffixed agents
        keeps working unchanged.
        """
        names = (project_key, *aliases)
        explicit = self._override(self.dev_dispatch, names)
        if explicit:
            return explicit
        heard = self._heard_here(names, self.dev_device)
        if heard:
            return heard
        return f"{project_key}-{self.dev_device}" if self.dev_device else project_key

    def orphan_tenant_agents(self) -> list[tuple[str, str]]:
        """Local agents qualified with a tenant this node does not listen on.

        `agents_for()` drops them without a word: on 2026-10-15 the tenants
        became Life instances (`life-work`) while BOT_LOCAL_AGENTS still said
        `acme/...`, and the bot booted with 0 listeners for 70 minutes. Returns
        `(tenant, agent)` pairs, sorted, so the boot can say it out loud.
        """
        return sorted(
            (t, a)
            for a, t in self.agent_tenants.items()
            if t and a in self.local_agents and t not in self.api_keys
        )

    def agents_for(self, tenant: str) -> list[str]:
        """Local agents to listen on for a tenant (tenant-qualified or unqualified)."""
        return [
            a
            for a in self.local_agents
            if self.agent_tenants.get(a, "") in ("", tenant)
        ]


# The Network keeps the old names as permanent aliases of the same tenant.
DEFAULT_TENANT_ALIASES = "life-work:life-acme"


def _parse_pairs(raw: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        k, v = part.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def load_agent_keys() -> dict[str, str]:
    """Parse HUB_AGENT_KEYS ("agent:key,agent:key") into {agent: key}.

    Standalone so the sidecars (deployer, hub_watch) read the same map without
    building a full HubConfig.
    """
    return _parse_pairs(os.environ.get("HUB_AGENT_KEYS"))


def api_key_env(tenant: str) -> str:
    """Env var holding a tenant's shared key: HUB_API_KEY_<TENANT>.

    Tenant names are Life instance names ('life-work'), so anything that is not
    a valid env-var character becomes '_': HUB_API_KEY_LIFE_WORK.
    """
    return "HUB_API_KEY_" + re.sub(r"[^A-Z0-9]", "_", tenant.upper())


def instance_tenants(env=None) -> list[str]:
    """The bus tenant of every instance registered on this device (#82).

    ``channels.bus.tenant`` of each ``instance.config.json``, read through the
    single channel reader. A connected instance (``instances connect <id> bus``)
    is heard without anyone editing ``HUB_TENANTS``. Never raises: a broken
    registry leaves the ``.env`` tenants alone, it does not switch the bus off.
    """
    import instances

    try:
        registry, _warnings = instances.load_registry(env)
    except instances.InstanceError as exc:
        import logging

        logging.getLogger("coordinator.hub_config").warning(
            "registro istanze illeggibile, tenant solo dal .env: %s", exc
        )
        return []
    out: list[str] = []
    for inst in registry.values():
        tenant = instances.channels_of(inst, env=env).bus_tenant
        if tenant and tenant not in out:
            out.append(tenant)
    return out


def tenant_key(tenant: str, env=None) -> str:
    """A tenant's shared key: ``HUB_API_KEY_<TENANT>``, else the device secret.

    ``instances connect <id> bus`` writes the key to
    ``$LIFE_HOME/secrets/<tenant>.key`` (0600, per device): the .env stays the
    override, the secret file is the default.
    """
    env = os.environ if env is None else env
    key = (env.get(api_key_env(tenant)) or "").strip()
    if key:
        return key
    import instances

    secret = instances.life_home(env) / "secrets" / f"{tenant}.key"
    try:
        return secret.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_tenants(env=None) -> list[str]:
    """``HUB_TENANTS`` first (order kept: the first is the default tenant), then
    every registered instance's tenant not already listed."""
    env = os.environ if env is None else env
    tenants = [t.strip() for t in env.get("HUB_TENANTS", "").split(",") if t.strip()]
    return tenants + [t for t in instance_tenants(env) if t not in tenants]


def load_config() -> HubConfig:
    tenants = load_tenants()
    declared = {
        t.strip() for t in os.environ.get("HUB_TENANTS", "").split(",") if t.strip()
    }
    agent_keys = load_agent_keys()
    api_keys = {}
    for t in tenants:
        key = tenant_key(t)
        if not key and t not in declared:
            # An instance tenant with no key on this device (created from another
            # one): listening would only collect 401s. Say it, do not guess.
            import logging

            logging.getLogger("coordinator.hub_config").warning(
                "tenant %s dell'istanza senza chiave su questo device "
                "(%s o $LIFE_HOME/secrets/%s.key): non ascoltato",
                t, api_key_env(t), t,
            )
            continue
        # A tenant stays configured when it has no shared key but we do hold
        # per-agent keys: that is exactly the post-revocation steady state, and
        # dropping the tenant here would make `enabled` False and switch the
        # whole hub integration off silently.
        if key or agent_keys:
            api_keys[t] = key
    chat_raw = os.environ.get("HUB_TELEGRAM_CHAT_ID", "0").strip()
    try:
        chat_id = int(chat_raw)
    except ValueError:
        chat_id = 0
    # HUB_LOCAL_AGENTS entries may be tenant-qualified: "tenant/agent:project".
    # Unqualified entries ("agent:project") listen on every tenant (legacy).
    # Unset or "auto" = computed from the Life graph; empty = none; a list =
    # explicit override, warned about when it diverges from the graph (#81).
    import bus_agents

    spec, _source = bus_agents.local_agents_spec(os.environ.get("HUB_LOCAL_AGENTS"))
    local_agents: dict[str, str] = {}
    agent_tenants: dict[str, str] = {}
    for key, project in _parse_pairs(spec).items():
        if "/" in key:
            tenant, agent = (s.strip() for s in key.split("/", 1))
            agent_tenants[agent] = tenant
            local_agents[agent] = project
        else:
            local_agents[key] = project
    return HubConfig(
        hub_url=os.environ.get("HUB_URL", "").rstrip("/"),
        api_keys=api_keys,
        telegram_agent=os.environ.get("HUB_TELEGRAM_AGENT", "telegram-owner"),
        telegram_chat_id=chat_id,
        local_agents=local_agents,
        agent_keys=agent_keys,
        turn_cap=int(os.environ.get("HUB_TURN_CAP", "12")),
        role=os.environ.get("COORDINATOR_ROLE", "hub").strip().lower() or "hub",
        prod_dispatch=_parse_pairs(os.environ.get("HUB_PROD_DISPATCH")),
        prod_device=os.environ.get("HUB_PROD_DEVICE", "minipc").strip() or "minipc",
        dev_dispatch=_parse_pairs(os.environ.get("HUB_DEV_DISPATCH")),
        dev_device=os.environ.get("HUB_DEV_DEVICE", "").strip(),
        agent_tenants=agent_tenants,
        local_agents_source=_source,
        recovery_window_h=int(os.environ.get("HUB_RECOVERY_WINDOW_H", "24")),
        recovery_max=int(os.environ.get("HUB_RECOVERY_MAX", "20")),
        pipeline_gate_timeout_h=int(os.environ.get("PIPELINE_GATE_TIMEOUT_H", "24")),
        claude_max_concurrency=int(os.environ.get("CLAUDE_MAX_CONCURRENCY", "3")),
        use_a2a=os.environ.get("HUB_USE_A2A", "0").strip().lower() in ("1", "true", "yes"),
        a2a_webhook_url=os.environ.get("HUB_A2A_WEBHOOK_URL", "").rstrip("/"),
        a2a_webhook_token=os.environ.get("HUB_A2A_WEBHOOK_TOKEN", ""),
        # CSV: un target per server prod. Un solo nome resta valido e produce la
        # pipeline di prima, stage per stage identica.
        network_ui_url=os.environ.get("NETWORK_UI_URL", "").strip().rstrip("/"),
        tenant_aliases=_parse_pairs(
            os.environ.get("HUB_TENANT_ALIASES", DEFAULT_TENANT_ALIASES)
        ),
        deployer_agents=tuple(
            a for a in (
                s.strip() for s in os.environ.get("PIPELINE_DEPLOYER_AGENT", "").split(",")
            ) if a
        ),
    )
