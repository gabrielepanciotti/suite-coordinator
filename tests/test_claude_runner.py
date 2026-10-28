"""claude_runner is the Telegram-independent claude -p spawner, importable headless."""

import asyncio
import inspect
import json


def test_exposes_run_claude_and_gate():
    import claude_runner

    assert inspect.iscoroutinefunction(claude_runner.run_claude)
    assert callable(claude_runner._claude_gate)
    # constants moved over with their env-driven defaults
    assert isinstance(claude_runner.CLAUDE_BIN, str)
    assert claude_runner.CLAUDE_MAX_CONCURRENCY >= 1
    # stream helpers moved into the module
    assert callable(claude_runner._parse_stream_line)
    assert callable(claude_runner._event_progress)
    assert callable(claude_runner._result_from_event)


def test_importing_runner_needs_no_telegram(monkeypatch):
    # No bot token, no telegram import required to use the runner.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    import importlib
    import claude_runner

    importlib.reload(claude_runner)
    assert claude_runner.run_claude is not None


def test_ai_backend_defaults_to_claude():
    import claude_runner

    # Must stay "claude" so a plain deploy (no AI_BACKEND set) behaves exactly
    # as before opencode existed -- this is the whole safety net for the switch.
    assert claude_runner.AI_BACKEND == "claude"
    assert claude_runner.OPENCODE_MODEL == "deepseek/deepseek-v4-flash"


async def test_run_claude_dispatches_to_claude_cli_by_default(monkeypatch):
    import claude_runner

    monkeypatch.setattr(claude_runner, "AI_BACKEND", "claude")
    called = {}

    async def fake_claude_cli(prompt, **kwargs):
        called["backend"] = "claude"
        return ("ok", "sid-1")

    async def fake_opencode(prompt, **kwargs):
        called["backend"] = "opencode"
        return ("ok", "sid-1")

    monkeypatch.setattr(claude_runner, "_run_claude_cli", fake_claude_cli)
    monkeypatch.setattr(claude_runner, "_run_opencode", fake_opencode)

    text, sid = await claude_runner.run_claude("hi", workspace="/w", persona=None)

    assert called["backend"] == "claude"
    assert (text, sid) == ("ok", "sid-1")


async def test_run_claude_dispatches_to_opencode_when_configured(monkeypatch):
    import claude_runner

    monkeypatch.setattr(claude_runner, "AI_BACKEND", "opencode")
    called = {}

    async def fake_claude_cli(prompt, **kwargs):
        called["backend"] = "claude"
        return ("ok", "sid-1")

    async def fake_opencode(prompt, **kwargs):
        called["backend"] = "opencode"
        return ("ok", "sid-1")

    monkeypatch.setattr(claude_runner, "_run_claude_cli", fake_claude_cli)
    monkeypatch.setattr(claude_runner, "_run_opencode", fake_opencode)

    await claude_runner.run_claude("hi", workspace="/w", persona=None)

    assert called["backend"] == "opencode"


def test_opencode_progress_ignores_text_and_step_bookkeeping():
    import claude_runner

    assert claude_runner._opencode_progress({"type": "text"}) is None
    assert claude_runner._opencode_progress({"type": "step_start"}) is None
    assert claude_runner._opencode_progress({"type": "step_finish"}) is None


def test_opencode_progress_labels_anything_else_as_a_tool():
    import claude_runner

    ev = {"type": "tool", "part": {"type": "bash"}}
    assert claude_runner._opencode_progress(ev) == "🔧 bash"
    # No part.type at all: falls back to the event's own type.
    assert claude_runner._opencode_progress({"type": "patch"}) == "🔧 patch"


# --- Transport failures & permission bypass (#29, #30) -----------------------


def test_is_transport_error_recognises_runner_failures_only():
    import claude_runner

    assert claude_runner.is_transport_error("❌ opencode exit=1\nboom") is True
    assert claude_runner.is_transport_error("❌ claude: error_during_execution") is True
    # A real reply -- even an unhappy one -- is an ANSWER, not a transport fault.
    assert claude_runner.is_transport_error("Non ci sono dati per oggi.") is False
    assert claude_runner.is_transport_error("(risposta vuota)") is False


