"""A declared dependency must never be silently mocked away.

``tests/conftest.py`` replaces missing modules with ``MagicMock`` at import
time so collection can proceed in a stripped-down environment. Every module on
that list except ``starlette`` is a *declared runtime dependency*, so in a
broken checkout the suite quietly runs against mocks of SQLAlchemy, FastAPI,
pydantic, bcrypt and httpx — and passes.

That is not hypothetical. During the September 5, 2026 review, 18 of the 60
declared packages were missing from a working checkout; six test modules failed
to collect and the review's own async regression could not run, while the rest
of the suite reported green.

These tests make a broken environment fail once, loudly and legibly, instead of
producing thousands of false passes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO = Path(__file__).resolve().parent.parent

# Top-level module each conftest stub entry belongs to, mapped to the
# distribution that provides it. starlette is deliberately absent from
# requirements.txt — it arrives transitively via fastapi.
STUBBED_MODULES = {
    "sqlalchemy": "SQLAlchemy",
    "bcrypt": "bcrypt",
    "pyotp": "pyotp",
    "httpx": "httpx",
    "fastapi": "fastapi",
    "pydantic": "pydantic",
    "starlette": None,      # transitive via fastapi
}


def _declared_requirements():
    names = set()
    for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9._-]+)", line)
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def _is_stub(module) -> bool:
    return isinstance(module, MagicMock)


@pytest.mark.parametrize("module_name", sorted(STUBBED_MODULES))
def test_core_dependency_is_real_not_a_stub(module_name):
    module = sys.modules.get(module_name)
    if module is None:
        pytest.fail(
            f"{module_name!r} is not imported at all. Install the project "
            f"dependencies: pip install -r requirements-dev.txt"
        )
    assert not _is_stub(module), (
        f"{module_name!r} is a MagicMock stub, not the real library. Every test "
        f"touching it is passing against a mock. Install the project "
        f"dependencies: pip install -r requirements-dev.txt"
    )


def test_every_stubbed_module_is_accounted_for():
    """The stub list and this guard must not drift apart.

    A module added to conftest's stub list without being listed here would be
    mockable again with nothing noticing.
    """
    conftest = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    block = conftest.split("for mod_name in [", 1)[1].split("]:", 1)[0]
    listed = {
        entry.strip().strip('"').strip("'").split(".")[0]
        for entry in block.replace("\n", "").split(",")
        if entry.strip()
    }
    listed.discard("")
    unaccounted = listed - set(STUBBED_MODULES)
    assert unaccounted == set(), (
        f"conftest stubs {sorted(unaccounted)} but this guard does not check "
        f"them; add them to STUBBED_MODULES."
    )


def test_stubbing_only_covers_declared_or_transitive_dependencies():
    """Documents why the stub list is dangerous: it is nearly all real deps."""
    declared = _declared_requirements()
    for module_name, distribution in STUBBED_MODULES.items():
        if distribution is None:
            continue
        assert distribution.lower() in declared, (
            f"{module_name} is stubbed but {distribution} is no longer a "
            f"declared requirement; the stub entry is stale."
        )


def test_subprocess_stdin_shim_is_installed_and_reversible():
    """The global shim must be restorable, not a one-way import-time mutation."""
    import subprocess

    from tests import conftest as ct

    assert getattr(subprocess, "_odysseus_stdin_default", False), (
        "the stdin shim is not installed; Windows subprocess tests will flake"
    )
    assert callable(getattr(ct, "_uninstall_subprocess_stdin_default", None)), (
        "the shim cannot be removed, so it cannot be scoped or reasoned about"
    )


def test_shim_round_trips_cleanly():
    import subprocess

    from tests import conftest as ct

    original_run = subprocess.run
    ct._uninstall_subprocess_stdin_default()
    try:
        assert not getattr(subprocess, "_odysseus_stdin_default", False)
        assert subprocess.run is not original_run, "uninstall did not restore run"
    finally:
        ct._install_subprocess_stdin_default()
    assert getattr(subprocess, "_odysseus_stdin_default", False)
