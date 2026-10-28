from hub_config import HubConfig


def _cfg(**kw):
    base = dict(hub_url="u", api_keys={"t": "k"}, telegram_agent="telegram-owner",
                telegram_chat_id=1, local_agents={})
    base.update(kw)
    return HubConfig(**base)


def test_prod_agent_defaults_to_prod_device():
    cfg = _cfg(prod_device="minipc")
    assert cfg.prod_agent("scheduler") == "scheduler-minipc"


def test_prod_agent_explicit_override_wins():
    cfg = _cfg(prod_device="minipc", prod_dispatch={"scheduler": "scheduler-cloud"})
    assert cfg.prod_agent("scheduler") == "scheduler-cloud"


def test_prod_agents_splits_multiple_targets():
    # Network gira su entrambi i prod: istanza privata sul box interno, pubblica
    # su quello di frontiera. Raggiungerne una sola lascerebbe l'altra indietro senza errori.
    cfg = _cfg(prod_dispatch={"network": "network-prod-private|network-prod-public"})
    assert cfg.prod_agents("network") == ("network-prod-private", "network-prod-public")
    assert cfg.prod_agent("network") == "network-prod-private"


def test_prod_agents_single_target_and_default():
    cfg = _cfg(prod_device="prod", prod_dispatch={"segnalatori": "portale-prod"})
    assert cfg.prod_agents("segnalatori") == ("portale-prod",)
    assert cfg.prod_agents("offerte") == ("offerte-prod",)


def test_dev_agent_bare_when_no_dev_device():
    # Legacy shape: a device whose dev agents carry no suffix keeps working.
    cfg = _cfg()
    assert cfg.dev_agent("scheduler") == "scheduler"


def test_dev_agent_uses_dev_device_suffix():
    # Regression 2026-07-31: with HUB_DEV_DEVICE set, dev_agent must be symmetric
    # to prod_agent. Returning the bare key addressed an agent that exists nowhere
    # on the bus, and every /pipeline stalled at its implement stage in silence.
    cfg = _cfg(dev_device="dev-linux")
    assert cfg.dev_agent("offerte") == "offerte-dev-linux"


def test_dev_agent_explicit_override_wins():
    cfg = _cfg(dev_device="dev-linux", dev_dispatch={"segnalatori": "portale-dev-linux"})
    assert cfg.dev_agent("segnalatori") == "portale-dev-linux"
    assert cfg.dev_agent("offerte") == "offerte-dev-linux"