class _FakeProc:
    """Minimal asyncio subprocess double: replays `lines` then EOF."""

    def __init__(self, lines, returncode=0, stderr=b""):
        self._lines = list(lines)
        self.returncode = None
        self._final_rc = returncode
        self.stdout = self
        self.stderr = _FakeStderr(stderr)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        self.returncode = self._final_rc
        return b""

    async def wait(self):
        self.returncode = self._final_rc
        return self._final_rc

    def kill(self):
        self.returncode = -9


class _FakeStderr:
    def __init__(self, data):
        self._data = data

    async def read(self):
        return self._data


def _patch_spawn(monkeypatch, proc, argv_sink):
    import claude_runner

    async def fake_exec(*argv, **kwargs):
        argv_sink.append(list(argv))
        return proc

    monkeypatch.setattr(claude_runner.asyncio, "create_subprocess_exec", fake_exec)


async def test_opencode_cmd_carries_dir_and_auto(monkeypatch):
    """--dir pins the project, --auto stops a headless run blocking on a
    permission question nobody can answer (#30)."""
    import claude_runner

    argv = []
    _patch_spawn(
        monkeypatch,
        _FakeProc(
            [
                b'{"type":"step_start","sessionID":"ses_1"}\n',
                b'{"type":"text","part":{"id":"p1","text":"ciao"}}\n',
                b'{"type":"step_finish"}\n',
            ]
        ),
        argv,
    )

    text, sid = await claude_runner._run_opencode(
        "hi", workspace="/workspaces/homeResearcher", persona=None
    )

    assert text == "ciao"
    assert sid == "ses_1"
    cmd = argv[0]
    assert "--auto" in cmd
    assert cmd[cmd.index("--dir") + 1] == "/workspaces/homeResearcher"


async def test_opencode_stream_without_step_finish_is_a_transport_error(monkeypatch):
    """The permission-prompt death: opencode exits 0 with no reply and no
    step-finish, and used to leave no trace at all in our logs (#30)."""
    import claude_runner

    argv = []
    _patch_spawn(
        monkeypatch,
        _FakeProc([b'{"type":"step_start","sessionID":"ses_2"}\n']),
        argv,
    )

    text, sid = await claude_runner._run_opencode("hi", workspace="/w", persona=None)

    assert claude_runner.is_transport_error(text)
    assert "step-finish" in text
    assert sid == "ses_2"


class _HangingProc(_FakeProc):
    """Replays `lines`, then hangs — the shape of a run stuck on a permission
    question: the process is alive and silent until the idle watchdog fires."""

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        await asyncio.sleep(30)
        return b""


