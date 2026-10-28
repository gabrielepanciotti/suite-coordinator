"""local_defaults: env > locale file > neutral fallback."""

from __future__ import annotations

import json

import pytest

import local_defaults


@pytest.fixture
def locale_file(tmp_path, monkeypatch):
    def write(data) -> None:
        path = tmp_path / "defaults.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        monkeypatch.setenv(local_defaults.LOCALE_FILE_ENV, str(path))
        local_defaults.clear_cache()

    yield write
    local_defaults.clear_cache()


def test_env_wins_over_the_local_file(locale_file):
    locale_file({"X_TENANT": "acme"})

    assert local_defaults.get("X_TENANT", "default", env={"X_TENANT": "other"}) == "other"


def test_blank_env_falls_through_to_the_local_file(locale_file):
    locale_file({"X_TENANT": "acme"})

    assert local_defaults.get("X_TENANT", "default", env={"X_TENANT": "  "}) == "acme"


def test_neutral_fallback_without_a_local_file(tmp_path, monkeypatch):
    monkeypatch.setenv(local_defaults.LOCALE_FILE_ENV, str(tmp_path / "missing.json"))
    local_defaults.clear_cache()
    try:
        assert local_defaults.get("X_TENANT", "default", env={}) == "default"
        assert local_defaults.get_list("X_LIST") == ()
    finally:
        local_defaults.clear_cache()


def test_malformed_file_degrades_to_fallback(locale_file, caplog):
    locale_file("{not json")

    assert local_defaults.get("X_TENANT", "default", env={}) == "default"
    assert "unreadable" in caplog.text


def test_underscore_keys_are_comments(locale_file):
    locale_file({"_comment": "x", "X": "y"})

    assert "_comment" not in local_defaults.defaults()


def test_get_list_drops_blanks(locale_file):
    locale_file({"X_LIST": ["a", " ", "b "]})

    assert local_defaults.get_list("X_LIST") == ("a", "b")


def _installation_values() -> list[str]:
    """Every value of the real locale file, as the needle to look for in code.

    Read from the file rather than listed here, so the test itself names nothing
    of the installation (#43) and follows the file when a value is added. A bare
    word (the bus tenant) is searched quoted, as a literal would appear in code.
    """
    import re
    from pathlib import Path

    path = Path(local_defaults.__file__).resolve().parent / "locale" / "defaults.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    values: list[str] = []
    for key, value in data.items():
        # A mount point inside the container, which the engine documents: not an identity.
        if key.startswith("_") or key == "MICROSOFT_TOKEN_CACHE_FILE":
            continue
        for item in value if isinstance(value, list) else [value]:
            item = str(item).strip()
            if item:
                values.append(f'"{item}"' if re.fullmatch(r"[\w-]+", item) else item)
    return values


def test_engine_modules_carry_no_installation_value():
    """The values that left the code in #43 must not creep back in."""
    from pathlib import Path

    needles = _installation_values()
    if not needles:
        pytest.skip("no locale/defaults.json: nothing installation-specific to leak")
    root = Path(local_defaults.__file__).resolve().parent
    for name in ("graph_mailer.py", "hub_claim.py", "hub_watch.py", "deployer_config.py",
                 "sentinella.py", "sentinella_probes.py", "notifications.py"):
        source = (root / name).read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in source, f"{needle} in {name}"
