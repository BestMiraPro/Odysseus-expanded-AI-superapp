"""I01 — the lock check must compare constraints, not only names.

``check_lock_covers_requirements.py`` compared normalised distribution names,
so changing ``package>=1`` to ``package>=2`` still passed against a lock
pinning ``package==1.5``. The CI job is labelled "Dependency lock is current",
which promises considerably more than name coverage.

Three distinct questions, kept distinct here:

* **name coverage** — is every direct requirement pinned at all?
* **constraint compatibility** — does the pin satisfy the declared specifier?
* **full resolver freshness** — only a real resolve can answer that; the job
  says so rather than implying it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "scripts" / "check_lock_covers_requirements.py"


@pytest.fixture
def check():
    spec = importlib.util.spec_from_file_location("check_lock", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _files(tmp_path, requirements, lock):
    req = tmp_path / "requirements.txt"
    lk = tmp_path / "requirements.lock.txt"
    req.write_text(requirements, encoding="utf-8")
    lk.write_text(lock, encoding="utf-8")
    return req, lk


# --------------------------------------------------------------------------
# Constraint compatibility
# --------------------------------------------------------------------------

def test_an_incompatible_pin_is_reported(check, tmp_path):
    req, lock = _files(tmp_path, "package>=2\n", "package==1.5\n")
    problems = check.incompatible_with_lock(req, lock)
    assert problems, "package>=2 against package==1.5 was accepted"
    assert "package" in problems[0]


def test_a_compatible_pin_passes(check, tmp_path):
    req, lock = _files(tmp_path, "package>=1\n", "package==1.5\n")
    assert check.incompatible_with_lock(req, lock) == []


def test_an_upper_bound_violation_is_reported(check, tmp_path):
    req, lock = _files(tmp_path, "mcp<2\n", "mcp==2.1.1\n")
    problems = check.incompatible_with_lock(req, lock)
    assert problems, "mcp<2 against mcp==2.1.1 was accepted"


def test_a_compound_specifier_is_honoured(check, tmp_path):
    req, lock = _files(tmp_path, "httpcore>=1.0,<2.0\n", "httpcore==1.0.9\n")
    assert check.incompatible_with_lock(req, lock) == []
    req, lock = _files(tmp_path, "httpcore>=1.0,<2.0\n", "httpcore==2.0.1\n")
    assert check.incompatible_with_lock(req, lock)


def test_an_unconstrained_requirement_accepts_any_pin(check, tmp_path):
    req, lock = _files(tmp_path, "fastapi\n", "fastapi==0.1.0\n")
    assert check.incompatible_with_lock(req, lock) == []


def test_names_are_canonicalised_before_comparison(check, tmp_path):
    req, lock = _files(tmp_path, "Python_DateUtil>=2\n", "python-dateutil==2.9.0\n")
    assert check.incompatible_with_lock(req, lock) == []
    assert check.missing_from_lock(req, lock) == []


# --------------------------------------------------------------------------
# Environment markers
# --------------------------------------------------------------------------

def test_a_requirement_excluded_by_a_marker_is_skipped(check, tmp_path):
    """Not an error and not a missing pin — it does not apply to this runtime."""
    req, lock = _files(tmp_path, 'pywin32; sys_platform == "win32"\n', "")
    assert check.missing_from_lock(req, lock) == []
    assert check.incompatible_with_lock(req, lock) == []


def test_a_requirement_included_by_a_marker_is_still_checked(check, tmp_path):
    req, lock = _files(tmp_path, 'package>=2; sys_platform == "linux"\n', "package==1.0\n")
    assert check.incompatible_with_lock(req, lock), (
        "a requirement that applies on the target platform was not checked"
    )


# --------------------------------------------------------------------------
# Extras and unsupported forms
# --------------------------------------------------------------------------

def test_extras_are_flagged_for_a_deeper_check(check, tmp_path):
    """A lightweight check cannot prove an extra's closure is in the lock."""
    req, lock = _files(tmp_path, "qrcode[pil]\n", "qrcode==8.0\n")
    assert check.extras_needing_resolution(req) == [("qrcode", ["pil"])]


def test_a_requirement_without_extras_needs_no_deeper_check(check, tmp_path):
    req, _lock = _files(tmp_path, "fastapi\n", "fastapi==0.1.0\n")
    assert check.extras_needing_resolution(req) == []


def test_a_url_requirement_fails_clearly(check, tmp_path):
    req, lock = _files(tmp_path, "package @ https://example.invalid/p.whl\n", "")
    problems = check.unsupported_forms(req)
    assert problems, "a URL requirement passed silently"
    assert "package" in problems[0]


# --------------------------------------------------------------------------
# The real committed files
# --------------------------------------------------------------------------

def test_the_committed_lock_satisfies_every_committed_constraint(check):
    problems = check.incompatible_with_lock(
        REPO / "requirements.txt", REPO / "requirements.lock.txt"
    )
    assert problems == [], (
        f"the lock violates a declared constraint: {problems}. "
        "Re-run scripts/lock_requirements.sh."
    )


def test_the_committed_requirements_use_no_unsupported_forms(check):
    assert check.unsupported_forms(REPO / "requirements.txt") == []


def test_main_fails_on_an_incompatible_pin(check, tmp_path):
    req, lock = _files(tmp_path, "package>=2\n", "package==1.5\n")
    assert check.main(req, lock) == 1


def test_main_passes_on_a_compatible_lock(check, tmp_path):
    req, lock = _files(tmp_path, "package>=1\n", "package==1.5\n")
    assert check.main(req, lock) == 0


# --------------------------------------------------------------------------
# The dev file must not undo the lock
# --------------------------------------------------------------------------

def test_dev_requirements_do_not_reinclude_the_loose_runtime_file():
    """CI installs the lock *and* the dev file. If the dev file pulls in the
    loose requirements.txt, pip is free to resolve past the pins."""
    text = (REPO / "requirements-dev.txt").read_text(encoding="utf-8")
    lines = [ln.split("#", 1)[0].strip() for ln in text.splitlines()]
    assert "-r requirements.txt" not in lines, (
        "requirements-dev.txt re-includes the unpinned runtime file, so the "
        "lock is not authoritative in the lane that installs both"
    )
