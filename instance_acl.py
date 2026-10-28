# instance_acl.py
"""Who may reach which Life instance, and which chat belongs to which (#83, F0).

One bot serving every instance turns the work/personal split from a *physical* one
(two tokens, two processes, two ``.env``) into a *logical* one inside a process. A
routing bug could then land a personal message in a work instance workspace. This module
is the single gate that keeps it from happening (spec 2026-10-15 §5):

- a chat belongs to **one** instance. Two instances declaring the same ``chatId``
  is a configuration error, not a case to arbitrate: the chat is *contested* and is
  routed nowhere until someone separates it;
- a user reaches an instance only if their profile lists it (admins reach all).
  Default deny: no profile, no instance.

Pure logic over plain data; :func:`index_from_registry` is the only function that
reads disk, through ``instances`` (the bot's read-only view of Life's registry).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

logger = logging.getLogger("coordinator.instance_acl")


@dataclass(frozen=True)
class ChatIndex:
    """``chatId -> instance`` for the chats declared in ``channels.telegram``."""

    owner: dict[int, str] = field(default_factory=dict)
    contested: dict[int, tuple[str, ...]] = field(default_factory=dict)

    def instance_of(self, chat_id: int) -> str | None:
        """The instance owning ``chat_id``; ``None`` if undeclared or contested."""
        return self.owner.get(chat_id)

    def is_contested(self, chat_id: int) -> bool:
        return chat_id in self.contested


def build_chat_index(pairs: Iterable[tuple[str, int | None]]) -> ChatIndex:
    """Index ``(instance_id, chat_id)`` pairs; a chat claimed twice is contested."""
    claims: dict[int, list[str]] = {}
    for instance_id, chat_id in pairs:
        if chat_id is None:
            continue
        ids = claims.setdefault(int(chat_id), [])
        if instance_id not in ids:
            ids.append(instance_id)
    owner: dict[int, str] = {}
    contested: dict[int, tuple[str, ...]] = {}
    for chat_id, ids in claims.items():
        if len(ids) == 1:
            owner[chat_id] = ids[0]
        else:
            contested[chat_id] = tuple(sorted(ids))
    return ChatIndex(owner=owner, contested=contested)


def index_from_registry(env: Mapping[str, str] | None = None) -> ChatIndex:
    """The chat index of every instance registered on this device.

    An unreadable registry yields an empty index (nothing owned, nothing contested):
    the gate then falls back to the profile check alone, exactly today's behaviour.
    """
    import instances

    try:
        registered, _ = instances.load_registry(env)
    except instances.InstanceError as exc:
        logger.warning("instance registry unreadable, chat index empty: %s", exc)
        return ChatIndex()
    pairs = []
    for instance_id, inst in sorted(registered.items()):
        try:
            pairs.append((instance_id, instances.channels_of(inst, env=env).telegram_chat_id))
        except Exception:  # one broken config must not hide the others
            logger.exception("channels of instance %s unreadable", instance_id)
    return build_chat_index(pairs)


def conflict_for(chat_id: int, instance_id: str, index: ChatIndex) -> str | None:
    """Another instance already declaring ``chat_id``, if any (refuse the write)."""
    other = index.owner.get(chat_id)
    if other is not None and other != instance_id:
        return other
    for iid in index.contested.get(chat_id, ()):
        if iid != instance_id:
            return iid
    return None


def authorize(profile, instance_id: str | None) -> bool:
    """Whether ``profile`` may reach ``instance_id``. Default deny.

    ``instance_id`` ``None`` means "this chat belongs to no declared instance": the
    decision then stays with the per-project checks that exist today.
    """
    if profile is None:
        return False
    if instance_id is None or profile.is_admin:
        return True
    import profiles

    return instance_id in profiles.instances_of(profile)


def describe_contested(index: ChatIndex) -> list[str]:
    """One human line per contested chat, for the startup log and the admin notice."""
    return [
        f"chat {chat_id} dichiarata da {' e '.join(ids)}: non la instrado finche' non si separa"
        for chat_id, ids in sorted(index.contested.items())
    ]
