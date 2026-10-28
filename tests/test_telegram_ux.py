"""Tests for the Telegram UX features ported from the official plugin:
reactions / in-place finish / outbox attachments (bot.py) and the persistent
live-session fast path (live_session.py).

Async coroutines are driven with ``asyncio.run`` inside plain sync test
functions, so the suite needs no pytest-asyncio config.
"""

import asyncio
import json
import os

import pytest

import bot
import live_session
from live_session import LiveSessionManager, LiveSessionUnavailable


# --- fakes -------------------------------------------------------------------
class _FakeStream:
    """Minimal stand-in for an asyncio StreamReader."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""

    async def read(self):  # stderr drain
        return b""


class _FakeStdin:
    def __init__(self):
        self.buf = b""
        self._closing = False

    def write(self, b):
        self.buf += b

    async def drain(self):
        pass

    def is_closing(self):
        return self._closing

    def close(self):
        self._closing = True


class _FakeProc:
    def __init__(self, stdout_lines, returncode=None):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream([])
        self.returncode = returncode

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class _FakeMsg:
    def __init__(self):
        self.edited = None
        self.edited_markup = None
        self.deleted = False

    async def edit_text(self, text, **kw):
        self.edited = text
        self.edited_markup = kw.get("reply_markup")

    async def delete(self):
        self.deleted = True


class _FakeBot:
    def __init__(self):
        self.photos = []
        self.docs = []

    async def send_photo(self, chat_id, fh, message_thread_id=None):
        self.photos.append(getattr(fh, "name", ""))

    async def send_document(self, chat_id, fh, filename=None, message_thread_id=None):
        self.docs.append(filename)


def _mgr(monkeypatch, proc, calls):
    async def fake_exec(*a, **k):
        calls["n"] += 1
        return proc

    monkeypatch.setattr(live_session.asyncio, "create_subprocess_exec", fake_exec)
    return LiveSessionManager(
        claude_bin="claude",
        model="sonnet",
        setting_sources="project,local",
        strict_mcp=True,
        idle_ttl_sec=100,
        max_timeout_sec=30,
        idle_event_timeout_sec=30,
        stream_limit=1024,
        parse_stream_line=lambda s: json.loads(s),
        result_from_event=lambda ev: ev.get("result", ""),
        event_progress=lambda ev: ev.get("label"),
        gate=None,
        logger=None,
    )


# --- live_session ------------------------------------------------------------
def test_encode_turn_shape():
    line = live_session._encode_turn("hello")
    assert line.endswith(b"\n")
    obj = json.loads(line)
    assert obj["type"] == "user"
    assert obj["message"]["content"][0]["text"] == "hello"


def test_live_turn_success(monkeypatch):
    async def _run():
        proc = _FakeProc(
            [
                b'{"type":"system","session_id":"sess-1"}\n',
                b'{"type":"result","result":"ciao","session_id":"sess-1"}\n',
            ]
        )
        calls = {"n": 0}
        mgr = _mgr(monkeypatch, proc, calls)
        reply, sid = await mgr.run_turn(
            ("c", 0), prompt="hi", workspace=".", persona="P", resume_id=None
        )
        assert reply == "ciao"
        assert sid == "sess-1"
        assert calls["n"] == 1
        assert b'"type": "user"' in proc.stdin.buf
        await mgr.close()

    asyncio.run(_run())


def test_live_eof_falls_back(monkeypatch):
    async def _run():
        proc = _FakeProc([])  # readline -> EOF immediately
        calls = {"n": 0}
        mgr = _mgr(monkeypatch, proc, calls)
        with pytest.raises(LiveSessionUnavailable):
            await mgr.run_turn(
                ("c", 0), prompt="hi", workspace=".", persona=None, resume_id=None
            )
        assert ("c", 0) not in mgr._live  # dead key dropped

    asyncio.run(_run())


def test_live_reuses_warm_process(monkeypatch):
    async def _run():
        proc = _FakeProc([b'{"type":"result","result":"t1","session_id":"s"}\n'])
        calls = {"n": 0}
        mgr = _mgr(monkeypatch, proc, calls)
        key = ("c", 0)
        r1, _ = await mgr.run_turn(
            key, prompt="a", workspace=".", persona=None, resume_id=None
        )
        assert r1 == "t1"
        proc.stdout._lines.append(
            b'{"type":"result","result":"t2","session_id":"s"}\n'
        )
        r2, _ = await mgr.run_turn(
            key, prompt="b", workspace=".", persona=None, resume_id=None
        )
        assert r2 == "t2"
        assert calls["n"] == 1  # spawned once, reused on turn 2
        await mgr.close()

    asyncio.run(_run())


# --- bot.py UX helpers -------------------------------------------------------
def test_finish_turn_edits_in_place_for_short_reply():
    async def _run():
        msg = _FakeMsg()
        sent = []

        async def send_long():
            sent.append(True)

        await bot._finish_turn(msg, "risposta breve", send_long)
        assert msg.edited == "risposta breve"
        assert not msg.deleted
        assert not sent  # single chunk -> edited, not resent

    asyncio.run(_run())


def test_finish_turn_resends_for_long_reply():
    async def _run():
        msg = _FakeMsg()
        sent = []

        async def send_long():
            sent.append(True)

        await bot._finish_turn(msg, "x" * 8000, send_long)
        assert msg.deleted
        assert sent  # multi-chunk -> placeholder deleted + streamed

    asyncio.run(_run())


def test_finish_turn_passes_reply_markup_when_editing_in_place():
    async def _run():
        msg = _FakeMsg()
        kb = object()

        async def send_long():
            pass

        await bot._finish_turn(msg, "risposta breve", send_long, reply_markup=kb)
        assert msg.edited_markup is kb

    asyncio.run(_run())


def test_send_html_chunks_markup_only_on_last_chunk():
    async def _run():
        sent = []

        class _Target:
            async def reply_text(self, text, **kw):
                sent.append((text, kw.get("reply_markup")))

        kb = object()
        await bot._send_html_chunks(_Target(), "x" * 8000, reply_markup=kb)
        assert len(sent) > 1
        assert all(markup is None for _, markup in sent[:-1])
        assert sent[-1][1] is kb

    asyncio.run(_run())


def test_send_outbox_routes_images_and_archives(monkeypatch, tmp_path):
    async def _run():
        monkeypatch.setattr(bot, "_OUTBOX_MAX_BYTES", 10)
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "a.txt").write_bytes(b"hi")  # 2 bytes -> document
        (outbox / "b.png").write_bytes(b"img")  # 3 bytes -> photo
        (outbox / "big.bin").write_bytes(b"x" * 20)  # over cap -> skipped

        fbot = _FakeBot()
        await bot._send_outbox(fbot, 1, None, str(tmp_path))

        assert any("b.png" in p for p in fbot.photos)
        assert "a.txt" in fbot.docs
        sent = outbox / ".sent"
        assert (sent / "a.txt").exists() and not (outbox / "a.txt").exists()
        assert (sent / "b.png").exists() and not (outbox / "b.png").exists()
        # oversized file stays put, untouched
        assert (outbox / "big.bin").exists()

    asyncio.run(_run())


def test_send_outbox_noop_without_dir(tmp_path):
    async def _run():
        fbot = _FakeBot()
        await bot._send_outbox(fbot, 1, None, str(tmp_path))  # no outbox/ -> no-op
        assert not fbot.photos and not fbot.docs

    asyncio.run(_run())


def test_live_lru_cap_evicts_oldest(monkeypatch):
    async def _run():
        procA = _FakeProc([b'{"type":"result","result":"A","session_id":"a"}\n'])
        procB = _FakeProc([b'{"type":"result","result":"B","session_id":"b"}\n'])
        queue = [procA, procB]

        async def fake_exec(*a, **k):
            return queue.pop(0)

        monkeypatch.setattr(live_session.asyncio, "create_subprocess_exec", fake_exec)
        mgr = LiveSessionManager(
            claude_bin="claude",
            model=None,
            setting_sources=None,
            strict_mcp=False,
            idle_ttl_sec=100,
            max_timeout_sec=30,
            idle_event_timeout_sec=30,
            stream_limit=1024,
            max_live=1,
            parse_stream_line=lambda s: json.loads(s),
            result_from_event=lambda ev: ev.get("result", ""),
            event_progress=lambda ev: ev.get("label"),
            gate=None,
            logger=None,
        )
        rA, _ = await mgr.run_turn(
            ("a", 0), prompt="x", workspace=".", persona=None, resume_id=None
        )
        assert rA == "A"
        rB, _ = await mgr.run_turn(
            ("b", 0), prompt="y", workspace=".", persona=None, resume_id=None
        )
        assert rB == "B"
        assert ("b", 0) in mgr._live  # cap=1: only the newest survives
        assert ("a", 0) not in mgr._live  # LRU evicted
        await asyncio.sleep(0.05)  # let the background _kill task run
        assert procA.returncode is not None  # A was actually killed
        await mgr.close()

    asyncio.run(_run())