async def test_opencode_resumed_session_that_hangs_is_a_transport_error(monkeypatch):
    """A resumed run that goes idle without a single token never reached the
    model: it is a poisoned session, not a silent agent (#30).

    Typed as a transport error so run_claude retries clean inside the SAME
    call, instead of spending one of the caller's two answer attempts on a
    session that can only hang again.
    """
    import claude_runner

    monkeypatch.setattr(claude_runner, "CLAUDE_IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(claude_runner, "CLAUDE_FIRST_TOKEN_TIMEOUT", 0.05)
    argv = []
    _patch_spawn(monkeypatch, _HangingProc([]), argv)

    text, sid = await claude_runner._run_opencode(
        "hi", workspace="/w", persona=None, resume_id="ses_poisoned"
    )

    assert claude_runner.is_transport_error(text)
    assert "ses_poisoned" in text
    assert sid is None  # nothing worth resuming: the caller must start over


async def test_a_silent_resume_dies_on_the_short_watchdog(monkeypatch):
    """Un resume ancora muto non aspetta l'idle timeout intero.

    Il 2026-09-06 un turno Telegram ha impiegato 21 minuti, 5 dei quali erano
    esattamente questa attesa: una sessione morta che nessuno interrompeva
    prima dei 300s. Il silenzio PRIMA del primo token e' l'unico caso in cui
    non e' lavoro in corso, quindi ha una sveglia sua, molto piu' corta.
    """
    import claude_runner

    monkeypatch.setattr(claude_runner, "CLAUDE_IDLE_TIMEOUT", 30)
    monkeypatch.setattr(claude_runner, "CLAUDE_FIRST_TOKEN_TIMEOUT", 0.05)
    _patch_spawn(monkeypatch, _HangingProc([]), [])

    inizio = asyncio.get_running_loop().time()
    text, _ = await claude_runner._run_opencode(
        "hi", workspace="/w", persona=None, resume_id="ses_muta"
    )
    assert asyncio.get_running_loop().time() - inizio < 5  # non ha atteso i 30
    assert claude_runner.is_transport_error(text)


async def test_once_it_speaks_the_long_idle_watchdog_takes_over(monkeypatch):
    """La sveglia corta vale SOLO fino al primo token: dopo, un lavoro lungo e
    sano (Playwright, ricerca web) non deve essere tagliato a 45s."""
    import claude_runner

    monkeypatch.setattr(claude_runner, "CLAUDE_IDLE_TIMEOUT", 0.4)
    monkeypatch.setattr(claude_runner, "CLAUDE_FIRST_TOKEN_TIMEOUT", 0.05)
    # Un token arriva davvero: da quel momento la finestra e' quella lunga.
    proc = _HangingProc(
        [b'{"type":"text","part":{"id":"p1","text":"sto lavorando"}}\n']
    )
    _patch_spawn(monkeypatch, proc, [])

    inizio = asyncio.get_running_loop().time()
    text, _ = await claude_runner._run_opencode(
        "hi", workspace="/w", persona=None, resume_id="ses_viva"
    )
    # Ha aspettato la finestra lunga (0.4s), non la corta (0.05s).
    assert asyncio.get_running_loop().time() - inizio >= 0.4
    # E si e' fermato come un blocco normale, non come sessione avvelenata: chi
    # ha gia' parlato non e' un resume da buttare, e' un lavoro che si e' piantato.
    assert not claude_runner.is_transport_error(text)
    assert "Nessuna attività" in text


async def test_opencode_fresh_session_that_hangs_stays_a_plain_timeout(monkeypatch):
    """Only a RESUME is downgraded. A fresh run that hangs is a genuine stall:
    retrying it identically would just hang again, so it keeps the ⏱ message
    and its session stays resumable by hand."""
    import claude_runner

    monkeypatch.setattr(claude_runner, "CLAUDE_IDLE_TIMEOUT", 0.05)
    argv = []
    _patch_spawn(
        monkeypatch, _HangingProc([b'{"type":"step_start","sessionID":"ses_3"}\n']), argv
    )

    text, sid = await claude_runner._run_opencode("hi", workspace="/w", persona=None)

    assert not claude_runner.is_transport_error(text)
    assert text.startswith("⏱")
    assert sid == "ses_3"


async def test_opencode_missing_step_finish_still_returns_the_text(monkeypatch):
    """A reply already produced is a reply: warn, but never discard it."""
    import claude_runner

    argv = []
    _patch_spawn(
        monkeypatch,
        _FakeProc([b'{"type":"text","part":{"id":"p1","text":"parziale"}}\n']),
        argv,
    )

    text, _sid = await claude_runner._run_opencode("hi", workspace="/w", persona=None)

    assert text == "parziale"


async def test_run_claude_degrades_a_rejected_resume_to_a_fresh_session(monkeypatch):
    """A resume_id the CLI refuses dies in ~2s without reaching a model. It must
    fall back to a new session inside the SAME call, not burn a caller's retry (#29)."""
    import claude_runner

    monkeypatch.setattr(claude_runner, "AI_BACKEND", "claude")
    seen = []

    async def fake_cli(prompt, *, resume_id=None, **kwargs):
        seen.append(resume_id)
        if resume_id:
            return ("❌ claude exit=1\nsession not found", resume_id)
        return ("risposta", "sid-new")

    monkeypatch.setattr(claude_runner, "_run_claude_cli", fake_cli)

    text, sid = await claude_runner.run_claude(
        "hi", workspace="/w", persona=None, resume_id="ses_dead"
    )

    assert seen == ["ses_dead", None]
    assert (text, sid) == ("risposta", "sid-new")


async def test_run_claude_does_not_retry_a_fresh_run_that_failed(monkeypatch):
    """Without a resume_id there is nothing to degrade to: one call, one run."""
    import claude_runner

    monkeypatch.setattr(claude_runner, "AI_BACKEND", "claude")
    calls = []

    async def fake_cli(prompt, *, resume_id=None, **kwargs):
        calls.append(resume_id)
        return ("❌ claude exit=1", None)

    monkeypatch.setattr(claude_runner, "_run_claude_cli", fake_cli)

    await claude_runner.run_claude("hi", workspace="/w", persona=None)

    assert calls == [None]


# --- resume failure classification (2026-10-09) ------------------------------
def test_classify_resume_failure_kinds():
    import claude_runner as cr

    assert cr.classify_resume_failure("❌ claude exit=1\nsession not found") == "session"
    assert cr.classify_resume_failure(
        "❌ opencode: sessione ses_x muta per 60s (probabile permesso)") == "session"
    assert cr.classify_resume_failure(
        "❌ opencode: Unexpected server error. Check server logs for details.") == "transient"
    assert cr.classify_resume_failure("❌ opencode: 503 Service Unavailable") == "transient"
    assert cr.classify_resume_failure("❌ opencode: HTTP 429") == "transient"
    assert cr.classify_resume_failure("❌ opencode: Rate limit exceeded") == "transient"
    assert cr.classify_resume_failure("❌ opencode exit=1\nboom") == "other"


async def test_transient_resume_failure_retries_same_session_once(monkeypatch):
    import claude_runner

    monkeypatch.setattr(claude_runner, "AI_BACKEND", "opencode")
    monkeypatch.setattr(claude_runner, "RESUME_TRANSIENT_BACKOFF_SEC", 0)
    seen = []

    async def fake(prompt, *, resume_id=None, **kwargs):
        seen.append(resume_id)
        if len(seen) == 1:
            return ("❌ opencode: Unexpected server error.", resume_id)
        return ("ok", resume_id)

    monkeypatch.setattr(claude_runner, "_run_opencode", fake)
    text, sid = await claude_runner.run_claude(
        "hi", workspace="/w", persona=None, resume_id="ses_live")
    assert seen == ["ses_live", "ses_live"]
    assert (text, sid) == ("ok", "ses_live")


async def test_transient_failure_twice_falls_back_to_fresh_once(monkeypatch):
    import claude_runner

    monkeypatch.setattr(claude_runner, "AI_BACKEND", "opencode")
    monkeypatch.setattr(claude_runner, "RESUME_TRANSIENT_BACKOFF_SEC", 0)
    seen = []

    async def fake(prompt, *, resume_id=None, **kwargs):
        seen.append(resume_id)
        return ("❌ opencode: Unexpected server error.", None)

    monkeypatch.setattr(claude_runner, "_run_opencode", fake)
    text, sid = await claude_runner.run_claude(
        "hi", workspace="/w", persona=None, resume_id="ses_live")
    # same session once more, then ONE fresh run: a broken session can't loop
    assert seen == ["ses_live", "ses_live", None]
    assert claude_runner.is_transport_error(text)


def test_classifier_ignores_bare_numbers():
    import claude_runner as cr

    assert cr.classify_resume_failure("❌ opencode exit=1\nat line 503 of x.js") == "other"
    assert cr.classify_resume_failure("❌ opencode exit=1\npid 429 died") == "other"
    assert cr.classify_resume_failure("❌ opencode: status: 502") == "transient"


async def test_on_session_fires_at_first_event_once(monkeypatch):
    """TicketManager#10: the caller learns the session id at the run's first
    event (init), not only at the end -- and once, not per event."""
    import claude_runner

    seen = []
    _patch_spawn(
        monkeypatch,
        _FakeProc(
            [
                b'{"type":"system","subtype":"init","session_id":"S1"}\n',
                b'{"type":"assistant","session_id":"S1"}\n',
                b'{"type":"result","result":"ok","session_id":"S1"}\n',
            ]
        ),
        [],
    )
    text, sid = await claude_runner._run_claude_cli(
        "hi", workspace="/w", persona=None, on_session=seen.append
    )
    assert (text, sid) == ("ok", "S1")
    assert seen == ["S1"]


async def test_on_session_failure_does_not_break_the_run(monkeypatch):
    import claude_runner

    def boom(_sid):
        raise RuntimeError("disk full")

    _patch_spawn(
        monkeypatch,
        _FakeProc([b'{"type":"result","result":"ok","session_id":"S1"}\n']),
        [],
    )
    text, _ = await claude_runner._run_claude_cli(
        "hi", workspace="/w", persona=None, on_session=boom
    )
    assert text == "ok"


def test_opencode_ignora_alias_claude_senza_provider():
    # `sonnet` non e' un modello opencode: va usato OPENCODE_MODEL, non l'alias.
    import claude_runner as cr
    src = open(cr.__file__, encoding="utf-8").read()
    assert 'model if model and "/" in model else OPENCODE_MODEL' in src


async def test_final_transient_error_gets_a_plain_hint(monkeypatch):
    import claude_runner

    monkeypatch.setattr(claude_runner, "AI_BACKEND", "opencode")
    monkeypatch.setattr(claude_runner, "RESUME_TRANSIENT_BACKOFF_SEC", 0)

    async def fake(prompt, *, resume_id=None, **kwargs):
        return ("❌ opencode: Unexpected server error.", "ses_x")

    monkeypatch.setattr(claude_runner, "_run_opencode", fake)
    text, sid = await claude_runner.run_claude(
        "hi", workspace="/w", persona=None, resume_id="ses_x")
    assert claude_runner.is_transport_error(text)
    assert claude_runner.TRANSIENT_HINT in text
    assert text.count(claude_runner.TRANSIENT_HINT) == 1
    assert sid == "ses_x"


async def test_non_transient_error_is_left_alone(monkeypatch):
    import claude_runner

    monkeypatch.setattr(claude_runner, "AI_BACKEND", "opencode")

    async def fake(prompt, *, resume_id=None, **kwargs):
        return ("❌ opencode exit=1\nboom", None)

    monkeypatch.setattr(claude_runner, "_run_opencode", fake)
    text, _ = await claude_runner.run_claude("hi", workspace="/w", persona=None)
    assert claude_runner.TRANSIENT_HINT not in text


_TOOL_USE = (
    b'{"type":"assistant","message":{"content":'
    b'[{"type":"tool_use","id":"t1","name":"Bash"}]}}\n'
)
_TOOL_RESULT = (
    b'{"type":"user","message":{"content":'
    b'[{"type":"tool_result","tool_use_id":"t1"}]}}\n'
)


class _SlowThenResult(_FakeProc):
    """Replays `lines`, resta muto `pausa` secondi (un tool lento), poi il result."""

    def __init__(self, lines, pausa):
        super().__init__(lines)
        self._pausa = pausa
        self._fatto = False

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if not self._fatto:
            self._fatto = True
            await asyncio.sleep(self._pausa)
            return b'{"type":"result","result":"fatto","session_id":"S1"}\n'
        return await super().readline()


async def test_a_tool_in_flight_gets_the_long_idle_window(monkeypatch):
    """Un tool ancora in corso (tool_use senza tool_result) non muore alla
    finestra d'inattivita' normale: build/test lunghi non emettono eventi
    (ComUnity#189)."""
    import claude_runner

    monkeypatch.setattr(claude_runner, "CLAUDE_IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(claude_runner, "CLAUDE_TOOL_IDLE_TIMEOUT", 2)
    _patch_spawn(monkeypatch, _SlowThenResult([_TOOL_USE], 0.3), [])

    text, _ = await claude_runner._run_claude_cli("hi", workspace="/w", persona=None)
    assert text == "fatto"


async def test_silence_without_a_tool_still_dies_on_the_plain_window(monkeypatch):
    """Senza tool in volo (o dopo il suo tool_result) il silenzio e' un blocco."""
    import claude_runner

    monkeypatch.setattr(claude_runner, "CLAUDE_IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(claude_runner, "CLAUDE_TOOL_IDLE_TIMEOUT", 2)
    _patch_spawn(monkeypatch, _SlowThenResult([_TOOL_USE, _TOOL_RESULT], 0.5), [])

    text, _ = await claude_runner._run_claude_cli("hi", workspace="/w", persona=None)
    assert "Nessuna attività" in text


async def test_a_tool_that_never_returns_still_dies_on_the_tool_window(monkeypatch):
    """Il limite di sicurezza resta: un tool che non torna muore comunque."""
    import claude_runner

    monkeypatch.setattr(claude_runner, "CLAUDE_IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(claude_runner, "CLAUDE_TOOL_IDLE_TIMEOUT", 0.2)
    _patch_spawn(monkeypatch, _SlowThenResult([_TOOL_USE], 5), [])

    text, _ = await claude_runner._run_claude_cli("hi", workspace="/w", persona=None)
    assert "Nessuna attività per 0.2s" in text
