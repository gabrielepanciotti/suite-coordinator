# cb_pack.py
"""Tiny shared callback-data codec (``pack_cb``/``parse_cb``), extracted out of
``bot.py`` so other pure modules (e.g. ``task_card.py``) can build Telegram
``callback_data`` strings without importing ``bot.py`` — which would create a
circular import once ``bot.py`` imports those modules back (``bot`` already
imports ``task_card`` for the M5 task-reminder card). Keep this module free of
Telegram/httpx/bot imports so it stays a leaf dependency.
"""

from __future__ import annotations


def pack_cb(*parts: str) -> str:
    """Encode callback_data as ``part:part:…`` (Telegram cap: 64 bytes)."""
    data = ":".join(parts)
    if len(data.encode()) > 64:
        raise ValueError(f"callback_data too long: {data!r}")
    return data


def parse_cb(data: str) -> list[str]:
    return data.split(":")
