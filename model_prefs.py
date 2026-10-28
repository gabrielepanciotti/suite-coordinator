"""Per-turn and per-topic model / effort selection for Telegram messages.

Two ways to steer a turn, deliberately overlapping:

* **inline prefix** — ``opus/high: refactor the poller`` applies to THAT message
  only. Order is free (``high/opus:``), one token is enough (``opus:``, ``max:``).
* **sticky topic default** — ``/modello opus max`` stores the pair for the
  (chat, topic) card, so every later message inherits it until it is changed or
  cleared with ``/modello auto``.

Precedence per turn: inline prefix > topic default > the CLAUDE_MODEL env
default. Effort is independent of the model: an unset effort simply omits
``--effort`` and lets the CLI pick.

Telegram-independent (stdlib only) so bot.py, the headless workers and the tests
all share one parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Canonical model names accepted by ``claude --model`` plus the shorthands a
# phone keyboard actually types. Values are what we hand to the CLI.
MODEL_ALIASES: dict[str, str] = {
    "opus": "opus",
    "o": "opus",
    "sonnet": "sonnet",
    "s": "sonnet",
    "haiku": "haiku",
    "h": "haiku",
    "fable": "fable",
    "f": "fable",
}

# ``claude --effort <level>``: low, medium, high, xhigh, max.
EFFORT_ALIASES: dict[str, str] = {
    "low": "low",
    "basso": "low",
    "medium": "medium",
    "med": "medium",
    "medio": "medium",
    "high": "high",
    "alto": "high",
    "xhigh": "xhigh",
    "xh": "xhigh",
    "max": "max",
    "massimo": "max",
}

# One or two tokens, separated by ``/`` or ``,``, ending in ``:``. Anchored at
# the very start so only a deliberate prefix is consumed — a colon anywhere
# later in the sentence is left alone.
_PREFIX_RE = re.compile(r"^\s*([A-Za-z]+)(?:\s*[/,]\s*([A-Za-z]+))?\s*:\s*", re.ASCII)


@dataclass(frozen=True)
class Prefs:
    """A (model, effort, choice_id) triple; ``None`` means "don't override".

    ``choice_id`` is opaque here on purpose: it names an entry in bot.py's
    MODEL_CATALOG (backend + model + required API key), which is a
    Telegram/deployment concern this stdlib-only module doesn't know about.
    It rides alongside model/effort so a /modello button pick (backend
    selection: Claude subscription vs Claude API key vs DeepSeek vs an
    OpenCode Zen model) persists the same way the legacy text tokens do, and
    survives a bot restart via to_json/from_json.
    """

    model: str | None = None
    effort: str | None = None
    choice_id: str | None = None

    def __bool__(self) -> bool:
        return bool(self.model or self.effort or self.choice_id)

    def merged_over(self, base: "Prefs") -> "Prefs":
        """Self wins field by field; ``base`` fills the gaps."""
        return Prefs(
            self.model or base.model,
            self.effort or base.effort,
            self.choice_id or base.choice_id,
        )

    def over_default(self, default: "Prefs") -> "Prefs":
        """Fill gaps from a project-level default (#65).

        Unlike ``merged_over`` the model selection is taken as a unit: model
        and choice_id travel together, so a card pinned to a catalog choice
        never inherits a stray plain model from the project (an ``opus``
        handed to an opencode backend would be nonsense). Effort is
        independent and fills field by field.
        """
        if self.model or self.choice_id:
            model, choice_id = self.model, self.choice_id
        else:
            model, choice_id = default.model, default.choice_id
        return Prefs(model, self.effort or default.effort, choice_id)

    def label(self) -> str:
        return f"{self.model or 'auto'}/{self.effort or 'auto'}"

    def to_json(self) -> list[str | None]:
        return [self.model, self.effort, self.choice_id]

    @classmethod
    def from_json(cls, v) -> "Prefs":
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            choice_id = v[2] if len(v) >= 3 else None
            return cls(restore_model(v[0]), normalize_effort(v[1]), choice_id)
        return cls()


# ``provider/model`` -- an OpenCode id picked from the live list (#68). It is
# not a Claude alias, so normalize_model would drop it and the card would lose
# its model at the first restart.
_PROVIDER_MODEL_RE = re.compile(r"^[\w.-]+/[\w.:@+-]+$")


def restore_model(token: str | None) -> str | None:
    """Model read back from state: a Claude alias, or a ``provider/model`` id
    kept verbatim."""
    if not token:
        return None
    alias = normalize_model(token)
    if alias:
        return alias
    raw = str(token).strip()
    return raw if _PROVIDER_MODEL_RE.match(raw) else None


def normalize_model(token: str | None) -> str | None:
    if not token:
        return None
    return MODEL_ALIASES.get(str(token).strip().lower())


def normalize_effort(token: str | None) -> str | None:
    if not token:
        return None
    return EFFORT_ALIASES.get(str(token).strip().lower())


def _classify(token: str) -> tuple[str | None, str | None]:
    """(model, effort) for one token — at most one of the two is set."""
    return normalize_model(token), normalize_effort(token)


def parse_prefix(text: str) -> tuple[Prefs, str]:
    """Split an optional ``model[/effort]:`` prefix off ``text``.

    Returns ``(prefs, remaining_text)``. The prefix is consumed only when every
    token is a known model/effort AND something is left to say — so
    ``"opus:"`` alone, or ``"nota: comprare il latte"``, stay untouched and
    reach claude verbatim.
    """
    if not text:
        return Prefs(), text
    m = _PREFIX_RE.match(text)
    if not m:
        return Prefs(), text
    tokens = [t for t in (m.group(1), m.group(2)) if t]
    model = effort = None
    for tok in tokens:
        mo, ef = _classify(tok)
        if mo and model is None:
            model = mo
        elif ef and effort is None:
            effort = ef
        else:
            # unknown word, or a second model / second effort -> not a prefix
            return Prefs(), text
    rest = text[m.end() :].strip()
    if not rest:  # "opus:" with no request is a question, not a directive
        return Prefs(), text
    return Prefs(model, effort), rest


def describe(
    prefs: Prefs, *, default_model: str | None = None, choice_label: str | None = None
) -> str:
    """Human-readable state for /modello and the turn footer.

    ``choice_label`` is the resolved MODEL_CATALOG label for ``prefs.choice_id``
    (looked up by the caller, which owns the catalog) -- shown in place of the
    raw model token when a card was set via the /modello buttons rather than
    the legacy ``opus max`` text tokens."""
    if prefs.choice_id and choice_label:
        model = choice_label
    else:
        model = prefs.model or (f"{default_model} (default)" if default_model else "auto")
    return f"modello <b>{model}</b> \u00b7 effort <b>{prefs.effort or 'auto'}</b>"


def known_tokens() -> str:
    """Compact help line listing what the parser accepts."""
    models = "opus · sonnet · haiku · fable"
    efforts = " · ".join(("low", "medium", "high", "xhigh", "max"))
    return f"modelli: {models}\neffort: {efforts}"
