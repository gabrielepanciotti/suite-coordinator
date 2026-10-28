"""Receipt photo -> structured food lines, read by the Telegram bots' own runner.

Another Life node (PersonalChef) POSTs a receipt photo to the notification API;
this module drives ``claude_runner.run_claude`` (subscription session, concurrency
gate, lean boot -- no API key) to read it and parses the JSON answer.

Kept free of FastAPI: ``api.py`` owns the route and maps :class:`VisionError`
to an HTTP status. ``run_claude`` is injected so tests never spawn a model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

import claude_runner

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
TIMEOUT_S = 120
_UNITS = {"g", "ml", "pz"}

PERSONA = (
    "You are a pure data extractor for supermarket receipts. You never chat, "
    "never explain, never ask questions. Your whole reply is one JSON object."
)

TASK_PROMPT = (
    "Use the Read tool to open the receipt image at this path: {path}\n\n"
    'Answer ONLY with JSON of the form {{"lines":[{{"raw_text":str,"name":str,'
    '"quantity":number|null,"unit":"g"|"ml"|"pz"|null}}]}}.\n'
    "Rules:\n"
    "- One entry per FOOD product line of the receipt, in receipt order.\n"
    "- raw_text = the line as printed, without the price.\n"
    "- name = the abbreviation expanded to a plain Italian product name.\n"
    "- quantity+unit ONLY when printed on the line (pack size like 400G, 1L, x6), "
    "normalised to g/ml/pz (kg->g, l/cl/dl->ml); otherwise both null. "
    "Never invent quantities.\n"
    "- Skip totals, IVA, discounts, payment lines and non-food items.\n"
    "No prose, no markdown, JSON only."
)


class VisionError(Exception):
    """A failure with the HTTP status and the Italian message to return."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def workspace_root() -> Path:
    """Dedicated scratch workspace (never a project workspace)."""
    root = Path(os.getenv("VISION_WORKSPACE") or Path(tempfile.gettempdir()) / "coordinator-vision")
    root.mkdir(parents=True, exist_ok=True)
    return root


def parse_lines(text: str) -> list[dict]:
    """First JSON object in ``text`` (fences/prose tolerated) -> clean lines."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    raw_lines = obj.get("lines") if isinstance(obj, dict) else None
    if not isinstance(raw_lines, list):
        raise ValueError("missing lines")
    out = []
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        qty = item.get("quantity")
        unit = item.get("unit")
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or unit not in _UNITS:
            qty, unit = None, None
        out.append({
            "raw_text": str(item.get("raw_text") or ""),
            "name": str(item.get("name") or item.get("raw_text") or ""),
            "quantity": float(qty) if qty is not None else None,
            "unit": unit,
        })
    return out


def _suffix(content_type: str) -> str:
    sub = content_type.split("/", 1)[-1].split(";")[0].strip().lower()
    return "." + (sub if sub.isalnum() else "jpg")


async def read_receipt(image: bytes, content_type: str, *, run_claude=None) -> dict:
    """Run the model on ``image``; return ``{"lines": [...]}`` or raise VisionError."""
    run = run_claude or claude_runner.run_claude
    tmp = tempfile.mkdtemp(prefix="receipt-", dir=workspace_root())
    try:
        path = Path(tmp) / f"receipt{_suffix(content_type)}"
        path.write_bytes(image)
        try:
            result = await asyncio.wait_for(
                run(TASK_PROMPT.format(path=path), workspace=tmp, persona=PERSONA),
                timeout=TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            raise VisionError(504, "Lettura dello scontrino scaduta: riprova.")
        except Exception:
            logger.exception("vision: runner crashed")
            raise VisionError(503, "Servizio di lettura non disponibile.")
        text = result[0]
        if claude_runner.is_auth_error(text) or claude_runner.is_transport_error(text):
            logger.warning("vision: runner failure: %s", text.replace("\n", " ")[:160])
            raise VisionError(503, "Servizio di lettura non disponibile.")
        try:
            return {"lines": parse_lines(text)}
        except (ValueError, json.JSONDecodeError):
            logger.warning("vision: unparseable output: %s", text.replace("\n", " ")[:200])
            raise VisionError(502, "Risposta del modello non interpretabile.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
