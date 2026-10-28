"""action="direct_cli": una capability che gira come sottoprocesso, senza
passare da un agente/LLM (a differenza del default action="session", dove
[CMD:/key] e' solo un prompt che l'agente deve interpretare da solo — vedi
/invito su ComUnity, 15-20s di round trip per un comando che dovrebbe essere
istantaneo)."""
import projects

import bot

_PROJECT = projects.Project(
    key="zzz_fake_cli",
    label="Fake",
    emoji="🧪",
    host_path="/hosts/zzz_fake_cli",
    workspace="/workspaces/zzz_fake_cli",
)


class _FakeCap:
    key = "invito"
    cli = "docker exec comunity-backend python -m scripts.invite new"
    action = "direct_cli"


class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, msg):
        self.effective_message = msg


class _FakeProc:
    def __init__(self, out=b"", returncode=0):
        self._out = out
        self.returncode = returncode

    async def communicate(self):
        return self._out, b""


def _patch_shell(monkeypatch, proc, sink):
    async def fake_shell(cmd, **kwargs):
        sink.append((cmd, kwargs.get("cwd")))
        return proc

    monkeypatch.setattr(bot.asyncio, "create_subprocess_shell", fake_shell)


async def test_args_are_shell_quoted_not_interpolated_into_cli(monkeypatch):
    """Un argomento con spazi/apici non deve mai poter spezzare il comando."""
    sink = []
    _patch_shell(monkeypatch, _FakeProc(out=b"ok\n"), sink)
    msg = _FakeMessage()

    await bot._handle_direct_cli_capability(
        _FakeUpdate(msg), _PROJECT, _FakeCap(), "Marco; rm -rf /"
    )

    cmd, cwd = sink[0]
    assert cmd == "docker exec comunity-backend python -m scripts.invite new 'Marco; rm -rf /'"
    assert cwd == "/workspaces/zzz_fake_cli"


async def test_no_args_runs_bare_cli(monkeypatch):
    sink = []
    _patch_shell(monkeypatch, _FakeProc(out=b"ok\n"), sink)
    await bot._handle_direct_cli_capability(_FakeUpdate(_FakeMessage()), _PROJECT, _FakeCap(), "")
    assert sink[0][0] == _FakeCap.cli


async def test_stdout_reaches_the_chat(monkeypatch):
    _patch_shell(monkeypatch, _FakeProc(out=b"WJU7HO  [valid]  scade: 14/09\n"), [])
    msg = _FakeMessage()
    await bot._handle_direct_cli_capability(_FakeUpdate(msg), _PROJECT, _FakeCap(), "gab")
    assert "WJU7HO" in msg.replies[0]


async def test_only_the_code_is_wrapped_for_tap_to_copy(monkeypatch):
    """Solo il codice va in <code> (tap-to-copy) — non l'intera riga in
    <pre>, che su Telegram si e' rivelato non copiabile affatto e comunque
    avrebbe reso "toccabile" anche "[valid] scade: ...", non solo il codice."""
    _patch_shell(monkeypatch, _FakeProc(out=b"WJU7HO  [valid]  scade: 14/09\n"), [])
    msg = _FakeMessage()
    await bot._handle_direct_cli_capability(_FakeUpdate(msg), _PROJECT, _FakeCap(), "gab")
    assert msg.replies[0] == "<code>WJU7HO</code>  [valid]  scade: 14/09"


async def test_output_with_no_code_shaped_line_stays_plain(monkeypatch):
    """Una riga senza il separatore a doppio spazio (es. "(nessun output)")
    non deve ricevere un <code> spezzato a caso sul primo token."""
    _patch_shell(monkeypatch, _FakeProc(out=b"(nessun output)\n"), [])
    msg = _FakeMessage()
    await bot._handle_direct_cli_capability(_FakeUpdate(msg), _PROJECT, _FakeCap(), "")
    assert msg.replies[0] == "(nessun output)"


async def test_nonzero_exit_is_flagged(monkeypatch):
    _patch_shell(monkeypatch, _FakeProc(out=b"boom", returncode=1), [])
    msg = _FakeMessage()
    await bot._handle_direct_cli_capability(_FakeUpdate(msg), _PROJECT, _FakeCap(), "gab")
    assert "errore" in msg.replies[0].lower()
    assert "boom" in msg.replies[0]


async def test_timeout_replies_without_raising(monkeypatch):
    async def fake_shell(cmd, **kwargs):
        class _Hangs:
            async def communicate(self):
                raise TimeoutError()

        return _Hangs()

    monkeypatch.setattr(bot.asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(bot.asyncio, "wait_for", lambda coro, timeout: coro)
    msg = _FakeMessage()
    await bot._handle_direct_cli_capability(_FakeUpdate(msg), _PROJECT, _FakeCap(), "gab")
    assert "timeout" in msg.replies[0].lower()
