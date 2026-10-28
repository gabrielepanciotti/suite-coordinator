# instances.py
"""Risoluzione delle istanze Life per il Coordinator (operatore, Life §4.1).

Il Coordinator e' un **operatore**: serve ogni istanza registrata sul device,
mai una sola scelta a boot. Le istanze si leggono dal **registro del device**
(``$LIFE_HOME/instances.json``, default ``~/.life``), contratto normativo in
``Life/docs/instance-resolution.md`` e implementazione di riferimento in
``packages/shared/suite_core/instances``.

Questo modulo replica *la forma* del registro e le regole R-1..R-9 senza
importare ``suite_core``: l'immagine del bot non spedisce i package di Life.
E' **read-only** sul registro: registrare/forget e' compito della CLI di Life.

Perche' esiste (issue Coordinator#78): finche' l'istanza era una sola bastava
``LIFE_INSTANCE_DIR``. Con N istanze ``LIFE_INSTANCE_DIR`` non regge piu':
un operatore deve enumerare il registro, dare a ogni record l'``instance_id``
che l'ha prodotto, e leggere canali e credenziali **dalla config dell'istanza**.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # the single channel reader of Life, when suite_core is installed (dev box)
    from suite_core.instances.channels import read_channels
except ImportError:  # the bot image does not ship Life: verbatim copy, drift-tested
    from life_channels import read_channels

logger = logging.getLogger("coordinator.instances")

BAND_SIZE = 100
"""Ampiezza di una banda di porte. Deve coincidere con Life (``paths.BAND_SIZE``)."""

INSTANCE_CONFIG_NAME = "instance.config.json"
"""Il solo contratto fra framework e istanza: ``<path-istanza>/instance.config.json``."""

# R-9: un id canonico finisce in nomi di database e in path.
ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

_LEGACY_SELECTORS = ("LIFE_INSTANCE_DIR", "LIFE_PROFILE")


class InstanceError(Exception):
    """Errore di configurazione del registro. Sempre fatale, mai degradato."""


class UnknownInstance(InstanceError):
    """L'id richiesto non e' registrato su questo device."""


@dataclass(frozen=True)
class Instance:
    """Una istanza registrata su questo device."""

    id: str
    path: Path
    profile: str
    band: int
    registered_at: str = ""
    sensitive: bool = False

    @property
    def offset(self) -> int:
        return self.band * BAND_SIZE


@dataclass(frozen=True)
class Resolution:
    """Esito della risoluzione, con la ragione in chiaro (R-7)."""

    id: str | None
    path: str | None
    profile: str | None
    band: int
    offset: int
    level: str
    reason: str
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Channels:
    """Canali e credenziali di una istanza (§4.4).

    Si leggono dalla config dell'istanza (``instance.config.json`` -> ``channels``).
    Solo per l'istanza di default del device, in **modalita' legacy** (registro
    vuoto), si ricade sulle variabili globali: e' il percorso non-distruttivo
    della migrazione, non un default per le istanze registrate.
    """

    instance_id: str
    bus_tenant: str = ""
    telegram_chat_id: int | None = None
    telegram_agent: str = ""
    network_ui_url: str = ""
    clickup_workspace: str = ""
    drive_folder: str = ""
    source: str = "none"  # "instance" | "global" | "none"


# ---------------------------------------------------------------------------
# Posizione dei file del registro (D4: calcolata a mano, identica a Life)
# ---------------------------------------------------------------------------

def life_home(env: Mapping[str, str] | None = None) -> Path:
    """Directory del registro: ``$LIFE_HOME`` se impostata, altrimenti ``~/.life``."""
    environ = os.environ if env is None else env
    override = environ.get("LIFE_HOME")
    if override:
        return Path(override)
    return Path.home() / ".life"


def instances_file(env: Mapping[str, str] | None = None) -> Path:
    return life_home(env) / "instances.json"


def state_file(env: Mapping[str, str] | None = None) -> Path:
    return life_home(env) / "state.json"


# ---------------------------------------------------------------------------
# Lettura (R-6: una entry malformata produce warning e viene saltata)
# ---------------------------------------------------------------------------

def _read_json(path: Path, fallback: dict, env: Mapping[str, str] | None) -> Any:
    if not path.exists():
        return dict(fallback)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InstanceError(f"{path} non e' leggibile: {exc}") from exc


def _is_valid_entry(entry: Any) -> bool:
    """Stessa validazione stretta di ``suite_core.instances.store`` (R-6)."""
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and isinstance(entry.get("profile"), str)
        and isinstance(entry.get("band"), int)
        and not isinstance(entry.get("band"), bool)
    )


def load_registry(
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, Instance], list[str]]:
    """Legge l'elenco delle istanze. Le entry malformate vengono saltate."""
    path = instances_file(env)
    raw = _read_json(path, {"version": 1, "instances": {}}, env)
    if not isinstance(raw, dict):
        raise InstanceError(f"{path} non e' un oggetto JSON")
    entries = raw.get("instances") or {}
    if not isinstance(entries, dict):
        raise InstanceError(f"{path}: 'instances' non e' un oggetto JSON")
    instances: dict[str, Instance] = {}
    warnings: list[str] = []
    for key, entry in entries.items():
        if not isinstance(key, str) or not ID_RE.match(key):
            warnings.append(f"istanza {key!r} ignorata: id non canonico")
            continue
        if not _is_valid_entry(entry):
            warnings.append(f"istanza {key!r} ignorata: configurazione incompleta")
            continue
        instances[key] = Instance(
            id=key,
            path=Path(entry["path"]),
            profile=entry["profile"],
            band=entry["band"],
            registered_at=entry.get("registeredAt", ""),
            sensitive=bool(entry.get("sensitive", False)),
        )
    return instances, warnings


