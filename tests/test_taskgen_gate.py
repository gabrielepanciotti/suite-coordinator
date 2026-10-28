import pytest

from taskgen_config import Watcher
from taskgen_gate import passes_pre_gate

pytestmark = pytest.mark.asyncio


def _w(pre_gate, gate_cmd=None):
    return Watcher(
        key="k",
        node_key="n",
        cadence="cron:0 11 * * *",
        pre_gate=pre_gate,
        enabled=True,
        recipient_profile="owner",
        gate_cmd=gate_cmd,
    )


async def test_temporal_is_noop_true():
    assert await passes_pre_gate(_w("temporal"), workspace=None) is True


async def test_dati_exit0_true():
    assert await passes_pre_gate(_w("dati", "exit 0"), workspace=None) is True


async def test_dati_exit1_false():
    assert await passes_pre_gate(_w("dati", "exit 1"), workspace=None) is False


async def test_dati_no_cmd_false():
    assert await passes_pre_gate(_w("dati", None), workspace=None) is False
