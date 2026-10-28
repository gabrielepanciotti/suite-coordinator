"""taskgen — motore autonomo di generazione task (pure pieces).

Pure functions only in this slice: parse_suggestions, apply_policy, Dedup.
No scheduler, no network, no bot import (grafo aciclico: run_claude verrà
INIETTATO nell'orchestratore run_watcher di un task successivo, come fa
providers.py — mai `import bot` da qui).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

import llm_usage
import wake_routing
from taskgen_config import Suggestion, TrustPolicy, Watcher

logger = logging.getLogger("coordinator.taskgen")

# Declared in <istanza>/config/llm-routing.json: model and fallback are the queue's.
SERVICE = "coordinator.taskgen"

_DEFAULT_TTL_SECONDS = 86400
_SLUG_INVALID = re.compile(r"[^a-z0-9-]+")


def _find_balanced_braces(text: str) -> list[str]:
    """Return all top-level-from-each-start balanced {...} substrings."""
    results: list[str] = []
    n = len(text)
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        for j in range(i, n):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    results.append(text[i : j + 1])
                    break
    return results


def _suggestions_from_obj(obj: dict) -> list[Suggestion] | None:
    """Build Suggestion list from a parsed {"suggestions": [...]} dict.

    Returns None if the dict doesn't have a usable "suggestions" list.
    """
    if not isinstance(obj, dict) or "suggestions" not in obj:
        return None
    items = obj.get("suggestions")
    if not isinstance(items, list):
        return None
    out: list[Suggestion] = []
    try:
        for it in items:
            out.append(
                Suggestion(
                    title=it["title"],
                    category=it["category"],
                    when_iso=it["when_iso"],
                    duration_min=it["duration_min"],
                    description=it.get("description"),
                    why=it.get("why"),
                )
            )
    except (KeyError, TypeError):
        return None
    return out


def parse_suggestions(text: str) -> list[Suggestion]:
    """Extract the FIRST valid JSON object with key "suggestions" from text.

    Handles ```json fences and bare {...} blocks (the balanced-brace scan
    finds depth-balanced {...} substrings anywhere in the text, including
    inside fenced code blocks). Never raises; returns [] on any failure or
    absence of a usable block.
    """
    try:
        if not text:
            return []

        candidates = _find_balanced_braces(text)

        for cand in candidates:
            try:
                obj = json.loads(cand)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            suggestions = _suggestions_from_obj(obj)
            if suggestions is not None:
                return suggestions

        return []
    except Exception:
        return []


def apply_policy(sugg: Suggestion, policy: TrustPolicy, emitted_today: int) -> str:
    """Return "trusted" or "propose" for a suggestion given the daily cap."""
    if emitted_today >= policy.daily_cap:
        return "propose"
    return policy.decision(sugg.category)


class Dedup:
    """In-memory (persistible) dedup set with TTL, driven by injected time.

    Never calls datetime.now()/time.time() internally — `now` is always
    passed in by the caller so tests stay deterministic.
    """

    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, float] = {}

    @staticmethod
    def key(sugg: Suggestion) -> str:
        raw = f"{sugg.title}|{sugg.when_iso}|{sugg.category}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def seen(self, sugg: Suggestion, *, now: float) -> bool:
        recorded_at = self._store.get(self.key(sugg))
        if recorded_at is None:
            return False
        return (now - recorded_at) <= self.ttl_seconds

    def record(self, sugg: Suggestion, *, now: float) -> None:
        self._store[self.key(sugg)] = now

    def to_dict(self) -> dict[str, float]:
        return dict(self._store)

    @classmethod
    def from_dict(cls, d: dict[str, float], ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> "Dedup":
        instance = cls(ttl_seconds=ttl_seconds)
        instance._store = dict(d)
        return instance


def build_introspection_seed(node_key: str) -> str:
    """Prompt asking a node to self-inspect and propose schedulable suggestions.

    The node runs headless (`run_claude`, injected) with this seed as its
    only instruction. It must reply with ONLY a fenced or bare JSON block
    shaped ``{"suggestions": [...]}`` (empty list if nothing worth
    surfacing) — see spec §3.3. ``parse_suggestions`` extracts the first
    valid such block from whatever text comes back.
    """
    return (
        f"Sei il nodo `{node_key}`. Ispeziona il tuo dominio (contesto, dati, "
        "vault/engine se presenti) e individua eventuali attività pianificabili "
        "utili alla persona (es. scadenze, promemoria, task ricorrenti).\n\n"
        "Rispondi SOLO con un blocco JSON, nessun altro testo, in questo schema:\n"
        '{"suggestions": [{"title": "...", "category": "...", '
        '"when_iso": "2026-07-07T20:00:00+02:00", "duration_min": 45, '
        '"description": "...", "why": "..."}]}\n\n'
        "Se non c'è nulla da suggerire, rispondi con "
        '{"suggestions": []}.'
    )


def _node_slug(node_key: str) -> str:
    """Derive a lowercase ``^[a-z0-9-]+$`` slug from a watcher's node_key.

    Prefers a real slug attribute exposed by the project registry
    (`projects.resolve_project`), when one exists; otherwise falls back to
    lowercasing `node_key` and stripping any character outside
    `[a-z0-9-]`. Imported lazily to keep taskgen's import graph acyclic
    (taskgen never imports bot.py; projects.py doesn't either).
    """
    slug = None
    try:
        import projects as _projects

        project = _projects.resolve_project(node_key)
        candidate = getattr(project, "slug", None) if project is not None else None
        if candidate:
            slug = str(candidate)
    except Exception:
        slug = None
    if not slug:
        slug = node_key
    slug = slug.lower()
    slug = _SLUG_INVALID.sub("-", slug).strip("-")
    return slug or "node"


def resolve_category_id(category, categories, default_category_id):
    """Case-insensitive match of a free-form category string to a Scheduler
    category id. Falls back to default_category_id when no name matches."""
    target = (category or "").strip().lower()
    for c in categories or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("name", "")).strip().lower() == target:
            return c.get("id")
    return default_category_id


async def run_watcher(
    watcher: Watcher,
    *,
    run_claude,
    hw_client,
    policy: TrustPolicy,
    dedup: "Dedup",
    resolve_workspace,
    send_notice,
    telegram_chat_id: int,
    now: float,
    default_category_id: int | None = None,
) -> dict:
    """Run one watcher tick end-to-end: introspect → parse → policy → emit.

    `run_claude`, `resolve_workspace` and `send_notice` are INJECTED
    callables (mocked in tests, bound to real bot functions in Task 5) —
    this module never imports bot.py, keeping the import graph acyclic.

    `Suggestion.category` is a free-form string; it is resolved to a real
    Scheduler category id via `resolve_category_id`, matched case-insensitively
    against `hw_client.list_categories()` (fetched once per tick, tolerating
    any failure — falls back to `[]` so resolution degrades to
    `default_category_id`). Used for both the trusted branch's
    `create_scheduled_task(category_id=...)` and the propose branch's payload.

    Propose branch (`create_proposal(kind="schedulable", ...)`) requires a
    full Scheduler ``TaskCreate``-shaped payload (name/category_id/
    duration_estimated/schedule_mode). If resolution yields no id (no name
    match and `default_category_id` is None), the proposal cannot be built:
    it is counted as a failure (no dedup record, so it's retried once the
    config is set) rather than sent as an invalid request that Scheduler
    would 422.

    Each suggestion's emission (Scheduler call + notice) is isolated in a
    try/except: a failure for one suggestion is logged and counted under
    ``"failed"`` without aborting the tick or the dedup record for that
    suggestion — so it is retried on the next tick (idempotent retry).

    Returns counters: ``{"trusted": n, "proposed": n, "skipped": n, "failed": n}``.
    """
    counters = {"trusted": 0, "proposed": 0, "skipped": 0, "failed": 0}

    seed = build_introspection_seed(watcher.node_key)
    run = await wake_routing.run_node_service(SERVICE, watcher.node_key, seed)
    if run is None:
        run = await run_claude(
            seed,
            workspace=resolve_workspace(watcher.node_key),
            persona=None,
            resume_id=None,
        )
    text, _sid = run
    llm_usage.record(run, node=watcher.node_key, caller="taskgen")
    suggestions = parse_suggestions(text)

    try:
        categories = await hw_client.list_categories()
    except Exception:
        categories = []

    node_slug = _node_slug(watcher.node_key)
    emitted_today = 0

    for sugg in suggestions:
        if dedup.seen(sugg, now=now):
            counters["skipped"] += 1
            continue

        decision = apply_policy(sugg, policy, emitted_today=emitted_today)

        try:
            if decision == "trusted":
                result = await hw_client.create_scheduled_task(
                    telegram_chat_id=telegram_chat_id,
                    name=sugg.title,
                    category_id=resolve_category_id(sugg.category, categories, default_category_id),
                    start_at=sugg.when_iso,
                    duration_min=sugg.duration_min,
                    description=sugg.description,
                )
                await send_notice(
                    kind="scheduled",
                    suggestion=sugg,
                    st_id=result.get("id") if isinstance(result, dict) else None,
                    telegram_chat_id=telegram_chat_id,
                )
            else:
                resolved = resolve_category_id(sugg.category, categories, default_category_id)
                if resolved is None:
                    logger.warning(
                        "taskgen watcher=%s suggestion=%r propose skipped: "
                        "no default_category_id configured",
                        watcher.key, sugg.title,
                    )
                    counters["failed"] += 1
                    continue
                payload = {
                    "name": sugg.title,
                    "category_id": resolved,
                    "duration_estimated": sugg.duration_min,
                    "schedule_mode": "weekly_count",
                    "weekly_count": 1,
                    "description": sugg.description,
                }
                proposal = await hw_client.create_proposal(
                    kind="schedulable",
                    node_slug=node_slug,
                    summary=sugg.title,
                    payload=payload,
                    telegram_chat_id=telegram_chat_id,
                )
                await send_notice(
                    kind="proposed",
                    suggestion=sugg,
                    proposal_id=proposal.get("id") if isinstance(proposal, dict) else None,
                    telegram_chat_id=telegram_chat_id,
                )
        except Exception as exc:
            logger.warning(
                "taskgen watcher=%s suggestion=%r failed: %s",
                watcher.key, sugg.title, exc,
            )
            counters["failed"] += 1
            continue

        if decision == "trusted":
            emitted_today += 1
            counters["trusted"] += 1
        else:
            counters["proposed"] += 1

        dedup.record(sugg, now=now)

    logger.info(
        "taskgen watcher=%s trusted=%d proposed=%d skipped=%d failed=%d",
        watcher.key, counters["trusted"], counters["proposed"], counters["skipped"],
        counters["failed"],
    )
    return counters
