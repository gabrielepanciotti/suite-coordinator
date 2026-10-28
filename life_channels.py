"""The channels of an instance: one schema, read in one place.

Every operator reads the ClickUp workspace, the Drive folder, the Telegram group and the bus
tenant of an instance from ``instance.config.json`` -> ``channels`` (operator contract §4.4,
spec 2026-10-14). The schema is written down in ``docs/instance.config.schema.md``:

    "channels": {
      "clickup":  {"workspace": "90152063849"},
      "drive":    {"provider": "gdrive", "folder": "energy-passport"},
      "telegram": {"chatId": -100XXXXXXXXXX, "agent": "owner-telegram"},
      "bus":      {"tenant": "life-acme"}
    }

Only identities live here, never credentials or device-specific addresses (the bus URL, the
rclone remote name, the Network UI URL): the file is versioned and shared by every device.
The flat keys an early Coordinator wrote (``busTenant``, ``telegramChatId`` ...) are still
read, so no instance breaks while operators move to the nested form.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CHANNEL_KEYS = ("clickup", "drive", "telegram", "bus")
# Calendar is visible and configurable, but it is not required for every instance.
OPTIONAL_CHANNELS = ("calendar",)
ALL_CHANNELS = CHANNEL_KEYS + OPTIONAL_CHANNELS
DRIVE_PROVIDERS = ("gdrive", "sharepoint", "onedrive")
CALENDAR_PROVIDERS = ("google", "outlook")

# (channel, nested key, legacy flat keys)
_FIELDS = (
    ("clickup", "workspace", ("clickupWorkspace",)),
    ("drive", "provider", ()),
    ("drive", "folder", ("driveFolder",)),
    ("calendar", "provider", ()),
    ("calendar", "id", ()),
    ("telegram", "chatId", ("telegramChatId", "telegram_chat_id")),
    ("telegram", "agent", ("telegramAgent", "telegram_agent")),
    ("bus", "tenant", ("busTenant", "bus_tenant")),
)


@dataclass(frozen=True)
class Channels:
    """What an operator needs to reach an instance. Empty string / None = not configured."""

    instance_id: str
    clickup_workspace: str = ""
    drive_provider: str = ""
    drive_folder: str = ""
    calendar_provider: str = ""
    calendar_id: str = ""
    telegram_chat_id: int | None = None
    telegram_agent: str = ""
    bus_tenant: str = ""
    problems: list[str] = field(default_factory=list)

    def missing(self) -> list[str]:
        """Channels with nothing configured, in CHANNEL_KEYS order."""
        have = {
            "clickup": bool(self.clickup_workspace),
            "drive": bool(self.drive_folder),
            "telegram": self.telegram_chat_id is not None,
            "bus": bool(self.bus_tenant),
        }
        return [k for k in CHANNEL_KEYS if not have[k]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def read_channels(instance_id: str, config: dict[str, Any]) -> Channels:
    """Channels from an already loaded ``instance.config.json``. Never raises."""
    raw = config.get("channels")
    if not isinstance(raw, dict):
        return Channels(instance_id, problems=["blocco `channels` assente"])
    values: dict[tuple[str, str], str] = {}
    for channel, key, legacy in _FIELDS:
        block = raw.get(channel)
        value = _text(block.get(key)) if isinstance(block, dict) else ""
        for name in legacy:
            value = value or _text(raw.get(name))
        values[(channel, key)] = value

    problems: list[str] = []
    chat_raw = values[("telegram", "chatId")]
    chat: int | None = None
    if chat_raw:
        try:
            chat = int(chat_raw)
        except ValueError:
            problems.append(f"telegram.chatId non numerico: {chat_raw!r}")
    provider = values[("drive", "provider")]
    if provider and provider not in DRIVE_PROVIDERS:
        problems.append(f"drive.provider {provider!r} sconosciuto (validi: {', '.join(DRIVE_PROVIDERS)})")
    if values[("drive", "folder")] and not provider:
        problems.append("drive.folder senza drive.provider: non si sa su quale Drive cercarla")
    calendar_provider = values[("calendar", "provider")]
    if calendar_provider and calendar_provider not in CALENDAR_PROVIDERS:
        problems.append(
            f"calendar.provider {calendar_provider!r} sconosciuto (validi: {', '.join(CALENDAR_PROVIDERS)})"
        )
    if values[("calendar", "id")] and not calendar_provider:
        problems.append("calendar.id senza calendar.provider: non si sa su quale calendario cercarlo")

    return Channels(
        instance_id=instance_id,
        clickup_workspace=values[("clickup", "workspace")],
        drive_provider=provider,
        drive_folder=values[("drive", "folder")],
        calendar_provider=calendar_provider,
        calendar_id=values[("calendar", "id")],
        telegram_chat_id=chat,
        telegram_agent=values[("telegram", "agent")],
        bus_tenant=values[("bus", "tenant")],
        problems=problems,
    )


def channels_at(instance_id: str, path: Path) -> Channels:
    """Channels of the instance checked out at ``path``. A missing/broken file is a problem, not a crash."""
    cfg_path = Path(path) / "instance.config.json"
    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Channels(instance_id, problems=[f"{cfg_path} assente"])
    except ValueError as exc:
        return Channels(instance_id, problems=[f"{cfg_path} non e' JSON valido: {exc}"])
    if not isinstance(config, dict):
        return Channels(instance_id, problems=[f"{cfg_path} non e' un oggetto JSON"])
    return read_channels(instance_id, config)
