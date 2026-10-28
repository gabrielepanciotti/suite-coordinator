# profiles.py
"""Per-user profiles for the Telegram session dashboard (M3).

A *Profile* decides **which projects a Telegram user can see and reach**. The
profile catalog lives in code (below); the ``user_id -> profile`` mapping lives
in the environment (secret, per-machine) via ``PROFILE_<KEY>_IDS`` variables.

Default deny: a user with no mapped profile sees and reaches nothing.

Pure logic — no Telegram imports — so it unit-tests like ``projects.py``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import projects
from projects import Project

logger = logging.getLogger("coordinator.profiles")


@dataclass(frozen=True)
class Profile:
    key: str                     # "owner" | "member"
    label: str
    is_admin: bool               # admin => sees everything, `projects` ignored
    projects: tuple[str, ...]    # visible project keys (ignored when is_admin)
    task_card_target: str = "coordinator_topic"  # where M5 task.due cards land
    # Life instances this profile may reach (#83). Ignored when is_admin.
    # Overridable per machine with PROFILE_<KEY>_INSTANCES (see instances_of()).
    instances: tuple[str, ...] = ()


# Allowed values for Profile.task_card_target (M5 task card routing).
CARD_TARGETS = ("coordinator_topic", "dm", "project_group")


# Profile catalog (static, versioned). Order matters: when a user id is listed
# in more than one PROFILE_*_IDS, the first profile here wins.
PROFILES: tuple[Profile, ...] = (
    Profile("owner", "Owner", is_admin=True, projects=(),
            task_card_target="coordinator_topic"),
    Profile("member", "a member", is_admin=False,
            projects=("personalChef", "mycorise", "travel", "experienceFinder",
                      "stayFinder", "transportFinder", "scheduler",
                      "academicAssistant"),
            task_card_target="dm",
            # a member is a participant of personal, not an instance of their own (#83).
            instances=("personal",)),
)


def _parse_ids(raw: str | None, *, source: str) -> list[int]:
    """Parse a comma-separated id list, dropping non-integer tokens with a warning.

    Mirrors the lenient parsing used for ALLOWED_USER_IDS in bot.py.
    """
    ids: list[int] = []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            ids.append(int(tok))
        except ValueError:
            logger.warning("ignoring non-integer id %r in %s", tok, source)
    return ids


def _build_user_map() -> dict[int, Profile]:
    """Build ``{user_id: profile}`` from the env (lazy; called per lookup).

    For each profile, read ``PROFILE_<KEY.upper()>_IDS``. If NO ``PROFILE_*_IDS``
    is set at all, fall back to treating ``ALLOWED_USER_IDS`` (or its legacy
    alias ``ALLOWED_CHAT_IDS``) as admins (= profile ``owner``), so existing
    single-user setups keep working unchanged. A user id mapped to several
    profiles is assigned the first one in PROFILES order (with a warning).
    """
    mapping: dict[int, Profile] = {}
    any_profile_env = False
    for profile in PROFILES:
        env_name = f"PROFILE_{profile.key.upper()}_IDS"
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        any_profile_env = True
        for uid in _parse_ids(raw, source=env_name):
            existing = mapping.get(uid)
            if existing is not None:
                logger.warning(
                    "user id %s in multiple profiles; keeping %r over %r",
                    uid, existing.key, profile.key,
                )
                continue
            mapping[uid] = profile

    if not any_profile_env:
        admin = PROFILES[0]
        legacy = os.environ.get("ALLOWED_USER_IDS") or os.environ.get(
            "ALLOWED_CHAT_IDS"
        )
        for uid in _parse_ids(legacy, source="ALLOWED_USER_IDS (fallback)"):
            mapping[uid] = admin
    return mapping


def profile_for(user_id: int) -> Profile | None:
    """Return the profile of ``user_id``, or ``None`` if not mapped (default deny)."""
    return _build_user_map().get(user_id)


def card_target_for(user_id: int) -> str:
    """Return the M5 task-card target mode for ``user_id`` (safe default ``dm``).

    Unmapped user (default deny) or a profile with an unrecognised target both
    resolve to ``"dm"`` — the safest place to land a personal task card.
    """
    profile = profile_for(user_id)
    if profile is None:
        return "dm"
    if profile.task_card_target not in CARD_TARGETS:
        logger.warning(
            "profile %r has invalid task_card_target %r; falling back to 'dm'",
            profile.key, profile.task_card_target,
        )
        return "dm"
    return profile.task_card_target


def visible_projects(profile: Profile) -> list[Project]:
    """Projects the profile may see.

    Admin -> the full registry; otherwise the registry filtered to
    ``profile.projects``, preserving the registry order.
    """
    if profile.is_admin:
        return projects.list_projects()
    allowed = set(profile.projects)
    return [p for p in projects.PROJECTS if p.key in allowed]


def can_access(profile: Profile, project_key: str) -> bool:
    """Whether ``profile`` may access the project identified by ``project_key``."""
    return profile.is_admin or project_key in profile.projects


def user_ids_for(profile_key: str) -> list[int]:
    """User ids mapped to ``profile_key`` (empty if none — default deny)."""
    return [uid for uid, p in _build_user_map().items() if p.key == profile_key]


def admin_user_ids() -> list[int]:
    """User ids whose profile is admin (used for ops/error notifications)."""
    return [uid for uid, p in _build_user_map().items() if p.is_admin]


def assistant_name_of(profile: Profile) -> str | None:
    """The name this person chose for their assistant (#89), or ``None``.

    Per machine, like the user ids: ``PROFILE_<KEY>_ASSISTANT_NAME``. People who
    come through Life carry it in their access answer instead.
    """
    raw = os.environ.get(f"PROFILE_{profile.key.upper()}_ASSISTANT_NAME", "")
    name = " ".join(raw.split())[:40]
    return name or None


def instances_of(profile: Profile) -> tuple[str, ...]:
    """The instances ``profile`` may reach: ``PROFILE_<KEY>_INSTANCES`` or the catalog.

    The env can only *narrow* what the catalog grants, never widen it: a typo or a
    copied ``.env`` must not open work instance to a personal participant.
    """
    raw = os.environ.get(f"PROFILE_{profile.key.upper()}_INSTANCES")
    if raw is None:
        return profile.instances
    wanted = tuple(s.strip() for s in raw.split(",") if s.strip())
    if profile.is_admin:
        return wanted
    extra = [i for i in wanted if i not in profile.instances]
    if extra:
        logger.warning("PROFILE_%s_INSTANCES lists %s outside the catalog; ignored",
                       profile.key.upper(), ", ".join(extra))
    return tuple(i for i in wanted if i in profile.instances)


def dm_project_for(profile: Profile) -> str | None:
    """Default project for this profile's unbound DMs (``PROFILE_<KEY>_DM_PROJECT``).

    None when unset, or when the profile may not access that project — an env
    typo must not open a project the profile cannot see.
    """
    raw = (os.environ.get(f"PROFILE_{profile.key.upper()}_DM_PROJECT") or "").strip()
    if not raw:
        return None
    if not can_access(profile, raw):
        logger.warning("PROFILE_%s_DM_PROJECT=%r not accessible to the profile; ignored",
                       profile.key.upper(), raw)
        return None
    return raw
