"""Svuota un gruppo Telegram cancellando i messaggi per id (``/azzera``).

Un bot non puo' leggere lo storico di una chat, quindi non c'e' un elenco da
scorrere: si manda un messaggio sonda per conoscere l'ultimo id (gli id sono
progressivi per chat, topic compresi) e si cancella a ritroso da li' fino a 1, a
blocchi da 100 con ``delete_messages``, che salta in silenzio gli id inesistenti.
Funziona dove il bot e' admin con il permesso di cancellare; altrove i blocchi
falliscono e il gruppo viene abbandonato dopo pochi tentativi.

Modulo foglia: prende il bot da fuori, non importa nulla del progetto.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger("coordinator")

BATCH = 100                 # tetto di delete_messages
PAUSE_SEC = 1.0             # fra un blocco e l'altro, per i limiti di Telegram
GIVE_UP_AFTER = 3           # blocchi consecutivi falliti prima di lasciar perdere
PROBE_TEXT = "🧹"


@dataclass
class WipeResult:
    chat_id: int
    last_id: int = 0
    batches: int = 0
    failed_batches: int = 0
    gave_up: bool = False
    error: str | None = None


async def wipe_chat(
    bot,
    chat_id: int,
    *,
    pause: float = PAUSE_SEC,
    on_progress: Callable[[WipeResult], Awaitable[None]] | None = None,
) -> WipeResult:
    res = WipeResult(chat_id)
    try:
        probe = await bot.send_message(chat_id, PROBE_TEXT)
    except Exception as exc:  # il bot non e' nel gruppo, o non puo' scrivere
        res.error = f"sonda non inviata: {exc}"
        return res
    last = getattr(probe, "message_id", None)
    if not isinstance(last, int):
        res.error = "sonda senza id"
        return res
    res.last_id = last
    consecutive_fail = 0
    top = last
    while top >= 1:
        ids = list(range(top, max(top - BATCH, 0), -1))
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=ids)
            consecutive_fail = 0
        except Exception as exc:
            res.failed_batches += 1
            consecutive_fail += 1
            logger.warning("azzera %s: blocco %s..%s fallito: %s", chat_id, ids[-1], ids[0], exc)
            if consecutive_fail >= GIVE_UP_AFTER:
                res.gave_up = True
                res.error = str(exc)
                return res
        res.batches += 1
        top -= BATCH
        if on_progress and res.batches % 20 == 0:
            await on_progress(res)
        if pause:
            await asyncio.sleep(pause)
    return res
