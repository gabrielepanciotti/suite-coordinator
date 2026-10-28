"""Every local module bot.py imports must be bind-mounted into the container.

The compose file mounts the source files one by one, by hand. A new module is
therefore invisible to the running container until somebody remembers to add a
line — and "somebody remembers" has already failed three times:

  * 2026-08-06: life_registry_client.py and node_menu.py were not mounted, so
    the container silently ran the image's older copies. The fix looked applied
    and was not: the only symptom was a menu serving zero nodes.
  * 2026-08-07: node_routing.py, same omission. This one crashed on import
    instead of degrading, which is the lucky case, not the general one.

An import that is missing is loud; an import that is *stale* is silent. This
test covers both by checking the list, not the symptom. And the third time
(2026-10-27, #103) the module came in through another module, not bot.py:
the list is now the transitive closure (scripts/deploy/check_mounts.py).
"""
from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
PRIVATE_COMPOSE = REPO / "docker-compose.yml"
_spec = importlib.util.spec_from_file_location(
    "check_mounts", REPO / "scripts" / "deploy" / "check_mounts.py")
check_mounts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_mounts)


def test_every_module_a_service_imports_is_mounted_into_it():
    """Transitively, from each service's entry point, in every compose file (#103:
    competenza_routing came in through projects.py, not bot.py, and slipped by)."""
    if not PRIVATE_COMPOSE.is_file():
        import pytest
        pytest.skip("production compose files are intentionally absent from the public export")
    missing = {
        f"{compose}:{service}": modules
        for compose in check_mounts.COMPOSE_FILES
        for service, modules in check_mounts.missing_mounts(REPO / compose).items()
    }
    assert not missing, (
        "questi moduli non sono montati nel container: il codice che gira sarebbe "
        f"quello dell'immagine, non questo. Aggiungili al compose: {missing}"
    )


def test_the_check_follows_imports_of_imports(tmp_path):
    (tmp_path / "bot.py").write_text("import projects\n")
    (tmp_path / "projects.py").write_text("def f():\n    from competenza import x\n")
    (tmp_path / "competenza.py").write_text("import json\n")
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n"
        "  coordinator-bot:\n"
        '    command: ["python", "bot.py"]\n'
        "    volumes:\n"
        "      - ./bot.py:/app/bot.py:ro\n"
        "      - ./projects.py:/app/projects.py:ro\n"
        "  coordinator-worker-poller:\n"
        '    command: ["python", "bot.py"]\n'
        "    volumes:\n"
        "      - .:/app:ro\n"
        "  coordinator-db:\n"
        "    image: postgres:16\n"
    )
    assert check_mounts.missing_mounts(compose, tmp_path) == {"coordinator-bot": ["competenza"]}


def test_life_instance_slots_mount_each_checkout_at_its_own_path():
    """Coordinator#78: instances under different parent dirs need one mount each,
    at the SAME path (instances.json holds absolute paths). Both compose files."""
    import pytest
    names = ("docker-compose.yml", "docker-compose.prod.yml")
    absent = [name for name in names if not (REPO / name).is_file()]
    if absent:
        pytest.skip("instance mounts belong to excluded production compose files: " + ", ".join(absent))
    for name in names:
        compose = (REPO / name).read_text(encoding="utf-8")
        for i in range(1, 5):
            var = f"LIFE_INSTANCE_MOUNT_{i}"
            assert (
                f"${{{var}:-./.ws-placeholder}}:${{{var}:-/workspaces/.life-instance-{i}-non-usato}}:ro"
                in compose
            ), f"{name}: slot {var} mancante o non a path identico"


def test_poller_mounts_the_instance_checkouts():
    """#83 F5: the standby poller reads the ladder from the instances; without
    their checkouts it must not poll at all, so they have to be mounted."""
    import pytest
    compose_path = REPO / "docker-compose.prod.yml"
    if not compose_path.is_file():
        pytest.skip("the production poller compose file is intentionally absent from the public export")
    compose = compose_path.read_text(encoding="utf-8")
    poller = compose.split("  coordinator-worker-poller:", 1)[1].split("\n  coordinator-", 1)[0]
    assert "${LIFE_INSTANCES_HOST_DIR:-./.ws-placeholder}" in poller
    for i in range(1, 5):
        assert f"${{LIFE_INSTANCE_MOUNT_{i}:-./.ws-placeholder}}" in poller
