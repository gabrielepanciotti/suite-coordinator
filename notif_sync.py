# notif_sync.py
"""Reconcile manifest-declared notifications into the live store + scheduler.

Each project's coordinator.toml [[notification]] entries are converted to
Notification objects and upserted (keyed by `key`) at startup, so editing the
toml and restarting reconciles the schedule. Manifest is the source of truth for
its own keys; notifications created via /notifiche (different keys) are untouched.
"""
from __future__ import annotations

import logging
import os

import manifest
import notif_store
import notifications
import profiles
import projects
import scheduler
from manifest import NotificationDecl
from notifications import Notification

logger = logging.getLogger("coordinator.notif_sync")


# Device-local destination override for manifest-declared notifications.
# A manifest can only say "dm" or "group"; both resolve DEVICE-SIDE — "group"
# needs a [telegram].group_chat_id in that project's coordinator.toml, "dm" needs
# mapped profile user ids. On this network most personal nodes have neither, so
# their notifications resolved to no target and vanished without a message: the
# subsystem looks configured and delivers nothing. Setting
# NOTIFICATION_DEST_OVERRIDE=chat:<id> in the (gitignored) .env routes every
# manifest notification whose declared target is dm/group to one real chat —
# a device decision, kept out of the nodes' versioned coordinator.toml, which
# must never carry a personal chat id. An explicit `to = "chat:<id>"` in a
# manifest is left alone: it already names its target.
DEST_OVERRIDE_ENV = "NOTIFICATION_DEST_OVERRIDE"
_OVERRIDABLE_DESTS = ("dm", "group")


def destination_for(d: NotificationDecl, env: dict | None = None) -> str:
    """The destination to store for a manifest decl, applying the device override."""
    override = ((os.environ if env is None else env).get(DEST_OVERRIDE_ENV) or "").strip()
    # A decl naming its recipients already says who must read it (e.g. the
    # owner's sentinel, which must never land in someone else's group).
    if d.recipients:
        return d.to
    if override and (d.to or "dm").strip().lower() in _OVERRIDABLE_DESTS:
        if (d.to or "").strip().lower() == "group" and _notifies_in_own_group(d):
            return d.to
        return override
    return d.to


def _notifies_in_own_group(d: NotificationDecl) -> bool:
    """True when the decl's project opted in (``notify_in_group``) and has a group."""
    proj = projects.resolve_project(d.project)
    tg = manifest.telegram_of(proj) if proj is not None else None
    if tg is None or not tg.notify_in_group:
        return False
    if tg.group_chat_id is None:
        logger.warning(
            "notification %s: notify_in_group set but project %r has no group_chat_id"
            " — using the device override", d.key, d.project,
        )
        return False
    return True


def _checked_recipients(d: NotificationDecl) -> tuple[str, ...] | None:
    """The decl's profile keys if every one maps to at least one Telegram id.

    None (with a warning) when any key is unknown or has no ids: the caller
    skips the notification rather than deliver it somewhere else.
    """
    known = {p.key for p in profiles.PROFILES}
    for key in d.recipients:
        if key not in known:
            logger.warning("notification %s: unknown recipient profile %r — skipped", d.key, key)
            return None
        if not profiles.user_ids_for(key):
            logger.warning(
                "notification %s: profile %r has no Telegram ids (PROFILE_%s_IDS) — skipped",
                d.key, key, key.upper(),
            )
            return None
    return tuple(d.recipients)


def decl_to_notification(d: NotificationDecl) -> Notification | None:
    """Manifest decl -> Notification; None when its ``recipients`` cannot resolve."""
    recipients: tuple[str, ...] = ()
    if d.recipients:
        # `recipients` names people to reach privately: paired with a group or
        # chat destination it would leak a personal notice into a shared chat.
        if (d.to or "dm").strip().lower() != "dm":
            logger.warning(
                "notification %s: recipients require to = \"dm\" (got %r) — skipped",
                d.key, d.to,
            )
            return None
        checked = _checked_recipients(d)
        if checked is None:
            return None
        recipients = checked
    params: dict = {}
    if d.provider == "agent":
        params["project"] = d.project
        params["prompt"] = d.prompt
        if d.command:  # run by the Coordinator, its stdout goes into the prompt
            params["command"] = d.command
            if d.timeout_sec is not None:
                params["timeout_sec"] = d.timeout_sec
    elif d.provider == "static":
        params["text"] = d.text
    elif d.provider == "command":
        params["project"] = d.project
        params["command"] = d.command
        if d.timeout_sec is not None:
            params["timeout_sec"] = d.timeout_sec
    elif d.provider == "report":
        # The producer's file, relative to the mounted workspaces; without it
        # the provider has nothing to read and stays silent (see _provider_report).
        params.update(d.params)
    if d.gate:
        params["gate"] = d.gate
    # Recipients are profile keys (notifications.resolve_targets maps them to
    # ids at send time). Without `recipients` in the toml they stay empty, as
    # before: group/chat are then the manifest's natural targets.
    return Notification(
        key=d.key,
        title=d.title,
        provider=d.provider,
        schedule=d.schedule,
        recipients=recipients,
        enabled=d.enabled,
        params=params,
        destination=destination_for(d),
    )


def sync_from_manifests() -> int:
    """Upsert+apply every manifest notification. Returns how many were synced."""
    n = 0
    for p in projects.list_projects():
        for decl in manifest.notifications_of(p):
            # Policy (2026-06-15): personal projects no longer emit notifications
            # via the Coordinator, EXCEPT allowlisted keys. A banned decl is never
            # upserted/scheduled, so e.g. mycorise's daily agent run + group send
            # never fire — while personalchef-* allowlisted keys are reconciled from the
            # toml (picking up prompt edits). Gate on p.kind (not is_banned) so the
            # decision uses the project being iterated, not a registry re-resolve.
            if p.kind == "personal" and not notifications.is_allowlisted(decl.key):
                continue
            notif = decl_to_notification(decl)
            if notif is None:
                continue
            # A manifest re-declares its own keys at EVERY boot, and would re-arm
            # a notification the person disabled from the web UI / /notifiche.
            # ``enabled`` is a runtime decision, not a manifest one: keep the
            # stored flag for an existing key, and only trust the manifest for a
            # brand-new notification. Everything else (schedule, prompt, params)
            # stays manifest-owned, so editing the toml still reconciles.
            existing = notif_store.get(notif.key)
            if existing is not None:
                notif.enabled = existing.enabled
            try:
                notif_store.upsert(notif)
                scheduler.apply(notif)
                n += 1
            except Exception:
                logger.exception("could not sync notification %s", decl.key)
    logger.info("synced %d manifest notifications", n)
    return n
