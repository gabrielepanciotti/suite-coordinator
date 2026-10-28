# deployer_config.py
"""Deployer config, parsed from env. Pure logic — no I/O. Mirrors hub_config.py."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import hub_config
import local_defaults

DEFAULT_ENV_KEYS = (
    "HUB_LOCAL_AGENTS,HUB_TURN_CAP,HUB_RECOVERY_WINDOW_H,"
    "HUB_RECOVERY_MAX,CLAUDE_MAX_CONCURRENCY,CLAUDE_MODEL"
)
# Defense-in-depth: a key matching this is rejected even if whitelisted, so no
# secret/OAuth/api-key can ever be written to .env via the bus.
_SECRET_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)


@dataclass(frozen=True)
class DeployerConfig:
    hub_url: str
    tenant: str
    api_key: str
    agent_id: str
    allowed_senders: frozenset[str]
    env_keys: frozenset[str]
    compose_file: str
    project_dir: str
    worker_service: str
    env_file: str
    # This deployer identity's own bus key, picked out of the shared
    # HUB_AGENT_KEYS map by agent_id. "" = still on the tenant key (logged).
    agent_key: str = ""
    branch: str = "master"
    # Git remote to pull from. "origin" by default; a Windows box whose origin is
    # GitHub over HTTPS cannot authenticate without an interactive desktop
    # session, and there the remote is set per device (DEPLOYER_REMOTE)
    # to an SSH mirror.
    remote: str = "origin"

    @property
    def enabled(self) -> bool:
        return bool(
            self.hub_url
            and (self.api_key or self.agent_key)
            and self.agent_id
            and self.allowed_senders
        )

    def env_key_allowed(self, key: str) -> bool:
        return key in self.env_keys and not _SECRET_RE.search(key)


def _csv(raw: str | None, default: str = "") -> frozenset[str]:
    return frozenset(p.strip() for p in (raw or default).split(",") if p.strip())


def load_deployer_config() -> DeployerConfig:
    tenant = local_defaults.get("DEPLOYER_TENANT", "default")
    env_file = os.environ.get("DEPLOYER_ENV_FILE", ".env")
    if os.path.isabs(env_file):
        raise ValueError(f"DEPLOYER_ENV_FILE must be relative, got: {env_file!r}")
    agent_id = os.environ.get("DEPLOYER_AGENT_ID", "").strip()
    return DeployerConfig(
        hub_url=os.environ.get("HUB_URL", "").rstrip("/"),
        tenant=tenant,
        api_key=os.environ.get(hub_config.api_key_env(tenant), ""),
        agent_id=agent_id,
        agent_key=hub_config.load_agent_keys().get(agent_id, ""),
        allowed_senders=_csv(os.environ.get("DEPLOYER_ALLOWED_SENDERS")),
        env_keys=_csv(os.environ.get("DEPLOYER_ENV_KEYS"), DEFAULT_ENV_KEYS),
        compose_file=os.environ.get("DEPLOYER_COMPOSE_FILE", "docker-compose.prod.yml"),
        project_dir=os.environ.get("DEPLOYER_PROJECT_DIR", "."),
        worker_service=os.environ.get("DEPLOYER_WORKER_SERVICE", "coordinator-worker"),
        env_file=env_file,
        branch=os.environ.get("DEPLOYER_BRANCH", "master").strip() or "master",
        remote=os.environ.get("DEPLOYER_REMOTE", "origin").strip() or "origin",
    )