def load_state(env: Mapping[str, str] | None = None) -> dict:
    """Legge lo stato mutabile (default attivo, prossima banda)."""
    path = state_file(env)
    state = _read_json(path, {"version": 1, "default": None, "nextBand": 0}, env)
    if not isinstance(state, dict):
        raise InstanceError(f"{path} non e' un oggetto JSON")
    return state


# ---------------------------------------------------------------------------
# Risoluzione (precedenza a quattro livelli, Life §5 / R-1..R-9)
# ---------------------------------------------------------------------------

def resolve(
    explicit_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Resolution:
    """Risolve UNA istanza. Precedenza: explicit > LIFE_INSTANCE > default > legacy.

    Un id non registrato e' sempre un errore (R-1), mai un fallback silenzioso.
    La modalita' legacy (registro vuoto + ``LIFE_INSTANCE_DIR``/``LIFE_PROFILE``)
    torna ``id=None`` ma consegna il ``path`` perche' l'operatore possa leggere
    la config: e' il gradino non-distruttivo della migrazione.
    """
    environ = os.environ if env is None else env
    instances, warnings = load_registry(environ)
    state = load_state(environ)

    env_id = (environ.get("LIFE_INSTANCE") or "").strip() or None
    legacy_dir_raw = (environ.get("LIFE_INSTANCE_DIR") or "").strip() or None

    if explicit_id and env_id and explicit_id != env_id:
        warnings = [*warnings, f"--instance={explicit_id} scavalca LIFE_INSTANCE={env_id}"]

    if explicit_id:
        if explicit_id not in instances:
            raise UnknownInstance(_unknown_msg(explicit_id, instances))
        chosen, level, source = explicit_id, "explicit", "--instance"
    elif env_id:
        if env_id not in instances:
            raise UnknownInstance(_unknown_msg(env_id, instances))
        chosen, level, source = env_id, "env", "LIFE_INSTANCE"
    elif state.get("default"):
        chosen, level, source = str(state["default"]), "default", "default del device"
        if chosen not in instances:
            raise UnknownInstance(_unknown_msg(chosen, instances))
    elif instances:
        known = ", ".join(sorted(instances))
        raise InstanceError(
            f"istanze registrate ma nessuna e' il default del device: {known}. "
            "Imposta il default con 'life instances use <id>'."
        )
    else:
        legacy = [name for name in _LEGACY_SELECTORS if environ.get(name)]
        if legacy:
            return Resolution(
                id=None,
                path=_normalise_path(legacy_dir_raw) if legacy_dir_raw else None,
                profile=environ.get("LIFE_PROFILE"),
                band=0,
                offset=0,
                level="legacy",
                reason=f"modalita' legacy: {legacy[0]} presente e nessun registro",
                warnings=warnings,
            )
        return Resolution(
            id=None, path=None, profile=None, band=0, offset=0,
            level="framework",
            reason="framework standalone: nessun registro e nessun selettore",
            warnings=warnings,
        )

    inst = instances[chosen]
    inst_path = _normalise_path(str(inst.path))
    if not inst.path.is_dir():
        raise UnknownInstance(
            f"istanza {chosen!r} registrata ma il path non esiste: {inst_path}"
        )
    if level == "env" and state.get("default") and state["default"] != chosen:
        warnings = [
            *warnings,
            f"LIFE_INSTANCE={chosen} ombreggia il default del device ({state['default']})",
        ]

    return Resolution(
        id=inst.id,
        path=inst_path,
        profile=inst.profile,
        band=inst.band,
        offset=inst.offset,
        level=level,
        reason=f"istanza={inst.id} da {source}, path={inst_path}, banda={inst.band}",
        warnings=warnings,
    )


def _unknown_msg(chosen: str, instances: Mapping[str, Instance]) -> str:
    known = ", ".join(sorted(instances)) or "(nessuna)"
    return f"istanza {chosen!r} non registrata su questo device. Registrate: {known}"


def _normalise_path(value: str) -> str:
    return value.replace("\\", "/")


def legacy_instance_id(env: Mapping[str, str] | None = None) -> str | None:
    """L'id dell'unica istanza di un device senza registro.

    Dal ``profile`` di ``<LIFE_INSTANCE_DIR>/instance.config.json`` (il profilo
    *e'* l'id dell'istanza), altrimenti da ``LIFE_INSTANCE``, altrimenti dal
    nome della cartella — mai dal punto di mount neutro (``life-instance``).
    ``None`` su un device framework puro (nessun selettore). E' il gradino
    non-distruttivo: un device a istanza sola continua a produrre record con un
    ``instance_id`` vero, senza dover registrare nulla.
    """
    environ = os.environ if env is None else env
    instance_dir = (environ.get("LIFE_INSTANCE_DIR") or "").strip()
    if instance_dir:
        profile = str(_read_config(Path(instance_dir)).get("profile") or "").strip()
        if profile:
            return profile
    explicit = (environ.get("LIFE_INSTANCE") or "").strip()
    if explicit:
        return explicit
    if instance_dir:
        name = Path(instance_dir).name
        if name and name not in ("life-instance", "instance"):
            return name
    return None


def default_instance_id(env: Mapping[str, str] | None = None) -> str | None:
    """L'id dell'istanza di default del device, o ``None`` su framework puro.

    E' l'id con cui vengono etichettati i record *prodotti oggi* da questo
    device (migrazione additiva di §4.2): le righe esistenti sono di questa
    istanza perche' finora il device ne serviva una sola. Con un registro vale
    il default del device; senza registro vale l'unica istanza legacy.
    """
    try:
        resolved = resolve(env=env).id
    except InstanceError:
        resolved = None
    return resolved or legacy_instance_id(env)


def get(instance_id: str, env: Mapping[str, str] | None = None) -> Instance | None:
    """L'istanza registrata con questo id, o None."""
    instances, _ = load_registry(env)
    return instances.get(instance_id)


def known_ids(env: Mapping[str, str] | None = None) -> list[str]:
    instances, _ = load_registry(env)
    return sorted(instances)


# ---------------------------------------------------------------------------
# Config per istanza (§4.4): canali e credenziali dalla config dell'istanza
# ---------------------------------------------------------------------------

def _read_config(instance_path: Path) -> dict:
    """``<path>/instance.config.json`` come dict, o ``{}`` se assente/malformato.

    Un file malformato e' un errore d'uso dell'istanza (non si indovina), ma non
    deve abbattere l'operatore: si logga e si degrada a ``{}``.
    """
    path = Path(instance_path) / INSTANCE_CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("instance.config.json di %s illeggibile: %s", instance_path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def instance_config(instance: Instance) -> dict:
    """La config dell'istanza (``instance.config.json``), o ``{}`` se assente."""
    return _read_config(Path(instance.path))


def channels_of(
    instance: Instance | None,
    *,
    env: Mapping[str, str] | None = None,
) -> Channels:
    """Canali di invio di una istanza.

    Lo schema e' quello di Life (``docs/instance.config.schema.md`` §Channels) e
    lo legge **il lettore unico di Life**, ``suite_core.instances.channels``: niente
    parsing proprio qui. Life lo legge anche nelle chiavi piatte vecchie
    (``busTenant``...). ``network_ui_url`` non e' un canale dell'istanza (e'
    un indirizzo per-device): viene sempre da ``NETWORK_UI_URL``.

    Quando l'istanza non dichiara canali ed e' l'istanza di default del device
    (legacy a istanza sola), si ricade sulle variabili globali del processo.
    """
    environ = os.environ if env is None else env
    if instance is None:
        return _channels_from_global("<legacy>", environ)

    cfg = instance_config(instance)
    if isinstance(cfg.get("channels"), dict) and cfg["channels"]:
        life = read_channels(instance.id, cfg)
        for problem in life.problems:
            logger.warning("istanza %s, canali: %s", instance.id, problem)
        return Channels(
            instance_id=instance.id,
            bus_tenant=life.bus_tenant,
            telegram_chat_id=life.telegram_chat_id,
            telegram_agent=life.telegram_agent,
            network_ui_url=_network_ui_url(environ),
            clickup_workspace=life.clickup_workspace,
            drive_folder=life.drive_folder,
            source="instance",
        )

    # Nessun canale dichiarato: fallback globale SOLO se questa e' l'istanza di
    # default del device (legacy a un'istanza sola). Un'istanza registrata ma
    # senza `channels` resta senza canali — nominata, non riempita a caso.
    if default_instance_id(environ) == instance.id:
        return _channels_from_global(instance.id, environ)
    return Channels(instance_id=instance.id, source="none")


def _network_ui_url(env: Mapping[str, str]) -> str:
    return (env.get("NETWORK_UI_URL") or "").strip().rstrip("/")


def _global_chat_id(env: Mapping[str, str]) -> int | None:
    raw = (env.get("HUB_TELEGRAM_CHAT_ID") or "").strip()
    try:
        chat = int(raw) if raw else None
    except ValueError:
        return None
    return chat if chat not in (None, 0) else None


def _channels_from_global(instance_id: str, env: Mapping[str, str]) -> Channels:
    tenants = [t.strip() for t in (env.get("HUB_TENANTS") or "").split(",") if t.strip()]
    chat = _global_chat_id(env)
    return Channels(
        instance_id=instance_id,
        bus_tenant=tenants[0] if tenants else "",
        telegram_chat_id=chat,
        telegram_agent=(env.get("HUB_TELEGRAM_AGENT") or "").strip(),
        network_ui_url=_network_ui_url(env),
        source="global" if (tenants or chat) else "none",
    )


# ---------------------------------------------------------------------------
# Vista "tutte le istanze" con degrado nominato (§4.6)
# ---------------------------------------------------------------------------

def describe(instance: Instance, *, env: Mapping[str, str] | None = None) -> dict:
    """Una riga per il selettore d'istanza: id, profilo, stato, canali.

    ``status`` e' ``ok`` o ``unreachable``. Un'istanza registrata ma
    irraggiungibile **compare** col suo motivo (degradazione nominata): non
    sparisce dalla vista.
    """
    reachable = Path(instance.path).is_dir()
    ch = channels_of(instance, env=env)
    return {
        "id": instance.id,
        "profile": instance.profile,
        "band": instance.band,
        "path": _normalise_path(str(instance.path)),
        "sensitive": instance.sensitive,
        "status": "ok" if reachable else "unreachable",
        "reason": "" if reachable else f"path non raggiungibile: {instance.path}",
        "channels": {
            "bus_tenant": ch.bus_tenant,
            "telegram_chat_id": ch.telegram_chat_id,
            "telegram_agent": ch.telegram_agent,
            "network_ui_url": ch.network_ui_url,
            "source": ch.source,
        },
    }


def describe_all(env: Mapping[str, str] | None = None) -> dict:
    """Tutte le istanze registrate + l'istanza di default, per l'API/frontend."""
    instances, warnings = load_registry(env)
    default_id = default_instance_id(env)
    return {
        "default": default_id,
        "instances": [describe(inst, env=env) for inst in instances.values()],
        "warnings": warnings,
    }
