"""Riconosce chi si presenta offrendo una competenza (legale, branding, ...)
e dice a quale settore del team va instradata. Puro e deterministico: niente
rete, niente LLM. Chi lo usa decide cosa farne (oggi l'estrattore
dell'auto-analisi lo annota sull'episodio; ComUnity#178)."""
from __future__ import annotations

import re

# Verbi/forme con cui qualcuno si propone o dice "io mi occupo di".
_OFFERTA = re.compile(
    r"\b(sar[àa]\s+il\s+nostro|saremo\s+il\s+vostro|sar[òo]\s+il\s+vostro|"
    r"mi\s+occup\w+\s+di|(?:vi\s+)?offr\w+|mi\s+metto\s+a\s+disposizione|"
    r"sono\s+(?:un\s+|una\s+|il\s+|la\s+)?(?:\w+\s+)?"
    r"(?:avvocat\w+|legal\w+|brand\w+|grafic\w+|commercialist\w+|developer|sviluppator\w+)|"
    r"supporto\s+(?:in|per|su|nel|nell)\w*)",
    re.IGNORECASE,
)

# (settore, parole chiave). L'ordine è la priorità a parità di match.
_SETTORI: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("settore-azienda", ("legal", "avvocat", "contratt", "privacy", "gdpr",
                         "commercialist", "fiscal", "amministrativ")),
    ("settore-marketing", ("branding", "brand", "marketing", "grafic",
                           "social", "comunicazion", "logo")),
    ("settore-dev", ("sviluppat", "developer", "programm", "software", "sito web")),
)


def settore_per_competenza(testo: str) -> str | None:
    """Il settore pertinente se `testo` è un'offerta di competenza, altrimenti None."""
    t = (testo or "").strip()
    if not t or not _OFFERTA.search(t):
        return None
    low = t.lower()
    for settore, chiavi in _SETTORI:
        if any(k in low for k in chiavi):
            return settore
    return None
