import asyncio

import chat_wipe


class Probe:
    def __init__(self, mid):
        self.message_id = mid


class FakeBot:
    def __init__(self, last, fail_from=None):
        self.last, self.fail_from = last, fail_from
        self.deleted: list[int] = []
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return Probe(self.last)

    async def delete_messages(self, chat_id, message_ids):
        if self.fail_from is not None:
            raise RuntimeError("Message can't be deleted")
        self.deleted += list(message_ids)
        return True


def test_cancella_da_ultimo_id_a_uno():
    b = FakeBot(250)
    r = asyncio.run(chat_wipe.wipe_chat(b, -100, pause=0))
    assert sorted(b.deleted) == list(range(1, 251))   # sonda compresa
    assert r.batches == 3 and not r.gave_up and r.error is None


def test_si_arrende_dopo_tre_blocchi_falliti():
    b = FakeBot(1000, fail_from=0)
    r = asyncio.run(chat_wipe.wipe_chat(b, -100, pause=0))
    assert r.gave_up and r.failed_batches == 3 and b.deleted == []


def test_sonda_non_inviata():
    class Muto(FakeBot):
        async def send_message(self, *a, **k):
            raise RuntimeError("Forbidden")

    r = asyncio.run(chat_wipe.wipe_chat(Muto(1), -100, pause=0))
    assert r.error and r.batches == 0


def test_target_salta_comunity_e_bot_dedicati(monkeypatch):
    from types import SimpleNamespace as NS

    import bot
    import manifest

    default = bot.bot_registry.DEFAULT_BOT
    progetti = [NS(key="Health"), NS(key="comunity"), NS(key="casa"), NS(key="sched"), NS(key="vuoto")]
    gruppi = {
        "Health": NS(group_chat_id=-1, bot=default),
        "comunity": NS(group_chat_id=-2, bot=default),
        "casa": NS(group_chat_id=-2, bot=default),  # stesso gruppo di comunity: si tiene
        "sched": NS(group_chat_id=-3, bot="altro"),
        "vuoto": NS(group_chat_id=0, bot=default),
    }
    monkeypatch.setattr(bot, "list_projects", lambda: progetti)
    monkeypatch.setattr(manifest, "telegram_of", lambda p: gruppi[p.key])
    targets, skipped = bot._wipe_targets()
    assert targets == [(-1, ["Health"])]
    assert len(skipped) == 2
