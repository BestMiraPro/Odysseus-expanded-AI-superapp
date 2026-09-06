"""The committed dependency lock must not drift from requirements.txt.

A lock nobody regenerates is worse than no lock: Docker and CI install it, so
a runtime dependency added to requirements.txt but missing from the lock is
simply absent at runtime — which is how 18 declared packages came to be missing
from a working checkout during the September 5 review.

The CI job runs ``.github/scripts/check_lock_covers_requirements.py``; these
tests drive the same module directly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "scripts" / "check_lock_covers_requirements.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_lock", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def check():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    return _load()


# --------------------------------------------------------------------------
# Requirement-name parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("fastapi", "fastapi"),
    ("httpcore>=1.0,<2.0", "httpcore"),
    ("pydantic>=2.13.4", "pydantic"),
    ("mcp<2", "mcp"),
    ("qrcode[pil]", "qrcode"),
    ("SQLAlchemy", "sqlalchemy"),
    ("psycopg2-binary", "psycopg2-binary"),
    ("python_dateutil", "python-dateutil"),   # PEP 503 normalisation
    ("  numpy  ", "numpy"),
])
def test_requirement_names_are_normalised(check, line, expected):
    assert check.requirement_name(line) == expected


@pytest.mark.parametrize("line", ["", "   ", "# a comment", "-r requirements.txt", "--index-url x"])
def test_non_requirement_lines_are_skipped(check, line):
    assert check.requirement_name(line) is None


def test_inline_comments_are_stripped(check):
    assert check.requirement_name("numpy  # fast arrays") == "numpy"


# --------------------------------------------------------------------------
# Coverage check
# --------------------------------------------------------------------------

def test_missing_package_is_reported(check, tmp_path):
    req = tmp_path / "requirements.txt"
    lock = tmp_path / "requirements.lock.txt"
    req.write_text("fastapi\nnumpy\n", encoding="utf-8")
    lock.write_text("fastapi==0.1.0\n", encoding="utf-8")

    missing = check.missing_from_lock(req, lock)
    assert missing == ["numpy"]


def test_fully_covered_requirements_report_nothing(check, tmp_path):
    req = tmp_path / "requirements.txt"
    lock = tmp_path / "requirements.lock.txt"
    req.write_text("fastapi\nSQLAlchemy\nqrcode[pil]\n", encoding="utf-8")
    lock.write_text("fastapi==0.1.0\nsqlalchemy==2.0.0\nqrcode==8.0\n", encoding="utf-8")

    assert check.missing_from_lock(req, lock) == []


def test_lock_may_contain_transitive_extras(check, tmp_path):
    """The lock is a full resolution, so it is a superset — that is fine."""
    req = tmp_path / "requirements.txt"
    lock = tmp_path / "requirements.lock.txt"
    req.write_text("fastapi\n", encoding="utf-8")
    lock.write_text("fastapi==0.1.0\nstarlette==0.40.0\nanyio==4.0.0\n", encoding="utf-8")

    assert check.missing_from_lock(req, lock) == []


def test_main_exits_nonzero_when_the_lock_is_stale(check, tmp_path):
    req = tmp_path / "requirements.txt"
    lock = tmp_path / "requirements.lock.txt"
    req.write_text("fastapi\nnumpy\n", encoding="utf-8")
    lock.write_text("fastapi==0.1.0\n", encoding="utf-8")

    assert check.main(req, lock) == 1


def test_main_exits_zero_when_the_lock_is_current(check, tmp_path):
    req = tmp_path / "requirements.txt"
    lock = tmp_path / "requirements.lock.txt"
    req.write_text("fastapi\n", encoding="utf-8")
    lock.write_text("fastapi==0.1.0\n", encoding="utf-8")

    assert check.main(req, lock) == 0


# --------------------------------------------------------------------------
# The real committed files
# --------------------------------------------------------------------------

def test_the_committed_lock_covers_the_committed_requirements(check):
    missing = check.missing_from_lock(
        REPO / "requirements.txt", REPO / "requirements.lock.txt"
    )
    assert missing == [], (
        f"requirements.lock.txt is stale — missing {missing}. "
        "Re-run scripts/lock_requirements.sh."
    )


def test_dev_requirements_are_not_in_the_runtime_file(check):
    """The runtime image must not ship a test framework."""
    runtime = {
        check.requirement_name(line)
        for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()
    }
    runtime.discard(None)
    assert "pytest" not in runtime
    assert "pytest-asyncio" not in runtime
    assert "httpx2" not in runtime
