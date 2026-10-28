# llm_balance.py
"""Watchdog on the DeepSeek balance (Coordinator#95).

The failure this module exists for: every bus wake and every scheduled job that
runs on DeepSeek started answering ``opencode: Insufficient Balance``, and the
first person to learn about it was whoever read the failed-fallback notice. The
provider publishes the number in advance — ``GET /user/balance`` — so it can be
asked on a schedule instead of being discovered from a chain of failures.

Two rules, inherited from ``notif_health`` and the sentinella:

* **Speak only when there is something to say.** A healthy balance produces
  ``None`` (the notification records "nothing to send", not a mute alarm).
* **Do not repeat forever.** While the balance stays at zero the alert is
  repeated at most once every ``REPEAT_SEC``; the recovery says so once.

The API key never appears in a log, an exception or the alert text: the request
header is built from the environment, errors carry only the status code, and the
message never echoes the payload.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

import atomic_io
import notif_store
import notifications

logger = logging.getLogger("coordinator.llm_balance")

KEY_ENV = "DEEPSEEK_API_KEY"
BALANCE_URL = os.environ.get("DEEPSEEK_BALANCE_URL", "https://api.deepseek.com/user/balance")
TIMEOUT_SEC = float(os.environ.get("DEEPSEEK_BALANCE_TIMEOUT_SEC", "10"))

WATCHDOG_KEY = "coordinator-saldo-deepseek"
WATCHDOG_TITLE = "Saldo DeepSeek"
WATCHDOG_PROVIDER = "deepseek-balance"
WATCHDOG_SCHEDULE = os.environ.get("DEEPSEEK_BALANCE_SCHEDULE", "0 * * * *")
# Where the periodic balance alert is delivered: the same device-side destination
# as the notification watchdog, so no chat id enters a versioned file.
WATCHDOG_DEST_ENV = "NOTIF_HEALTH_DEST"
STATE_PATH = os.environ.get("DEEPSEEK_BALANCE_STATE", "/app/data/deepseek_balance.json")
# While the balance is still zero: repeat no more often than this (6 h).
REPEAT_SEC = int(os.environ.get("DEEPSEEK_BALANCE_REPEAT_SEC", str(6 * 3600)))


class BalanceError(Exception):
    """The balance could not be read. The text never contains the API key."""


def configured(env=None) -> bool:
    """Is this deployment supposed to spend on DeepSeek?

    A claude-only process has no DeepSeek balance to watch. A missing key is not
    an error either: it is a device where the provider is not configured.
    """
    env = os.environ if env is None else env
    backend = (env.get("AI_BACKEND") or "claude").strip().lower()
    return backend == "opencode" and bool((env.get(KEY_ENV) or "").strip())


def _amount(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_balance(payload: object) -> tuple[bool, str]:
    """``(is_zero, human detail)`` from the ``/user/balance`` body.

    ``is_available`` is authoritative when present (DeepSeek sets it false once
    there is nothing left). Otherwise the totals decide, and a body we cannot
    read is NOT an alarm: a parsing bug must not page a person.
    """
    if not isinstance(payload, dict):
        return False, "risposta inattesa dal servizio"
    infos = [i for i in payload.get("balance_infos") or [] if isinstance(i, dict)]
    amounts = []
    for info in infos:
        amount = _amount(info.get("total_balance"))
        if amount is not None:
            amounts.append((str(info.get("currency") or "?").upper(), amount))
    detail = ", ".join(f"{currency} {amount:g}" for currency, amount in amounts)
    available = payload.get("is_available")
    if available is False:
        return True, detail or "saldo non dichiarato"
    if available is True:
        return False, detail or "saldo disponibile"
    if amounts:
        return all(amount <= 0 for _, amount in amounts), detail
    return False, "saldo non dichiarato"


def alert_text(detail: str) -> str:
    return (
        "🔴 <b>Saldo DeepSeek esaurito</b>\n"
        f"Saldo attuale: {detail}.\n"
        "I risvegli dal bus e i job a orario su DeepSeek falliranno finché non "
        "ricarichi o cambi coda/backend. (Nessuna chiave è inclusa in questo avviso.)"
    )


def recovery_text(detail: str) -> str:
    return (
        "🟢 <b>Saldo DeepSeek ripristinato</b>\n"
        f"Saldo attuale: {detail}. I risvegli e i job su DeepSeek possono ripartire."
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, state: dict) -> None:
    if not path.parent.is_dir():
        return
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        atomic_io.replace_with_retry(tmp, path)
    except Exception:
        logger.exception("stato saldo DeepSeek non persistito su %s", path)


def decide(
    zero: bool,
    detail: str,
    state: dict,
    now: datetime,
    *,
    repeat_sec: int = REPEAT_SEC,
) -> tuple[str | None, dict]:
    """The message to send (or None) and the new state, from one reading.

    Transition into zero -> one alert. Still zero -> silence until
    ``repeat_sec`` has elapsed since the last alert. Back to credit -> one
    recovery message, only if an alert had been sent.
    """
    was_alarm = bool(state.get("alarm"))
    if not zero:
        new = {"alarm": False, "last_alert_iso": "", "detail": detail}
        return (recovery_text(detail) if was_alarm else None), new
    last = state.get("last_alert_iso") or ""
    if was_alarm and last:
        try:
            elapsed = (now - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            elapsed = repeat_sec
        if elapsed < repeat_sec:
            return None, {**state, "alarm": True, "detail": detail}
    new = {"alarm": True, "last_alert_iso": now.isoformat(), "detail": detail}
    return alert_text(detail), new


def _state_path(path) -> Path:
    return Path(path or STATE_PATH)


async def fetch_balance(
    key: str, *, url: str = BALANCE_URL, timeout: float = TIMEOUT_SEC, transport=None
) -> dict:
    """``GET /user/balance``. Raises :class:`BalanceError` without the key."""
    if not key:
        raise BalanceError("chiave DeepSeek non configurata")
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise BalanceError(f"rete: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise BalanceError(f"HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise BalanceError("risposta non JSON") from None


async def check(
    *,
    now: datetime | None = None,
    env=None,
    state_path=None,
    transport=None,
    fetch=None,
) -> str | None:
    """One balance reading: the alert text, or None. Never raises."""
    env = os.environ if env is None else env
    if not configured(env):
        return None
    key = (env.get(KEY_ENV) or "").strip()
    path = _state_path(state_path)
    try:
        payload = await (fetch or fetch_balance)(
            key,
            url=(env.get("DEEPSEEK_BALANCE_URL") or BALANCE_URL),
            transport=transport,
        )
    except BalanceError as exc:
        logger.warning("saldo DeepSeek non leggibile: %s", exc)
        return None
    except Exception as exc:  # a provider must never kill the scheduler
        logger.warning("saldo DeepSeek non leggibile: %s", type(exc).__name__)
        return None
    zero, detail = parse_balance(payload)
    previous = _read_state(path)
    text, state = decide(zero, detail, previous, now or _now())
    if state != previous:
        _write_state(path, state)
    return text


def ensure_watchdog(env=None) -> str | None:
    """Create/refresh the balance notification. Returns its key, or None.

    Like ``notif_health.ensure_watchdog``: idempotent, keeps a manual disable,
    and does nothing on a device where DeepSeek is not in use.
    """
    env = os.environ if env is None else env
    if not configured(env):
        return None
    dest = (env.get(WATCHDOG_DEST_ENV) or "").strip()
    current = notif_store.get(WATCHDOG_KEY)
    if not dest and current is None:
        logger.warning(
            "%s not set: no DeepSeek balance alert exists (GET /user/balance is "
            "only visible from the logs)", WATCHDOG_DEST_ENV,
        )
        return None
    notif = notifications.Notification(
        key=WATCHDOG_KEY,
        title=WATCHDOG_TITLE,
        provider=WATCHDOG_PROVIDER,
        schedule=(env.get("DEEPSEEK_BALANCE_SCHEDULE") or WATCHDOG_SCHEDULE),
        recipients=(),
        enabled=current.enabled if current is not None else True,
        params={},
        destination=dest or (current.destination if current else "dm"),
    )
    notif_store.upsert(notif)
    return notif.key
