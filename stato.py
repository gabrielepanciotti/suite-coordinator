"""``/stato``: cosa sta degradando adesso nella rete (spec Life degradazione nominata, P4).

Chiede a Life ``GET /api/health/network`` e lo rende in testo. Tre esiti per nodo,
non due: ``unreachable`` pesa piu' di ``degraded`` e non diventa mai «tutto ok».
Se Life stesso non risponde lo si dice, invece di tacere: e' la degradazione
che questo comando esiste per mostrare.

Modulo foglia: niente import del progetto.
"""
from __future__ import annotations

import html
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("coordinator")

_ICON = {"ok": "✅", "degraded": "⚠️", "error": "❌", "unreachable": "🔌"}
_MAX_DETAIL_NODES = 25


def base_url() -> str:
    return os.environ.get("LIFE_BACKEND_URL", "http://localhost:9999").rstrip("/")


def fetch(url: str | None = None, *, timeout: float = 10.0) -> dict:
    """Legge la salute di rete. Solleva `RuntimeError` con il motivo se non riesce."""
    base = url or base_url()
    if base.lower() in {"off", "none", "disabled"}:
        raise RuntimeError("registro Life spento (LIFE_BACKEND_URL=off)")
    try:
        with urllib.request.urlopen(f"{base}/api/health/network", timeout=timeout) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("risposta non interpretabile")
    return data


def _degradation_line(d: object) -> str:
    if not isinstance(d, dict):
        return html.escape(str(d))
    parts = [str(d.get("component") or "?")]
    if d.get("subject"):
        parts[0] += f"/{d['subject']}"
    if d.get("reason"):
        parts.append(str(d["reason"]))
    if d.get("fallback"):
        parts.append(f"ripiego: {d['fallback']}")
    if d.get("detail"):
        parts.append(str(d["detail"]))
    return html.escape(" · ".join(parts))


def render(data: dict) -> str:
    """Testo HTML per Telegram."""
    status = data.get("status", "ok")
    summary = data.get("summary") or {}
    head = f"{_ICON.get(status, '❔')} <b>Rete: {html.escape(str(status))}</b>"
    counts = " · ".join(
        f"{_ICON[k]} {summary[k]}" for k in ("unreachable", "error", "degraded", "ok") if summary.get(k)
    )
    lines = [head + (f"  ({counts})" if counts else "")]
    if data.get("detail"):
        lines.append(html.escape(str(data["detail"])))
    nodes = [n for n in data.get("nodes") or [] if n.get("status") != "ok"]
    unreachable = [n for n in nodes if n.get("status") == "unreachable"]
    others = [n for n in nodes if n.get("status") != "unreachable"]
    for n in others[:_MAX_DETAIL_NODES]:
        lines.append(f"{_ICON.get(n.get('status'), '❔')} <b>{html.escape(str(n.get('id')))}</b>"
                     + (f" ({html.escape(str(n['detail']))})" if n.get("detail") else ""))
        for d in n.get("degradations") or []:
            lines.append(f"   • {_degradation_line(d)}")
    if len(others) > _MAX_DETAIL_NODES:
        lines.append(f"… e altri {len(others) - _MAX_DETAIL_NODES} nodi")
    if unreachable:
        ids = ", ".join(html.escape(str(n.get("id"))) for n in unreachable)
        lines.append(f"🔌 <b>Non rispondono ({len(unreachable)}):</b> {ids}")
    if not nodes and status == "ok":
        lines.append("Nessuna degradazione: ogni nodo ha risposto.")
    return "\n".join(lines)


def render_failure(reason: str) -> str:
    """Life non risponde: e' una degradazione, non un «niente da segnalare»."""
    return (f"🔌 <b>Stato di rete non disponibile</b>: Life non risponde "
            f"({html.escape(reason)}). Non significa che sia tutto ok.")
