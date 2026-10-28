"""Tests for atomic_io.replace_with_retry.

The Windows lock this guards against is timing-dependent and cannot be produced
on demand, so os.replace is faked: a counter decides how many attempts raise
PermissionError before one succeeds. That is exactly the shape of a real-time
antivirus scan holding the file for a few milliseconds.

time.sleep is stubbed everywhere so the backoff is asserted, not waited on.
"""

from __future__ import annotations

import pytest

import atomic_io


def _fake_replace(fail_times: int, calls: list):
    """os.replace that raises PermissionError for its first ``fail_times`` calls."""

    def replace(src, dst):
        calls.append((src, dst))
        if len(calls) <= fail_times:
            raise PermissionError(5, "Accesso negato")

    return replace


@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff durations instead of sleeping them."""
    slept: list[float] = []
    monkeypatch.setattr(atomic_io.time, "sleep", slept.append)
    return slept


def test_succeeds_first_try_without_sleeping(monkeypatch, no_sleep):
    calls: list = []
    monkeypatch.setattr(atomic_io.os, "replace", _fake_replace(0, calls))
    atomic_io.replace_with_retry("a.tmp", "a.json")
    assert calls == [("a.tmp", "a.json")]
    assert no_sleep == []  # the happy path must not cost a single millisecond


def test_retries_until_the_lock_clears(monkeypatch, no_sleep):
    calls: list = []
    monkeypatch.setattr(atomic_io.os, "replace", _fake_replace(2, calls))
    atomic_io.replace_with_retry("a.tmp", "a.json")
    assert len(calls) == 3  # two locked attempts, third wins


def test_backoff_doubles(monkeypatch, no_sleep):
    calls: list = []
    monkeypatch.setattr(atomic_io.os, "replace", _fake_replace(3, calls))
    atomic_io.replace_with_retry("a.tmp", "a.json")
    assert no_sleep == [0.01, 0.02, 0.04]


def test_reraises_when_lock_never_clears(monkeypatch, no_sleep):
    """A permanent lock (read-only file, wrong ACL) must not pass silently."""
    calls: list = []
    monkeypatch.setattr(atomic_io.os, "replace", _fake_replace(99, calls))
    with pytest.raises(PermissionError):
        atomic_io.replace_with_retry("a.tmp", "a.json")
    assert len(calls) == atomic_io._ATTEMPTS  # tried every attempt, then gave up


def test_other_oserrors_are_not_retried(monkeypatch, no_sleep):
    """Only PermissionError is transient: a missing source is final."""
    calls: list = []

    def replace(src, dst):
        calls.append((src, dst))
        raise FileNotFoundError(2, "No such file")

    monkeypatch.setattr(atomic_io.os, "replace", replace)
    with pytest.raises(FileNotFoundError):
        atomic_io.replace_with_retry("a.tmp", "a.json")
    assert len(calls) == 1  # no pointless backoff on an error that cannot clear


def test_really_replaces_on_disk(tmp_path):
    """End-to-end against the real filesystem, no fakes."""
    src, dst = tmp_path / "a.tmp", tmp_path / "a.json"
    src.write_text("nuovo", encoding="utf-8")
    dst.write_text("vecchio", encoding="utf-8")
    atomic_io.replace_with_retry(str(src), str(dst))
    assert dst.read_text(encoding="utf-8") == "nuovo"
    assert not src.exists()
