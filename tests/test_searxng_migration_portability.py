"""The SearXNG settings migration must run on every platform it ships to.

``scripts/migrate_searxng_settings.py`` writes atomically and preserves the
file's owner and mode, because the Compose cap set (``cap_drop: ALL`` plus
CHOWN/SETGID/SETUID/DAC_OVERRIDE, no FOWNER) means root cannot chmod a file
already owned by ``searxng:searxng``. That reasoning is POSIX-specific, and so
are the calls implementing it — ``os.fchmod``, ``os.fchown`` and a directory
``fsync`` via ``O_DIRECTORY``. None exist on Windows, so the script raised
``AttributeError`` there and 12 of its tests failed.

Ownership preservation must not be weakened to achieve portability. These tests
pin both halves: the POSIX guarantees stay exactly as they were where the
platform supports them, and the atomic replace still works where it does not.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_searxng_settings.py"

RETAINED = b"server:\n  secret_key: 'abc'\n"


def _module():
    spec = importlib.util.spec_from_file_location("migrate_searxng_settings", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrate():
    return _module()


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "settings.yml"
    path.write_bytes(RETAINED)
    return path


def test_migration_runs_on_this_platform(migrate, settings):
    """The regression: it raised AttributeError on Windows before reaching disk."""
    assert migrate.migrate_settings(settings) is True
    assert b"use_default_settings: true" in settings.read_bytes()


def test_original_content_is_preserved(migrate, settings):
    migrate.migrate_settings(settings)
    body = settings.read_bytes()
    assert b"secret_key: 'abc'" in body
    assert b"server:" in body


def test_no_temporary_file_is_left_behind(migrate, settings):
    migrate.migrate_settings(settings)
    leftovers = [p.name for p in settings.parent.iterdir() if p.name != settings.name]
    assert leftovers == [], f"temporary files left: {leftovers}"


def test_migration_is_idempotent(migrate, settings):
    assert migrate.migrate_settings(settings) is True
    after_first = settings.read_bytes()
    assert migrate.migrate_settings(settings) is False
    assert settings.read_bytes() == after_first


def test_the_file_is_replaced_not_truncated_in_place(migrate, settings, monkeypatch):
    """Atomicity is the whole point: a crash must never leave a partial file."""
    replaced = []
    real_replace = os.replace

    def spy(src, dst, *a, **k):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(migrate.os, "replace", spy)
    migrate.migrate_settings(settings)
    assert len(replaced) == 1
    assert replaced[0][1] == str(settings)


# --------------------------------------------------------------------------
# POSIX guarantees must survive the portability work
# --------------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX-only guarantee")
def test_ownership_is_still_preserved_on_posix(migrate, settings, monkeypatch):
    calls = []
    monkeypatch.setattr(migrate.os, "fchown",
                        lambda fd, uid, gid: calls.append(("fchown", uid, gid)))
    monkeypatch.setattr(migrate.os, "fchmod",
                        lambda fd, mode: calls.append(("fchmod", mode)))

    migrate.migrate_settings(settings)

    names = [c[0] for c in calls]
    assert "fchmod" in names, "mode preservation was dropped"
    assert "fchown" in names, "ownership preservation was dropped"
    # chmod must precede chown: once the file belongs to searxng:searxng, root
    # has no FOWNER and can no longer chmod it.
    assert names.index("fchmod") < names.index("fchown")


@pytest.mark.skipif(hasattr(os, "fchown"), reason="checks the non-POSIX path")
def test_windows_skips_ownership_but_still_writes(migrate, settings):
    """There is no owner/mode model to preserve here, and no searxng container."""
    assert migrate.migrate_settings(settings) is True
    assert b"use_default_settings: true" in settings.read_bytes()


def test_posix_only_calls_are_capability_guarded():
    """Guard on the attribute, not the platform name.

    A ``sys.platform`` test would silently skip these on any POSIX-like
    platform the string check did not anticipate, quietly dropping the
    ownership guarantee where it is genuinely needed.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    for call in ("fchown", "fchmod"):
        assert f'hasattr(os, "{call}")' in source, (
            f"os.{call} is not capability-guarded"
        )
