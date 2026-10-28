# api.py
"""FastAPI HTTP layer for managing notifications from a web frontend.

Thin wrapper over the existing logic: notif_store (persistence), notifications
(model/validation), scheduler (live APScheduler), providers (content). Never
imports bot.py — run_claude/send_target are reached via scheduler._S, which the
bot injects at boot. Same source of truth as the Telegram /notifiche UI.

Auth (Coordinator#88): the port is reachable from tailnet devices that are not
ours to trust (a VPS exposed to internet), so every route wants
``COORDINATOR_API_TOKEN`` (``X-Api-Token`` or ``Authorization: Bearer``) except
the public node card (``/api/health``, ``/api/overview``) and the routes that
carry their own secret (cards, A2A webhook, vision). Deny by default: a new
route is protected without anyone remembering to.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import a2a_webhook
import instances
import manifest
import notif_store
import overview
import notifications
import profiles
import projects
import providers
import life_registry_client
import remote_cards
import reparto_routing
import scheduler
import vision_receipt
from notifications import Notification

logger = logging.getLogger("coordinator.api")

# Routes open without COORDINATOR_API_TOKEN: the public node card, and the
# ones that check a secret of their own (X-Card-Token, A2A token, VISION_TOKEN).
OPEN_PATHS = ("/api/health", "/api/overview", "/api/cards", "/api/vision/receipt")
OPEN_PREFIXES = ("/a2a/webhook/",)


def api_token() -> str:
    return (os.environ.get("COORDINATOR_API_TOKEN") or "").strip()


def _presented_token(request: Request) -> str:
    token = request.headers.get("x-api-token") or ""
    if not token:
        scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
    return token


def require_api_token(request: Request) -> None:
    """Fail closed: no token configured on the device = every protected route 503."""
    path = request.url.path
    if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
        return
    expected = api_token()
    if not expected:
        raise HTTPException(503, detail="COORDINATOR_API_TOKEN non configurato: API chiusa")
    got = _presented_token(request)
    if not got or not hmac.compare_digest(got.encode(), expected.encode()):
        raise HTTPException(401, detail="token API mancante o errato")


# No /docs, /redoc, /openapi.json: they would list every route to anyone, and
# they are added outside the dependency below.
app = FastAPI(
    title="Coordinator Notifications",
    dependencies=[Depends(require_api_token)],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # the token, not the origin, is the gate (#88)
    allow_methods=["*"],
    allow_headers=["*"],
)
# A2A push-notification receiver (inbound webhook from Network). Inert unless
# HUB_A2A_WEBHOOK_URL is configured.
app.include_router(a2a_webhook.router)
# Scheda-nodo live (GET /api/overview) + liveness (GET /api/health) — public,
# read-only, no secrets (Life node convention).
app.include_router(overview.router)


@app.exception_handler(HTTPException)
async def _http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    body = detail if isinstance(detail, dict) else {"detail": detail}
    return JSONResponse(status_code=exc.status_code, content=body)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class NotificationIn(BaseModel):
    key: str | None = None
    title: str
    provider: str
    schedule: str
    recipients: list[str] = Field(default_factory=list)
    enabled: bool = True
    params: dict = Field(default_factory=dict)
    destination: str = "dm"
    # Life instance that owns the record (contract §4.3): required on write.
    instance: str | None = None


class NotificationPatch(BaseModel):
    title: str | None = None
    provider: str | None = None
    schedule: str | None = None
    recipients: list[str] | None = None
    enabled: bool | None = None
    params: dict | None = None
    destination: str | None = None
    # Required on write too (contract §4.3).
    instance: str | None = None


# ---------------------------------------------------------------------------
# Instance selector (operator contract, Coordinator#78 / Life §4)
# ---------------------------------------------------------------------------

def _require_instance(
    value: str | None,
    *,
    current: Notification | None = None,
) -> str | None:
    """The write target's instance id.

    On a **registered** device (§4.3) an ``instance`` that IS declared must be
    one of the registered ids: an unknown one is a 422 that NAMES the valid ids,
    never a silent fallback (R-1 in Life). A write that OMITS it is **not**
    rejected: the callers that predate multi-instance are still around — the
    manifest seed, the Telegram wizard, the Health integration, and the ComUnity
    announce scripts that PATCH ``{"enabled": true}`` / ``{"params": {...}}``
    with no instance (checked 2026-10-14) — and a hard 422 there would break
    them silently. An omitted instance lands on the row's own instance (an
    update keeps its ownership) or on the device default from ``state.json`` (a
    create), each with a warning. On a device with **no registry** (the legacy
    single instance, R-3) the field stays optional and defaults to that
    instance's id: the pre-multi-instance deployment keeps working bit-for-bit.
    """
    raw = (value or "").strip()
    registered = instances.known_ids()
    if registered:
        if raw:
            if raw not in registered:
                raise HTTPException(422, detail={
                    "detail": f"istanza non registrata: {raw}",
                    "valid_instances": registered,
                })
            return raw
        # Omitted: never a 422 for a field that legacy writers never sent. An
        # update keeps the row's instance (resetting it to the default would
        # silently relabel e.g. a comunity notification); a create takes the
        # device default.
        if current is not None and current.instance_id:
            logger.warning(
                "scrittura senza instance: mantengo %r (notifica %r)",
                current.instance_id, current.key,
            )
            return current.instance_id
        fallback = instances.default_instance_id()
        if not fallback:
            raise HTTPException(422, detail={
                "detail": "instance obbligatorio in scrittura (nessun default sul device)",
                "valid_instances": registered,
            })
        logger.warning(
            "scrittura senza instance: uso il default del device %r", fallback
        )
        return fallback
    return raw or instances.legacy_instance_id()


def _valid_read_instance(value: str | None) -> str | None:
    """Validate a ``?instance=`` filter; None means 'every instance'."""
    raw = (value or "").strip()
    if not raw:
        return None
    registered = instances.known_ids()
    if registered and raw not in registered:
        raise HTTPException(422, detail={
            "detail": f"istanza non registrata: {raw}",
            "valid_instances": registered,
        })
    return raw


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _next_run(key: str) -> str | None:
    """ISO next fire time from the live scheduler, or None."""
    return scheduler.next_run(key)


def _manifest_keys() -> set[str]:
    """Keys declared in project manifests (origin == 'manifest')."""
    out: set[str] = set()
    for p in projects.list_projects():
        try:
            for decl in manifest.notifications_of(p):
                out.add(decl.key)
        except Exception as exc:
            logger.warning("manifest parse error for project %s: %s", p.key, exc)
            continue
    return out


def _serialize(notif: Notification, *, runs: dict, manifest_keys: set[str],
               outcomes: dict | None = None) -> dict:
    d = notif.to_dict()
    d["last_run"] = runs.get(notif.key)
    rec = (outcomes or {}).get(notif.key) or {}
    d["last_outcome"] = rec.get("last_outcome")
    d["last_category"] = rec.get("last_category")
    d["next_run"] = _next_run(notif.key)
    d["origin"] = "manifest" if notif.key in manifest_keys else "web"
    d["instance"] = notif.instance_id
    # Resolve to the canonical Life node (maps legacy names via project aliases)
    # so the web UI groups by node with a clean label instead of the raw legacy slug.
    raw = notifications.project_of(notif)
    proj = projects.resolve_project(raw)
    d["project"] = proj.key if proj else raw
    d["project_label"] = proj.label if proj else raw
    return d


def _to_notification(payload: NotificationIn, *, key: str) -> Notification:
    return Notification(
        key=key, title=payload.title, provider=payload.provider,
        schedule=payload.schedule, recipients=tuple(payload.recipients),
        enabled=payload.enabled, params=dict(payload.params),
        destination=payload.destination,
    )


def _slugify(title: str) -> str:
    base = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")
    return base or "notif"


# ---------------------------------------------------------------------------
# Endpoints — Task 1: GET /api/notifications
# ---------------------------------------------------------------------------

@app.get("/api/notifications")
def list_notifications(instance: str | None = None) -> dict:
    """Scheduled notifications. ``?instance=<id>`` filters; absent = all.

    Read contract §4.3. The response also names the resolved instance set so the
    frontend can render the 'tutte' view with a per-item badge.
    """
    wanted = _valid_read_instance(instance)
    runs = notif_store.load_runs()
    mkeys = _manifest_keys()
    outcomes = notif_store.load_outcomes()
    items = notif_store.load()
    if wanted is not None:
        items = [n for n in items if n.instance_id == wanted]
    return {
        "notifications": [_serialize(n, runs=runs, manifest_keys=mkeys, outcomes=outcomes)
                          for n in items],
        "instance": wanted,
        "instances": instances.describe_all()["instances"],
    }


@app.get("/api/instances")
def list_instances() -> dict:
    """Registered Life instances of this device, with named degradation (§4.6).

    An instance registered but unreachable is reported with ``status:
    unreachable`` and a reason — it is never dropped from the list.
    """
    return instances.describe_all()


@app.get("/api/dm-replies")
def dm_replies_list(node: str | None = None, limit: int = 100) -> dict:
    """Private replies routed back to the node that asked (#55), newest last.

    Each row says how it was routed: ``bus`` (conversation id) or ``admin``
    (the bus refused; an admin was told). A node with no bus agent reads its
    answers here.
    """
    import dm_replies

    return {"replies": dm_replies.read_replies(node, max(1, min(limit, 500)))}


@app.get("/api/notifications/health")
def notifications_health(only_alarms: bool = False) -> dict:
    """Which enabled notifications have stopped actually sending, and why.

    Route order matters: this must stay ABOVE any ``/api/notifications/{key}``
    path, or FastAPI matches "health" as a key.

    ``status`` is ``critical`` when something that should have sent did not (or
    when the watchdog itself is mute), ``degraded`` when notifications are
    blocked/suppressed — named, never hidden — and ``ok`` only when every
    enabled notification has reached someone within its own cadence.
    """
    import notif_health

    report = notif_health.build_report()
    if only_alarms:
        report = {**report, "notifications": report["alarms"]}
    return report


# ---------------------------------------------------------------------------
# Endpoints — Task 2: POST / PATCH / DELETE
# ---------------------------------------------------------------------------

def _refuse_manifest_only(provider: str | None, params: dict | None = None) -> None:
    """Notifications that run code come from coordinator.toml, never from the API:
    a code-running provider, or an agent's ``params["command"]``."""
    if provider in notifications.MANIFEST_ONLY_PROVIDERS or str(
            (params or {}).get("command") or "").strip():
        raise HTTPException(
            403,
            detail=f"la notifica (provider '{provider}') esegue comandi: si dichiara "
                   "solo in coordinator.toml, non via API",
        )


@app.post("/api/notifications", status_code=201)
def create_notification(payload: NotificationIn) -> dict:
    _refuse_manifest_only(payload.provider, payload.params)
    instance_id = _require_instance(payload.instance)
    existing = {n.key for n in notif_store.load()}
    key = payload.key or notifications.gen_key(_slugify(payload.title), existing)
    if key in existing:
        raise HTTPException(409, detail=f"key già esistente: {key}")
    notif = _to_notification(payload, key=key)
    notif.instance_id = instance_id
    errors = notifications.validate(notif)
    if errors:
        raise HTTPException(422, detail={"errors": errors})
    notif_store.upsert(notif)
    scheduler.apply(notif)
    return {"key": key}


@app.patch("/api/notifications/{key}")
def update_notification(key: str, patch: NotificationPatch) -> dict:
    current = notif_store.get(key)
    if current is None:
        raise HTTPException(404, detail=f"notifica non trovata: {key}")
    updates = patch.model_dump(exclude_unset=True)
    _refuse_manifest_only(updates.get("provider"), updates.get("params"))
    if (current.provider in notifications.MANIFEST_ONLY_PROVIDERS
            or current.params.get("command")) and ("params" in updates or "provider" in updates):
        # enabled/title/schedule/recipients/destination stay editable; what runs does not.
        _refuse_manifest_only(current.provider, current.params)
    instance_id = _require_instance(updates.pop("instance", None), current=current)
    data = current.to_dict()
    for field, value in updates.items():
        data[field] = value
    data["instance_id"] = instance_id
    notif = Notification.from_dict(data)
    errors = notifications.validate(notif)
    if errors:
        raise HTTPException(422, detail={"errors": errors})
    notif_store.upsert(notif)
    scheduler.apply(notif)            # apply() registra o rimuove il job secondo enabled
    return {"key": key}


@app.delete("/api/notifications/{key}")
def delete_notification(key: str) -> dict:
    if not notif_store.delete(key):
        raise HTTPException(404, detail=f"notifica non trovata: {key}")
    scheduler.unschedule(key)
    return {"key": key}


# ---------------------------------------------------------------------------
# Endpoints — Task 3: GET /api/meta
# ---------------------------------------------------------------------------

@app.get("/api/meta")
def meta() -> dict:
    profs = [{"key": pr.key, "label": pr.label,
              "has_users": bool(profiles.user_ids_for(pr.key))}
             for pr in profiles.PROFILES]
    projs = [{"key": p.key, "label": p.label} for p in projects.list_projects()]
    groups = []
    for p in projects.list_projects():
        tg = manifest.telegram_of(p)
        if tg is not None and tg.group_chat_id is not None:
            groups.append({"project": p.key, "label": p.label,
                           "chat_id": tg.group_chat_id})
    return {
        "profiles": profs,
        "projects": projs,
        "groups": groups,
        "presets": dict(notifications._PRESETS),
        "providers": [p for p in notifications.VALID_PROVIDERS
                      if p not in notifications.MANIFEST_ONLY_PROVIDERS],
        "provider_scope": dict(providers.PROVIDER_PROJECT),
    }


# ---------------------------------------------------------------------------
# Endpoints — Task 4: POST /{key}/test + POST /preview
#
# NOTE: the /preview route MUST be defined BEFORE /{key}/test so that FastAPI
# does not match "preview" as a {key} parameter.
# ---------------------------------------------------------------------------

@app.post("/api/notifications/preview")
async def preview(payload: NotificationIn) -> dict:
    _refuse_manifest_only(payload.provider, payload.params)   # a preview would execute the command
    notif = _to_notification(payload, key=payload.key or "preview")
    errors = notifications.validate(notif)
    # 'nessun destinatario' non blocca l'anteprima (non si invia); filtra solo quello
    blocking = [e for e in errors if e != notifications.NO_RECIPIENTS_MSG]
    if blocking:
        raise HTTPException(422, detail={"errors": blocking})
    try:
        text = await asyncio.wait_for(
            providers.run_provider(notif, run_claude=scheduler.get_run_claude()),
            timeout=180,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, detail="anteprima scaduta (provider troppo lento)")
    return {"text": text or ""}


@app.post("/api/notifications/{key}/test")
async def test_now(key: str) -> dict:
    if notif_store.get(key) is None:
        raise HTTPException(404, detail=f"notifica non trovata: {key}")
    outcome = await scheduler._fire(key)   # provider + invio reale via send_target
    # `ok` significa INVIATA, non "ho provato": rispondere true su una notifica
    # scartata dalla policy è come ha fatto a restare invisibile per settimane.
    return {"ok": outcome == "inviata", "outcome": outcome}


# ---------------------------------------------------------------------------
# Endpoints — generic remote card protocol (Coordinator#45, spec §4.1)
# ---------------------------------------------------------------------------

@app.post("/api/cards")
async def post_card(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    token = request.headers.get("X-Card-Token")
    status, body = await remote_cards.show(payload, token)
    return JSONResponse(status_code=status, content=body)


class RoutePreviewIn(BaseModel):
    text: str
    root: str
    instance: str | None = None


@app.post("/api/route/preview")
async def route_preview(payload: RoutePreviewIn) -> dict:
    """Where a free-text message dropped in ``root``'s room would go (dry run).

    Same function the node bots call on first contact (``reparto_routing.route``),
    department agent included — so it spends one model call. Nothing is bound,
    nothing is sent to Telegram.

    ``instance`` (#78) selects whose graph the dry run reads; absent, the
    instance that owns ``root`` on this device is used (the device default when
    the node lives in none or in several). An unregistered id is a 422, exactly
    like the notification endpoints.
    """
    wanted = _valid_read_instance(payload.instance)
    if wanted:
        instance = wanted
    else:
        import bus_agents

        instance = bus_agents.instance_for_node(payload.root)
    nodes = await asyncio.to_thread(
        life_registry_client.load_graph_nodes, instance=instance
    )
    decision = await reparto_routing.route(payload.text, payload.root, nodes)
    return {
        "instance": instance,
        "kind": decision.kind,
        "node_id": decision.node_id,
        "planned": decision.planned,
        "reparto": decision.reparto,
        "motivo": decision.motivo,
        "candidates": [
            {"node_id": c.node_id, "score": c.score, "reasons": list(c.reasons)}
            for c in decision.candidates
        ],
    }


# ---------------------------------------------------------------------------
# Vision: receipt photo -> food lines (PersonalChef), same runner as the bots
# ---------------------------------------------------------------------------
@app.post("/api/vision/receipt")
async def vision_receipt_endpoint(
    request: Request,
    x_vision_token: str | None = Header(default=None),
) -> dict:
    # Raw body (Content-Type: image/*), NOT multipart: UploadFile needs
    # python-multipart, which the deployed image does not ship, and FastAPI
    # raises at route registration without it -- that would stop the whole API.
    # Mandatory (#88): an unset VISION_TOKEN closes the route, never opens it.
    expected = (os.getenv("VISION_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="VISION_TOKEN non configurato.")
    if not hmac.compare_digest((x_vision_token or "").encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Token non valido.")
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Il file deve essere un'immagine.")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > vision_receipt.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Immagine troppo grande (max 10 MB).")
    data = await request.body()
    if len(data) > vision_receipt.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Immagine troppo grande (max 10 MB).")
    try:
        return await vision_receipt.read_receipt(
            data, content_type, run_claude=scheduler.get_run_claude()
        )
    except vision_receipt.VisionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


# ---------------------------------------------------------------------------
# GET /api/conversations/{conv_id}/sessions — TicketManager#10
# ---------------------------------------------------------------------------

@app.get("/api/conversations/{conv_id}/sessions")
def conversation_sessions(conv_id: int) -> dict:
    """The claude sessions bus wakes ran (or are running) on a conversation.

    The Launcher opens the SAME session from a TicketManager card, so ``cwd``
    is where the session lives NOW — read from its transcript, worktree
    included — not the folder the wake launched it in.
    """
    import hub
    import hub_origin
    import hub_store

    rows = hub_origin.conversation_sessions(
        hub_store.get_conv(conv_id), hub._HUB_PROJECTS_DIR
    )
    if not rows:
        raise HTTPException(status_code=404, detail="conversation has no sessions")
    return {"conversation_id": conv_id, "sessions": rows}
